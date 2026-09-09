"""End-to-end forecast assembly: pulled inputs -> frames for the workbook.

`run_forecast` is pure over its inputs (no BigQuery, no files) so it can be
tested on the committed fixtures and re-run on any pulled snapshot. It returns a
`ForecastBundle` whose frames the report renderer lays out according to
`config/report_target.yaml`.

Frames produced (keys used by the report spec):

    weekly          TCIN x PO week: expected PO units, grade, band, stream, flags
    shipments       TCIN x ship week (replenishment forecast + booked forward)
    monthly         TCIN x month x stream: expected shipments for S&OP with grade
    accuracy_*      backtest tables that justify the grades and bands
    bm_store_check  B&M sheet store plan vs BPD selling stores, per TCIN
    bm_load_check   B&M Load_Orders vs the launch/forward units the engine carries
    bm_coverage     which TCINs the sheet, the item master and the run each cover
    exceptions      every item that needs a human eye
    legend          grade -> confidence -> expected error band

The channel owner's Brick & Mortar store plan (`bm_forecast`, from
`biom_admin.bm_target_schedule_snapshot` or a `--bm` file) shapes the measured POS
forecast's forward store count BEFORE the weekly simulation and never sets its level;
see model/bm_combine.py. Every row carries `authority`: measured, measured_shaped_by_plan
or stated_only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from pipelines.target_shipment_forecast.backtest import scoring
from pipelines.target_shipment_forecast.backtest.rolling import panel_to_long
from pipelines.target_shipment_forecast.channels.target import forward
from pipelines.target_shipment_forecast.channels.target.calendar import TargetCalendar, sunday_week
from pipelines.target_shipment_forecast.inputs.item_master import ItemMaster
from pipelines.target_shipment_forecast.model import bm_combine as bc
from pipelines.target_shipment_forecast.model import consumption, intervals, plan_anchor
from pipelines.target_shipment_forecast.model import grade as grading

PO_VS_SHIP_NOTE = (
    "'Expected PO units (create month)' and 'Expected shipments (units)' are the SAME "
    "weekly orders bucketed by two different dates (PO week vs PO week + the item "
    "group's ship offset), never one plus the other. They are equal in total and differ "
    "per month by what a week carries across a month end: "
    "create_month = ship_month - (carried in from the prior month) + (carried out to the "
    "next). Worked example, TCIN 94928292 Sep-26: 14,832 = 6,480 - 552 + 8,904."
)
"""Stated in the README sheet so no reader (or downstream document) infers addition.

biom_sql fix (a), 2026-09-07: the upstream workbook guide described the create-month
figure as the ship-month figure "plus" the last week of the month, which reads as
6,480 + 8,904 = 15,384 and is wrong by exactly the 552 units created in the week of
30 Aug — an August create month that ships on 4 Sep. `test_model.py` asserts the
identity so it cannot drift from the code again.
"""


@dataclass
class ForecastBundle:
    """Everything the workbook needs."""

    frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    readme: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _cfg(cfg: Mapping[str, Any], *keys: str, default: Any = None, field_name: str = "value") -> Any:
    node: Any = cfg
    for k in keys:
        if not isinstance(node, Mapping) or k not in node:
            return default
        node = node[k]
    if isinstance(node, Mapping) and field_name in node:
        return node[field_name]
    return node if not isinstance(node, Mapping) else default


def _item_attrs(item_master: ItemMaster, calendar: TargetCalendar) -> pd.DataFrame:
    """Per TCIN: sku, description, item_group, ship_offset_days, casepack, item_state."""
    im = item_master.frame.reset_index(drop=True)
    rows = []
    for r in im.itertuples(index=False):
        dept, cls = item_master.department_class(int(r.tcin))
        group = calendar.item_group_for(dept, cls)
        sku = item_master.sku_for(int(r.tcin))
        desc = getattr(r, "target_description", None)
        rows.append(
            {
                "tcin": int(r.tcin),
                "sku": sku,
                "description": desc if isinstance(desc, str) else "",
                "item_group": group or "",
                "ship_offset_days": calendar.ship_offset_days(group) if group else 5,
                "casepack": item_master.casepack_of_record(int(r.tcin)).casepack,
                "item_state": getattr(r, "item_state", None),
                "mapping_confidence": getattr(r, "mapping_confidence", None),
            }
        )
    return pd.DataFrame(rows)


def _resolve_item_state(
    attrs: pd.DataFrame,
    live: pd.DataFrame | None,
    *,
    as_of: date,
    max_age_days: int,
) -> tuple[dict[int, str], dict[str, Any]]:
    """TCIN -> Target ITEM_STATE, preferring the live feed over the committed CSV.

    biom_sql fix (d), 2026-09-07. `item_state` drives two things — the LAUNCH_FILL flag
    and, through it, the `planned_forward` stream — and it used to come only from
    `data/item_master_target.csv`, a hand-maintained snapshot. A launch that changed
    state in Target's feed therefore could not change a Shipcast stream until someone
    edited the CSV.

    The CSV is kept as a per-TCIN FALLBACK rather than deleted, because the live feed
    does not cover the whole universe (42 of the 43 TCINs on 2026-09-07). Every
    substitution is REPORTED, never silent: the returned meta drives Exceptions rows
    for a diverged state, a TCIN the feed does not carry, and a feed older than
    `max_age_days`.
    """
    csv_state = {
        int(r.tcin): r.item_state for r in attrs.itertuples() if isinstance(r.item_state, str)
    }
    meta: dict[str, Any] = {
        "source": "committed CSV (data/item_master_target.csv)",
        "live_rows": 0,
        "live_last_update": None,
        "age_days": None,
        "used_live": False,
        "diverged": [],
        "not_in_live_feed": [],
        "stale": False,
    }
    if live is None or live.empty:
        meta["reason"] = "live item_state not pulled (run `pull` to refresh)"
        return csv_state, meta

    lv = live.copy()
    lv["tcin"] = pd.to_numeric(lv["tcin"], errors="coerce")
    lv = lv[lv["tcin"].notna() & lv["item_state"].notna()]
    last = pd.to_datetime(lv["last_update_date"], errors="coerce", utc=True).max()
    meta["live_rows"] = int(len(lv))
    if pd.notna(last):
        meta["live_last_update"] = str(last.date())
        # DATE grain on both sides, matching the SQL's `DATE(last_update_date) <= @as_of`.
        # Subtracting an intraday TIMESTAMP from midnight-on-as_of reported age_days = -1
        # for a feed published the same day (first-run validation, 2026-09-07) — a negative
        # age that the `age > max_age` staleness guard cannot catch.
        meta["age_days"] = (as_of - last.date()).days

    if meta["age_days"] is not None and meta["age_days"] > max_age_days:
        meta["stale"] = True
        meta["reason"] = f"stale: {meta['age_days']} d > {max_age_days} d; kept the committed CSV"
        return csv_state, meta

    live_state = {int(r.tcin): str(r.item_state) for r in lv.itertuples()}
    resolved = dict(csv_state)
    for tcin, state in live_state.items():
        prior = csv_state.get(tcin)
        if prior is not None and prior != state:
            meta["diverged"].append({"tcin": tcin, "csv": prior, "live": state})
        resolved[tcin] = state
    meta["not_in_live_feed"] = sorted(set(csv_state) - set(live_state))
    meta["used_live"] = True
    meta["source"] = "bpd_raw.wkly_tcin_item (live), CSV fallback per TCIN"
    return resolved, meta


def _booked_forward(
    orders: pd.DataFrame | None, *, as_of: date, calendar: TargetCalendar, fwd_days: int, grace: int
) -> pd.DataFrame:
    """Open forward/launch PO lines -> `tcin, ship_week, receipt_week, units, purchase_order_id`."""
    cols = ["tcin", "ship_week", "receipt_week", "units", "purchase_order_id", "ship_begin_d"]
    if orders is None or orders.empty:
        return pd.DataFrame(columns=cols)
    lines = forward.classify_lines(
        orders, as_of=as_of, forward_threshold_days=fwd_days, lapsed_grace_days=grace
    )
    fwd = forward.open_lines(lines)
    fwd = fwd[fwd["stream"] == forward.FORWARD]
    if fwd.empty:
        return pd.DataFrame(columns=cols)
    f = fwd.copy()
    f["ship_week"] = sunday_week(f["ship_begin_d"])
    transit = f["receiving_location_id"].map(
        lambda dc: calendar.transit_days(int(dc)) if pd.notna(dc) else calendar.transit_default_days
    )
    f["receipt_week"] = sunday_week(f["ship_begin_d"] + pd.to_timedelta(transit, unit="D"))
    g = f.groupby(["tcin", "ship_week", "receipt_week", "purchase_order_id"], as_index=False).agg(
        units=("open_units", "sum"), ship_begin_d=("ship_begin_d", "min")
    )
    return g[cols]


def _cases(units: pd.Series, casepack: pd.Series) -> pd.Series:
    cp = pd.to_numeric(casepack, errors="coerce")
    return (pd.to_numeric(units, errors="coerce") / cp.where(cp > 0)).round(1)


def run_forecast(
    inputs: Mapping[str, Any],
    *,
    cfg: Mapping[str, Any],
    calendar: TargetCalendar,
    item_master: ItemMaster,
    as_of: date,
    horizon_weeks: int = 16,
    months: int = 16,
    panel: pd.DataFrame | None = None,
) -> ForecastBundle:
    """Assemble the weekly and monthly forecasts plus accuracy and exceptions frames.

    `inputs` keys (from `shipcast pull` / adapters): `plan_snapshots`,
    `orders_latest`, `po_actuals_weekly`, `sales_weekly`, `inventory_weekly`,
    `dfe_asof`, `item_state`, `launch_seed` (curated assumptions,
    `biom_admin.seed_target_launch_velocity`), plus `bm_forecast` (a parsed
    `BmForecast` of the channel owner's B&M Target Schedule, or None) and
    `bm_schedule_meta` (where it came from; see run.bm_schedule_input).

    EVERY input is a BigQuery read in the scheduled path. There is no Drive call and no
    manually placed file: the monthly POS forecast is `consumption.dist_velocity`, the
    `planned_launch` stream comes from Target's own PO plan plus the live item-state
    feed, and the B&M sheet arrives as a BigQuery snapshot landed by its own ingest job.
    The RDZ supply sheet was removed 2026-09-08, so no cell is capped by Biom-side
    supply, which was already true in v1 -- the RDZ read only ever produced an
    Exceptions row.
    `panel` is the committed backtest panel (tests/fixtures/signal_panel.csv)
    or a fresher one; it drives the bands and the Accuracy sheet.
    """
    b = ForecastBundle()
    plan = inputs.get("plan_snapshots")
    orders = inputs.get("orders_latest")
    actuals = inputs.get("po_actuals_weekly")
    sales = inputs.get("sales_weekly")
    inv = inputs.get("inventory_weekly")
    dfe = inputs.get("dfe_asof")
    launch_seed = inputs.get("launch_seed")
    bm_fc = inputs.get("bm_forecast")  # BmForecast | None
    bm_meta: dict[str, Any] = dict(inputs.get("bm_schedule_meta") or {})
    bm_frame = bm_fc.frame if bm_fc is not None and not bm_fc.frame.empty else None
    attrs = _item_attrs(item_master, calendar)
    attr_sku = {int(r.tcin): r.sku for r in attrs.itertuples(index=False)}
    labels = grading.labels_from_config(cfg)
    fwd_days = int(_cfg(cfg, "streams", "forward_threshold_days", default=14))
    grace = int(_cfg(cfg, "streams", "lapsed_grace_days", default=7))
    pf_mult = float(_cfg(cfg, "streams", "planned_forward_multiple", default=3.0))
    stale_plan = int(_cfg(cfg, "freshness", "stale_plan_days", default=4))
    trailing = int(_cfg(cfg, "consumption", "trailing_pos_weeks", default=8))
    rr_early = (
        consumption.runrate(sales, as_of, weeks=trailing)
        if not sales.empty
        else pd.DataFrame(columns=["tcin", "pos_wk"])
    )
    pos_map = {int(r.tcin): float(r.pos_wk) for r in rr_early.itertuples()}

    # ---- 1. weekly plan-anchored forecast -------------------------------------------
    state_map, item_state_meta = _resolve_item_state(
        attrs,
        inputs.get("item_state"),
        as_of=as_of,
        max_age_days=int(_cfg(cfg, "freshness", "item_state_max_age_days", default=21)),
    )
    weekly = plan_anchor.expected_po_units(
        plan if plan is not None else pd.DataFrame(),
        actuals if actuals is not None else pd.DataFrame(),
        as_of=as_of,
        horizon_weeks=horizon_weeks,
        calendar=calendar,
        tcins=item_master.tcins,
        item_state=state_map,
        pos_runrate=pos_map,
        planned_forward_multiple=pf_mult,
        stale_plan_days=stale_plan,
    )
    weekly = grading.grade_rows(weekly, cfg["grades"]["by_lead_days"])

    # ---- 2. empirical bands from the backtest panel --------------------------------
    c = float(_cfg(cfg, "intervals", "log_offset_c", default=1.0))
    min_pool = int(_cfg(cfg, "intervals", "min_pool_rows", default=100))
    fitted = pd.DataFrame()
    coverage = pd.DataFrame()
    long_rows = pd.DataFrame()
    if panel is not None and not panel.empty:
        long_rows = panel_to_long(panel)
        plan_rows = long_rows[long_rows["signal"] == "plan_sat_ordered"]
        if not plan_rows.empty:
            fitted = intervals.fit_ratio_quantiles(plan_rows, c=c, min_pool_rows=min_pool)
            coverage = intervals.lowo_coverage(plan_rows, c=c, min_pool_rows=min_pool)
    if not fitted.empty:
        is_plan = weekly["fallback_rung"].isin([plan_anchor.RUNG_PLAN])
        banded = intervals.apply_bands(weekly[is_plan], fitted, c=c)
        weekly = weekly.merge(
            banded[["po_week", "tcin", "low", "high", "band_source"]],
            on=["po_week", "tcin"],
            how="left",
        )
    weekly = grading.attach_confidence(weekly, labels, value_col="expected_po_units")
    weekly.loc[weekly["fallback_rung"] == plan_anchor.RUNG_CREATED, ["low", "high"]] = np.nan
    weekly = weekly.merge(attrs, on="tcin", how="left")
    weekly["ship_week"] = sunday_week(
        weekly["po_week"]
        + pd.to_timedelta(weekly["ship_offset_days"].fillna(5).astype(int), unit="D")
    )
    weekly["expected_cases"] = _cases(weekly["expected_po_units"], weekly["casepack"])
    weekly["po_week_label"] = weekly["po_week"].dt.strftime("%Y-%m-%d")
    fy = weekly["po_week"].map(lambda d: calendar.fiscal_week(pd.Timestamp(d).date()))
    weekly["fiscal_week"] = fy.map(lambda t: f"FY{t[0]} W{t[1]:02d}")
    b.frames["weekly"] = weekly

    # ---- 3. booked forward lines --------------------------------------------------------
    booked = _booked_forward(orders, as_of=as_of, calendar=calendar, fwd_days=fwd_days, grace=grace)
    b.frames["booked_forward"] = booked.merge(
        attrs[["tcin", "sku", "description", "casepack"]], on="tcin", how="left"
    )

    # ---- 4. shipments view: replenishment forecast + booked forward by ship week ----
    rep = weekly[
        [
            "tcin",
            "sku",
            "description",
            "item_group",
            "ship_week",
            "expected_po_units",
            "expected_cases",
            "grade",
            "confidence",
            "stream",
            "casepack",
        ]
    ].copy()
    # planned_launch must be carried through by NAME: this is the view the warehouse
    # ships from, and a launch pipeline-fill shown as "forecast_replenishment" is a
    # materially wrong label on it. (Introduced and caught in the same session as
    # the stream split, 2026-09-08 — the previous map had no planned_launch entry because
    # the stream did not exist at the weekly grain.)
    rep["stream"] = (
        rep["stream"]
        .map(
            {
                plan_anchor.STREAM_CREATED: "created",
                plan_anchor.STREAM_PLANNED_FORWARD: "planned_forward",
                plan_anchor.STREAM_PLANNED_LAUNCH: "planned_launch",
            }
        )
        .fillna("forecast_replenishment")
    )
    rep = rep.rename(columns={"expected_po_units": "units", "expected_cases": "cases"})
    bk = b.frames["booked_forward"].copy()
    if not bk.empty:
        bk = bk.groupby(
            ["tcin", "sku", "description", "ship_week", "casepack"], as_index=False, dropna=False
        )["units"].sum()
        bk["cases"] = _cases(bk["units"], bk["casepack"])
        bk["stream"] = "booked_forward"
        bk["grade"] = "A"
        bk["confidence"] = "A · booked PO"
        bk = bk.merge(attrs[["tcin", "item_group"]], on="tcin", how="left")
    ship = pd.concat([rep, bk[rep.columns]] if not bk.empty else [rep], ignore_index=True)
    ship = ship[ship["ship_week"] >= pd.Timestamp(calendar.week_start(as_of))]
    b.frames["shipments"] = ship.sort_values(["ship_week", "tcin", "stream"]).reset_index(drop=True)

    # ---- 5. monthly consumption model -------------------------------------------------
    month_list = consumption.months_from(as_of, months)
    dfe_ratio = float(
        np.mean(
            [
                _cfg(cfg, "consumption", "dfe_pos_ratio", field_name="low", default=1.1),
                _cfg(cfg, "consumption", "dfe_pos_ratio", field_name="high", default=1.4),
            ]
        )
    )
    dfe_max_age = int(_cfg(cfg, "consumption", "dfe_max_age_days", default=21))
    receipt_lag = int(_cfg(cfg, "consumption", "receipt_lag_weeks", default=3))
    inv = (
        inv
        if inv is not None
        else pd.DataFrame(columns=["week_start_d", "week_end_d", "tcin", "on_hand"])
    )
    actuals = actuals if actuals is not None else pd.DataFrame(columns=["wk", "tcin", "act_rep"])

    # DFE is no longer a POS candidate (stale since 2026-07-27 and it failed the
    # admission gate on merit: WAPE 1.391, bias +98%). The call stays because the run
    # still reports the feed's freshness and raises DFE_STALE_OR_MISSING.
    _dfe_m, dfe_note = consumption.dfe_by_month(
        dfe, ratio=dfe_ratio, as_of=as_of, max_age_days=dfe_max_age
    )
    pos_blend, pos_notes = consumption.pos_forecast(
        sales,
        as_of=as_of,
        months=month_list,
        trailing_weeks=trailing,
        slope_weeks=int(_cfg(cfg, "consumption", "dist_slope_weeks", default=3)),
        ramp_cap=float(_cfg(cfg, "consumption", "dist_ramp_cap", default=1.15)),
        curated=launch_seed,
        min_obs=int(_cfg(cfg, "consumption", "season_min_obs", default=4)),
        min_years=int(_cfg(cfg, "consumption", "season_min_years", default=2)),
    )
    cand_notes = {"dfe": dfe_note}
    cand_scores = consumption.score_pos_candidates(
        sales,
        as_of=as_of,
        trailing_weeks=trailing,
        slope_weeks=int(_cfg(cfg, "consumption", "dist_slope_weeks", default=3)),
        ramp_cap=float(_cfg(cfg, "consumption", "dist_ramp_cap", default=1.15)),
    )
    # V-6: the POS forecast is now single-sourced, so a stalled weekly sales feed hits
    # the whole monthly view with no second opinion. Refuse to present it as current
    # beyond pos_max_age_days rather than publishing a silently stale month.
    pos_max_age = int(_cfg(cfg, "consumption", "pos_max_age_days", default=14))
    pos_last_week = (
        pd.to_datetime(sales["week_end_d"]).max().date() if not sales.empty else None
    )
    pos_age_days = (as_of - pos_last_week).days if pos_last_week else None
    pos_stale = pos_age_days is not None and pos_age_days > pos_max_age
    pos_notes["pos"]["last_week_end"] = str(pos_last_week) if pos_last_week else None
    pos_notes["pos"]["age_days"] = pos_age_days
    pos_notes["pos"]["max_age_days"] = pos_max_age
    pos_notes["pos"]["stale"] = bool(pos_stale)

    # ---- B&M store ramp: SHAPE the POS forecast before the simulation ------------------
    # The sheet's store plan as a ratio to its own anchor month, applied to BPD's anchor
    # store count; whichever of BPD's own projection and the ramped count claims more
    # doors wins. Level stays BPD's. Facts are never touched. See model/bm_combine.py.
    anchor = pd.Timestamp(consumption.month_start(pd.Timestamp(as_of)))
    bm_ramp_cap = float(_cfg(cfg, "bm_schedule", "ramp_cap", default=bc.RAMP_CAP))
    bm_material = float(_cfg(cfg, "bm_schedule", "material_pct", default=bc.RAMP_MATERIAL))
    bm_tol = float(_cfg(cfg, "bm_schedule", "load_tolerance", default=bc.LOAD_TOLERANCE))
    bm_stated_months = bool(
        _cfg(cfg, "bm_schedule", "include_stated_only_months", default=True)
    )
    # doors the sheet's anchor is read against: store locations holding inventory in the
    # latest week on or before as_of (weekly_inv_tcin_loc), DCs excluded. The ramp's
    # denominator is max(sheet anchor, this), so the sheet's understatement of the present
    # never becomes growth; the Store plan check sheet prints both.
    stocked: dict[int, float] = {}
    if not inv.empty and {"is_dc", "locations_with_inventory"}.issubset(inv.columns):
        st_inv = inv.loc[~inv["is_dc"].astype(bool)].copy()
        st_inv["week_end_d"] = pd.to_datetime(st_inv["week_end_d"])
        st_inv = st_inv.loc[st_inv["week_end_d"] <= pd.Timestamp(as_of)]
        if not st_inv.empty:
            latest = st_inv.loc[st_inv["week_end_d"] == st_inv["week_end_d"].max()]
            stocked = {
                int(r.tcin): float(r.locations_with_inventory)
                for r in latest.itertuples(index=False)
                if pd.notna(r.locations_with_inventory)
            }
    ramp_cols = [
        "tcin", "month_start", "bm_stores", "bm_stores_anchor", "bm_stores_anchor_used",
        "bm_upspw", "ramp", "ramp_flag",
    ]
    ramp = (
        bc.store_ramp(bm_frame, anchor, ramp_cap=bm_ramp_cap, bpd_stocked=stocked)
        if bm_frame is not None
        else pd.DataFrame(columns=ramp_cols)
    )
    pos_blend, shape_note = bc.shape_pos_forecast(pos_blend, ramp, anchor=anchor, material=bm_material)
    rr_tbl = consumption.runrate(sales, as_of, weeks=trailing)
    oh = consumption.chain_on_hand_latest(inv, as_of)
    cal_min = _cfg(cfg, "consumption", "calibration_min_week", default=None)
    hist = (
        consumption.history_panel(sales, inv, actuals, as_of=as_of, min_week=cal_min)
        if not sales.empty and not inv.empty
        else pd.DataFrame()
    )
    ctrl_default = (
        float(_cfg(cfg, "consumption", "controller", field_name="band", default=8)),
        float(_cfg(cfg, "consumption", "controller", field_name="k", default=12)),
    )
    if not hist.empty:
        controller, ctrl_grid = consumption.calibrate_controller(hist, default=ctrl_default)
    else:
        controller = consumption.Controller(
            ctrl_default[0], ctrl_default[1], np.nan, np.nan, 0, "default: no history panel"
        )
        ctrl_grid = pd.DataFrame()
    booked_receipts = (
        booked.groupby(["tcin", "receipt_week"], as_index=False)["units"]
        .sum()
        .rename(columns={"receipt_week": "week_start"})
        if not booked.empty
        else pd.DataFrame(columns=["tcin", "week_start", "units"])
    )
    weeks_ahead = int(months * 4.5) + 4
    sim = consumption.simulate(
        weekly_forecast=weekly,
        pos_month=pos_blend,
        runrate_tbl=rr_tbl,
        on_hand=oh,
        actuals=actuals,
        booked_receipts=booked_receipts,
        controller=controller,
        as_of=as_of,
        weeks_ahead=weeks_ahead,
        calendar=calendar,
        receipt_lag_weeks=receipt_lag,
    )
    offsets = {int(r.tcin): int(r.ship_offset_days) for r in attrs.itertuples()}
    monthly = consumption.monthly_from_simulation(
        sim,
        months=month_list,
        calendar=calendar,
        ship_offset_days=offsets,
        pos_blend=pos_blend,
        runrate_tbl=rr_tbl,
    )
    if monthly.empty:
        monthly = pd.DataFrame(
            columns=["tcin", "month_start", "expected_po_units", "expected_ship_units", "pos_units"]
        )
    monthly["stream"] = consumption.STREAM_REPLEN
    # Target's own plan spikes for launches / pipeline loads: a separate stream, never replenishment.
    pf_cols = ["tcin", "month_start", "planned_forward_ship_units", "planned_forward_po_units"]
    pf = (
        monthly[pf_cols].copy()
        if set(pf_cols).issubset(monthly.columns)
        else pd.DataFrame(columns=pf_cols)
    )
    pf = pf[(pf["planned_forward_ship_units"] > 0) | (pf["planned_forward_po_units"] > 0)]
    pf = pf.rename(
        columns={
            "planned_forward_ship_units": "expected_ship_units",
            "planned_forward_po_units": "expected_po_units",
        }
    )
    pf["stream"] = consumption.STREAM_PLANNED_FORWARD
    pf["grade"] = "C"
    # planned_launch: Target's own plan units for an item that has never sold, split out
    # of the weekly engine the same way planned_forward is. Before 2026-09-08 this stream
    # was the OWNER SHEET's Load_Orders row net of what Target had already booked, i.e.
    # the residual of a human forecast over Target's plan. It is now BPD-derived.
    pl_cols = ["tcin", "month_start", "planned_launch_ship_units", "planned_launch_po_units"]
    plw = (
        monthly[pl_cols].copy()
        if set(pl_cols).issubset(monthly.columns)
        else pd.DataFrame(columns=pl_cols)
    )
    plw = plw[(plw["planned_launch_ship_units"] > 0) | (plw["planned_launch_po_units"] > 0)]
    plw = plw.rename(
        columns={
            "planned_launch_ship_units": "expected_ship_units",
            "planned_launch_po_units": "expected_po_units",
        }
    )
    plw["stream"] = consumption.STREAM_PLANNED_LAUNCH
    plw["grade"] = "D"
    has_hist = (
        set(rr_tbl.loc[rr_tbl["pos_wk"] > 0, "tcin"].astype(int)) if not rr_tbl.empty else set()
    )
    # The retired `owner_only_pos` penalty is replaced by `pos_thin`: a POS row that is
    # thin on its own terms rather than one sourced from a spreadsheet. Four computable
    # conditions, all printed as columns — a curated-seed row, fewer selling weeks than
    # the trailing window, a forward store count that hit the ramp cap, or under 100
    # selling stores (the dispersion measured at that scale is 6x within one category).
    pos_flags = monthly["pos_flags"].fillna("") if "pos_flags" in monthly else pd.Series(
        "", index=monthly.index
    )
    thin_stores = (
        pd.to_numeric(monthly.get("stores_fwd"), errors="coerce") < 100
        if "stores_fwd" in monthly
        else pd.Series(False, index=monthly.index)
    ).fillna(False)
    pos_thin = (
        pos_flags.str.contains(consumption.POS_THIN)
        | pos_flags.str.contains(consumption.POS_DIST_CAPPED)
        | pos_flags.str.contains(consumption.POS_CURATED)
        | thin_stores
    )
    monthly["grade"] = [
        consumption.grade_month(
            int(mi), float(ps), pos_thin=bool(pt), has_history=int(t) in has_hist
        )
        for mi, ps, pt, t in zip(
            monthly["month_index"],
            monthly["plan_share"].fillna(0),
            pos_thin,
            monthly["tcin"],
            strict=True,
        )
    ]
    # A row the sheet's ramp moved rests partly on a stated plan: one grade worse, the
    # same penalty a thin POS row pays, and its provenance says so.
    if "authority" in monthly:
        shaped_rows = monthly["authority"].eq(bc.AUTH_SHAPED)
        monthly.loc[shaped_rows, "grade"] = monthly.loc[shaped_rows, "grade"].map(bc.one_worse)
    # booked forward by ship month
    if not booked.empty:
        bm = booked.copy()
        bm["month_start"] = consumption.month_start(bm["ship_week"])
        bm = bm.groupby(["tcin", "month_start"], as_index=False)["units"].sum()
        bm["expected_ship_units"] = bm["units"]
        bm["expected_po_units"] = 0.0
        bm["stream"] = consumption.STREAM_BOOKED
        bm["grade"] = "A"
        bm = bm[bm["month_start"].isin(month_list)].drop(columns="units")
    else:
        bm = pd.DataFrame(
            columns=[
                "tcin",
                "month_start",
                "expected_ship_units",
                "expected_po_units",
                "stream",
                "grade",
            ]
        )
    # CURATED launch loads, netted exactly as the retired owner-sheet `Load_Orders` row
    # was: a human's launch volume MINUS what Target has already booked or already
    # carries in its own plan that month, floored at zero. Source is
    # biom_admin.seed_target_launch_velocity (basis `load_orders`), not Drive. This is
    # the one quantity BPD cannot supply — a launch nobody has ordered yet — and it is
    # additive to `plw`, the plan-derived launch stream above, not a replacement for it.
    loads = consumption.curated_load_orders_by_month(launch_seed)
    pl_empty = pd.DataFrame(
        columns=[
            "tcin",
            "month_start",
            "expected_ship_units",
            "expected_po_units",
            "stream",
            "grade",
        ]
    )
    if not loads.empty:
        loads = loads[
            loads["month_start"].isin(month_list)
            & (loads["month_start"] >= consumption.month_start(pd.Timestamp(as_of)))
        ]
    if not loads.empty:
        covered = pd.concat(
            [
                bm[["tcin", "month_start", "expected_ship_units"]],
                pf[["tcin", "month_start", "expected_ship_units"]],
                plw[["tcin", "month_start", "expected_ship_units"]],
            ],
            ignore_index=True,
        )
        covered = (
            covered.groupby(["tcin", "month_start"], as_index=False)["expected_ship_units"]
            .sum()
            .rename(columns={"expected_ship_units": "booked"})
        )
        loads = loads.merge(covered, on=["tcin", "month_start"], how="left")
        loads["booked"] = loads["booked"].fillna(0.0)
        loads["expected_ship_units"] = (
            (loads["load_units"] - loads["booked"]).clip(lower=0).round(0)
        )
        loads = loads[loads["expected_ship_units"] > 0]
        loads["expected_po_units"] = loads["expected_ship_units"]
        loads["stream"] = consumption.STREAM_PLANNED_LAUNCH
        loads["grade"] = "E"  # curated assumption, never graded better than indicative
        pl = loads[list(pl_empty.columns)] if not loads.empty else pl_empty
    else:
        pl = pl_empty
    monthly_all = pd.concat([monthly, pf, plw, bm, pl], ignore_index=True, sort=False)
    # Provenance. Fact streams (booked, planned forward, planned launch, curated) are
    # measured by construction; only a replenishment row the ramp moved is "shaped".
    if "authority" not in monthly_all:
        monthly_all["authority"] = bc.AUTH_MEASURED
    monthly_all["authority"] = monthly_all["authority"].where(
        monthly_all["authority"].notna(), bc.AUTH_MEASURED
    )
    monthly_all.loc[monthly_all["stream"] != consumption.STREAM_REPLEN, "authority"] = bc.AUTH_MEASURED
    # Months only the sheet describes -- beyond the engine's reach, or an item with no BPD
    # row at all -- are carried as stated_only at grade E, so the planner sees the sheet's
    # full reach and exactly how much of it rests on nothing measured.
    stated = pd.DataFrame()
    if bm_frame is not None and bm_fc is not None:
        covered = {
            (int(t), pd.Timestamp(m))
            for t, m in zip(monthly_all["tcin"], pd.to_datetime(monthly_all["month_start"]), strict=True)
        }
        stated_months = (
            [pd.Timestamp(m) for m in bm_fc.months if pd.Timestamp(m) >= anchor]
            if bm_stated_months
            else list(month_list)
        )
        stated = bc.bm_only_rows(bm_frame, covered, months=stated_months)
    if not stated.empty:
        st = stated.rename(columns={"units": "expected_ship_units", "ramp": "bm_ramp"})
        st["expected_po_units"] = st["expected_ship_units"]
        st["pos_units"] = st["expected_ship_units"]  # the sheet's Velocity is its POS
        st["stores_fwd"] = st["bm_stores"]
        st["stores_source"] = bc.STORES_FROM_PLAN
        st["pos_candidates"] = "bm_sheet_velocity"
        st = st.merge(
            bm_frame.loc[bm_frame["tcin"].notna(), ["tcin", "month_start", "bm_upspw"]]
            .assign(tcin=lambda d: d["tcin"].astype("int64"), month_start=lambda d: pd.to_datetime(d["month_start"]))
            .rename(columns={"bm_upspw": "upspw"}),
            on=["tcin", "month_start"],
            how="left",
        )
        monthly_all = pd.concat([monthly_all, st.drop(columns=["units_bpd", "bm_stores_anchor"])], ignore_index=True, sort=False)
    for c in (
        "expected_ship_units",
        "expected_po_units",
        "pos_units",
        "drawdown_release",
        "on_hand_start",
        "on_hand_end",
    ):
        if c in monthly_all:
            monthly_all[c] = pd.to_numeric(monthly_all[c], errors="coerce").round(0)
    monthly_all = grading.attach_confidence(monthly_all, labels, value_col="expected_ship_units")
    monthly_all = monthly_all.merge(
        attrs[["tcin", "sku", "description", "item_group", "casepack", "item_state"]],
        on="tcin",
        how="left",
    )
    monthly_all["month"] = pd.to_datetime(monthly_all["month_start"]).dt.strftime("%b-%y")
    flags = []
    for r in monthly_all.itertuples(index=False):
        f = []
        if getattr(r, "authority", None) == bc.AUTH_STATED:
            # sheet only: nothing measured behind it, so none of the measured-row flags apply
            flags.append(bc.FLAG_STATED)
            continue
        if r.stream == consumption.STREAM_PLANNED_LAUNCH:
            f.append("PLANNED_LAUNCH")
        if r.stream == "planned_forward":
            f.append("PLANNED_FORWARD")
        if str(state_map.get(int(r.tcin), "")).upper() == "READY_FOR_ORDER":
            f.append("LAUNCH_FILL")
        if r.stream == consumption.STREAM_REPLEN and int(r.tcin) not in has_hist:
            f.append("NEW_TCIN_NO_HISTORY")
        if getattr(r, "step_up_month", "") == "STEP_UP":
            f.append("STEP_UP")
        pf_flags = getattr(r, "pos_flags", None)
        if isinstance(pf_flags, str) and pf_flags:
            f.extend(pf_flags.split("|"))
        if (
            r.stream == consumption.STREAM_REPLEN
            and not isinstance(getattr(r, "pos_candidates", None), str)
            and int(r.tcin) not in has_hist
        ):
            f.append(consumption.POS_NO_HISTORY)
        flags.append("|".join(dict.fromkeys(f)))
    monthly_all["flags"] = flags
    monthly_all["source_mix"] = np.where(
        monthly_all["stream"] == consumption.STREAM_REPLEN,
        "plan "
        + (monthly_all["plan_share"].fillna(0) * 100).round(0).astype(int).astype(str)
        + "% / controller "
        + ((1 - monthly_all["plan_share"].fillna(0)) * 100).round(0).astype(int).astype(str)
        + "%",
        monthly_all["stream"],
    )
    monthly_all.loc[monthly_all["authority"] == bc.AUTH_STATED, "source_mix"] = "B&M sheet only"
    b.frames["monthly"] = monthly_all.sort_values(["tcin", "month_start", "stream"]).reset_index(
        drop=True
    )
    b.frames["weekly_path"] = sim

    # ---- 5b. the sheet's three checks: doors, loads, coverage --------------------------
    bm_store = pd.DataFrame()
    bm_loads = pd.DataFrame()
    if bm_frame is not None and bm_fc is not None:
        bm_store = bc.store_check(
            bm_frame, ramp, anchor, pos_blend, sku_for=attr_sku, stocked_stores=stocked
        )
        facts = monthly_all.loc[
            (monthly_all["stream"] != consumption.STREAM_REPLEN)
            & (monthly_all["authority"] != bc.AUTH_STATED),
            ["tcin", "month_start", "expected_ship_units"],
        ]
        bpd_launch = (
            facts.groupby(["tcin", "month_start"], as_index=False)["expected_ship_units"]
            .sum()
            .rename(columns={"expected_ship_units": "bpd_units"})
        )
        bm_loads = bc.load_order_verdicts(bm_frame, bpd_launch, months=list(month_list), tolerance=bm_tol)
        b.frames["bm_store_check"] = bm_store
        b.frames["bm_load_check"] = bm_loads
        b.frames["bm_coverage"] = bc.coverage(
            bm_frame,
            bm_fc.unresolved,
            item_tcins=item_master.tcins,
            run_tcins=set(monthly_all["tcin"].astype(int)),
            item_state=state_map,
            sku_for=attr_sku,
        )

    # ---- 6. accuracy --------------------------------------------------------------------
    if not long_rows.empty:
        lr = long_rows.copy()
        lr["lead_bucket"] = intervals.lead_bucket(lr["lead_days"])
        plan_rows = lr[lr["signal"] == "plan_sat_ordered"].copy()
        plan_rows["lead_days_int"] = plan_rows["lead_days"].fillna(-1).astype(int)
        by_lead = scoring.score_table(
            plan_rows[plan_rows["horizon"] == 1],
            actual_col="actual",
            forecast_col="forecast",
            by=["lead_days_int"],
        ).rename(columns={"lead_days_int": "lead_days"})
        b.frames["accuracy_plan_by_lead"] = by_lead
        sig = scoring.score_table(
            lr[lr["horizon"] == 1], actual_col="actual", forecast_col="forecast", by=["signal"]
        ).sort_values("wape")
        # biom_sql fix (c): the fallback used to be 500 while config said 1,000, so a
        # reader could not tell which bootstrap produced the printed interval. Config is
        # the only source now, and the draw count is stamped on the frame and the README.
        boot_draws = int(_cfg(cfg, "gate", "bootstrap", field_name="n_boot", default=1000))
        cis = []
        for s, g in lr[lr["horizon"] == 1].groupby("signal"):
            _pt, lo, hi = scoring.week_block_bootstrap_ci(
                g,
                week_col="week",
                actual_col="actual",
                forecast_col="forecast",
                n_boot=boot_draws,
            )
            cis.append({"signal": s, "wape_lo80": lo, "wape_hi80": hi})
        sig = sig.merge(pd.DataFrame(cis), on="signal", how="left")
        naive = sig.loc[sig["signal"] == "mean4_rep", "wape"]
        naive_w = float(naive.iloc[0]) if len(naive) else np.nan
        max_bias = float(_cfg(cfg, "gate", "max_abs_bias", default=0.15))
        sig["admitted_weekly"] = (sig["wape"] < naive_w) & (sig["bias"].abs() < max_bias)
        sig["bootstrap_draws"] = boot_draws
        b.frames["accuracy_signals_h1"] = sig
        b.frames["accuracy_horizon"] = scoring.score_table(
            plan_rows, actual_col="actual", forecast_col="forecast", by=["horizon"]
        )
        # p0 statistic: plan said 0, history positive
        assert panel is not None
        p = panel.copy()
        if {"plan_sat_ordered_W", "mean4_rep", "act_rep"}.issubset(p.columns):
            z = p[
                (pd.to_numeric(p["plan_sat_ordered_W"], errors="coerce").fillna(0) == 0)
                & (pd.to_numeric(p["mean4_rep"], errors="coerce").fillna(0) > 0)
            ]
            b.readme["p0_plan_zero_history_positive"] = (
                f"{int((z['act_rep'] > 0).sum())} of {len(z)} TCIN-weeks with plan 0 and recent POs saw a PO anyway ({z.loc[z['act_rep'] > 0, 'act_rep'].sum():,.0f} units)"
            )
    if not fitted.empty:
        b.frames["accuracy_bands"] = fitted.merge(
            coverage, on="bucket", how="left", suffixes=("", "_lowo")
        )
    if not cand_scores.empty:
        b.frames["accuracy_pos_candidates"] = cand_scores
    season_diag = pos_notes.get("season_diag")
    if season_diag is not None and not season_diag.empty:
        b.frames["accuracy_season_index"] = season_diag
    ctrl_tbl = pd.DataFrame(
        [
            {"parameter": "wos_band_weeks", "value": controller.band},
            {"parameter": "adjust_weeks_k", "value": controller.k},
            {"parameter": "wape_weekly_orders", "value": controller.wape},
            {"parameter": "bias", "value": controller.bias},
            {"parameter": "n_tcin_weeks", "value": controller.n},
            {"parameter": "basis", "value": controller.basis},
        ]
    )
    b.frames["accuracy_controller"] = ctrl_tbl
    if not ctrl_grid.empty:
        b.frames["accuracy_controller_grid"] = ctrl_grid.head(15)

    # ---- 7. exceptions -----------------------------------------------------------------
    ex: list[dict[str, Any]] = []
    for r in item_master.unmapped().itertuples(index=False):
        ex.append(
            {
                "tcin": int(r.tcin),
                "sku": getattr(r, "biom_sku", None),
                "issue": "UNMAPPED_ITEM",
                "detail": getattr(r, "notes", "") or getattr(r, "mapping_rule", ""),
            }
        )
    known = set(item_master.tcins)
    for t in sorted(set(weekly["tcin"].astype(int)) - known):
        ex.append(
            {
                "tcin": t,
                "sku": None,
                "issue": "TCIN_NOT_IN_ITEM_MASTER",
                "detail": "appears in plan or orders; add to data/item_master_target.csv",
            }
        )
    # One row per (item, flag set), not one per week: sixteen identical rows for an item
    # with no history said nothing sixteen times.
    wk_flagged = weekly[
        weekly["flags"].str.contains(
            "NEW_TCIN_NO_HISTORY|PLANNED_FORWARD|STALE_PLAN|NO_PO_8WK", regex=True
        )
    ]
    for (t, fl), g in wk_flagged.groupby(["tcin", "flags"], sort=True):
        wks = pd.to_datetime(g["po_week"])
        leads = pd.to_numeric(g["lead_days"], errors="coerce").dropna()
        lead_txt = (
            f"lead {int(leads.min())}-{int(leads.max())} d" if not leads.empty else "lead n/a"
        )
        ex.append(
            {
                "tcin": int(t),
                "sku": g["sku"].iloc[0],
                "issue": str(fl),
                "detail": (
                    f"{len(g)} PO week(s) {wks.min().date()}..{wks.max().date()}, expected "
                    f"{float(g['expected_po_units'].sum()):,.0f} units in total, {lead_txt}"
                ),
            }
        )
    if cand_notes["dfe"].get("used") is False:
        ex.append(
            {
                "tcin": None,
                "sku": None,
                "issue": "DFE_STALE_OR_MISSING",
                "detail": f"{cand_notes['dfe']}",
            }
        )
    # ---- POS forecast coverage: never a silent zero -------------------------------
    # A TCIN with no productive selling history gets NO pos row from dist_velocity (see
    # consumption.dist_velocity). That is reported here per TCIN, once, with whatever
    # BPD does know about the launch, so a reader can tell "we cannot forecast this yet"
    # from "we forecast zero". This is the category the retired owner sheet used to
    # paper over with a 48-month number nobody could score.
    forecast_tcins = (
        set(pos_blend["tcin"].astype(int)) if pos_blend is not None and not pos_blend.empty else set()
    )
    curated_tcins = set(pos_notes["pos"].get("tcins_from_curated_seed") or [])
    plan_units_by_tcin = (
        weekly.groupby("tcin")["expected_po_units"].sum() if not weekly.empty else pd.Series(dtype=float)
    )
    # A never-launched item and a drawdown item both fail dist_velocity, for opposite
    # reasons, and conflating them is exactly what the design doc warned against: one
    # needs a launch assumption, the other needs delisting. Told apart on whether the
    # TCIN has EVER sold in the pulled window.
    ever_sold = (
        set(sales.loc[pd.to_numeric(sales["units"], errors="coerce").fillna(0) > 0, "tcin"].astype(int))
        if not sales.empty
        else set()
    )
    for t in sorted(set(item_master.tcins) - forecast_tcins):
        planned = float(plan_units_by_tcin.get(t, 0.0) or 0.0)
        st = str(state_map.get(int(t), "")) or "unknown"
        if int(t) in ever_sold:
            ex.append(
                {
                    "tcin": int(t),
                    "sku": attr_sku.get(int(t)),
                    "issue": consumption.POS_NO_STORES,
                    "detail": (
                        f"has POS history but NO selling store in the trailing {trailing} "
                        "weeks: a drawdown or a delist, not a new item. dist_velocity emits "
                        f"NO monthly velocity (not zero). item_state {st}; Target plan "
                        f"carries {planned:,.0f} units in the horizon"
                    ),
                }
            )
            continue
        ex.append(
            {
                "tcin": int(t),
                "sku": attr_sku.get(int(t)),
                "issue": consumption.POS_NO_HISTORY,
                "detail": (
                    f"never sold anywhere in the pulled window and no selling stores: "
                    f"dist_velocity emits NO monthly velocity (not zero). item_state {st}; "
                    f"Target plan carries {planned:,.0f} units in the horizon. Add a "
                    "curated assumption to biom_admin.seed_target_launch_velocity if a "
                    "number is needed"
                ),
            }
        )
    for t in sorted(curated_tcins):
        ex.append(
            {
                "tcin": int(t),
                "sku": attr_sku.get(int(t)),
                "issue": consumption.POS_CURATED,
                "detail": (
                    "monthly velocity came from biom_admin.seed_target_launch_velocity "
                    "(curated human assumption), not from BPD; graded E"
                ),
            }
        )
    if not pos_blend.empty and "flags" in pos_blend:
        capped = pos_blend[pos_blend["flags"].fillna("").str.contains(consumption.POS_DIST_CAPPED)]
        for t in sorted(set(capped["tcin"].astype(int))):
            ex.append(
                {
                    "tcin": int(t),
                    "sku": attr_sku.get(int(t)),
                    "issue": consumption.POS_DIST_CAPPED,
                    "detail": (
                        "projected store count hit the ramp cap "
                        f"({float(_cfg(cfg, 'consumption', 'dist_ramp_cap', default=1.15))}x its own "
                        "observed peak); velocity beyond that month is held flat"
                    ),
                }
            )
        thin = pos_blend[pos_blend["flags"].fillna("").str.contains(consumption.POS_THIN)]
        for t in sorted(set(thin["tcin"].astype(int))):
            ex.append(
                {
                    "tcin": int(t),
                    "sku": attr_sku.get(int(t)),
                    "issue": consumption.POS_THIN,
                    "detail": f"fewer than {trailing} complete selling weeks; velocity graded one worse",
                }
            )
    # The seasonal index prints its own refusal. Today every month is unsupported, which
    # is a measurement about BPD's history depth, not a defect — see the design doc.
    sd = pos_notes.get("season_diag")
    unsupported = sorted(set(range(1, 13)) - set(pos_notes["pos"].get("season_applied_months") or []))
    if unsupported:
        have = (
            {int(r.month_of_year): (int(r.n), int(r.n_years)) for r in sd.itertuples(index=False)}
            if sd is not None and not sd.empty
            else {}
        )
        ex.append(
            {
                "tcin": None,
                "sku": None,
                "issue": "SEASON_INDEX_UNSUPPORTED",
                "detail": (
                    f"month-of-year factor held at 1.000 for months {unsupported}: "
                    f"{pos_notes['pos'].get('season_basis')}. observed (n, n_years) per month: "
                    f"{have or 'none estimable'}"
                ),
            }
        )
    if pos_stale:
        ex.append(
            {
                "tcin": None,
                "sku": None,
                "issue": "POS_FEED_STALE",
                "detail": (
                    f"weekly POS last week-end {pos_last_week} is {pos_age_days} d before "
                    f"as_of, over pos_max_age_days {pos_max_age}. The monthly view is "
                    "single-sourced on this feed — treat every monthly number as stale"
                ),
            }
        )
    for d in item_state_meta.get("diverged", []):
        ex.append(
            {
                "tcin": int(d["tcin"]),
                "sku": None,
                "issue": "ITEM_STATE_DIVERGED",
                "detail": (
                    f"live wkly_tcin_item says {d['live']}, item_master CSV says {d['csv']}; "
                    "live wins. LAUNCH_FILL / planned_forward follow the live value — "
                    "update data/item_master_target.csv"
                ),
            }
        )
    for t in item_state_meta.get("not_in_live_feed", []):
        ex.append(
            {
                "tcin": int(t),
                "sku": None,
                "issue": "ITEM_STATE_NOT_IN_LIVE_FEED",
                "detail": "not in bpd_raw.wkly_tcin_item; item_state fell back to the committed CSV",
            }
        )
    if item_state_meta.get("stale") or not item_state_meta.get("used_live"):
        ex.append(
            {
                "tcin": None,
                "sku": None,
                "issue": "ITEM_STATE_FEED_NOT_USED",
                "detail": f"{item_state_meta}",
            }
        )
    # ---- the B&M sheet: absent, or what it disagrees with -------------------------------
    if bm_frame is None or bm_fc is None:
        ex.append(
            {
                "tcin": None,
                "sku": None,
                "issue": "BM_SCHEDULE_NOT_AVAILABLE",
                "detail": (
                    f"{bm_meta.get('reason') or 'no B&M Target Schedule for this run'}. The "
                    "forward store count is BPD's own projection only (flat at each item's "
                    "peak beyond its measured ramp) and no stated_only months are shown"
                ),
            }
        )
    else:
        for w in bm_fc.warnings:
            issue, _, detail = str(w).partition(":")
            ex.append({"tcin": None, "sku": None, "issue": issue.strip(), "detail": detail.strip()})
        for u in bm_fc.unresolved:
            ex.append(
                {
                    "tcin": None,
                    "sku": u.get("bm_sku"),
                    "issue": "BM_SKU_UNRESOLVED",
                    "detail": (
                        f"{u.get('reason')}; '{u.get('description') or ''}' (key {u.get('unique_key')}). "
                        "Its months are not shaped and not shown; add the SKU to "
                        "data/item_master_target.csv or data/sku_aliases_target.tsv"
                    ),
                }
            )
        if not bm_store.empty:
            for r in bm_store.itertuples(index=False):
                # BM_RAMP_ANCHOR_FROM_BPD alone is the normal state for a mature item (the
                # sheet sits a few percent under BPD's stocked count) and is printed on the
                # Store plan check sheet; only a REFUSAL is an exception here.
                refusal = "|".join(
                    f for f in str(r.ramp_flag or "").split("|") if f and f != "BM_RAMP_ANCHOR_FROM_BPD"
                )
                if refusal:
                    ex.append(
                        {
                            "tcin": int(r.tcin),
                            "sku": r.sku,
                            "issue": refusal,
                            "detail": (
                                f"sheet stores at anchor {float(r.bm_stores_anchor or 0):,.0f}, BPD "
                                f"stocked {r.bpd_stocked_stores}, denominator used "
                                f"{float(r.bm_stores_anchor_used or 0):,.0f}, plan peak "
                                f"{float(r.bm_stores_plan_peak or 0):,.0f} -> ramp to peak "
                                f"{r.bm_ramp_to_peak}. A refused ramp is held at 1.0 (BPD projection only)"
                            ),
                        }
                    )
                gap = r.stores_gap_pct
                if gap is not None and gap == gap and abs(float(gap)) > 0.10 and not refusal:
                    basis = (
                        f"{float(r.bpd_stocked_stores):,.0f} stocked stores"
                        if r.bpd_stocked_stores == r.bpd_stocked_stores
                        else f"{float(r.bpd_selling_stores):,.0f} selling stores"
                    )
                    ex.append(
                        {
                            "tcin": int(r.tcin),
                            "sku": r.sku,
                            "issue": "BM_STORES_DISAGREE",
                            "detail": (
                                f"sheet says {float(r.bm_stores_anchor):,.0f} doors this month, BPD "
                                f"measures {basis} ({float(gap):+.1%}). The ramp to the sheet's plan "
                                f"peak {float(r.bm_stores_plan_peak or 0):,.0f} is read from "
                                f"{float(r.bm_stores_anchor_used or 0):,.0f} (x{r.bm_ramp_to_peak}), "
                                "not from the sheet's own anchor"
                            ),
                        }
                    )
        if not bm_loads.empty:
            for r in bm_loads.loc[bm_loads["verdict"] != "BM_LOAD_AGREES"].itertuples(index=False):
                ex.append(
                    {
                        "tcin": int(r.tcin),
                        "sku": attr_sku.get(int(r.tcin), r.bm_sku),
                        "issue": str(r.verdict),
                        "detail": (
                            f"{r.note}. Sheet Load_Orders {float(r.bm_load_units):,.0f} "
                            f"({r.bm_months or '-'}) vs engine launch/forward "
                            f"{float(r.bpd_load_units):,.0f} ({r.bpd_months or '-'}); cross-check "
                            "only, no unit moved"
                        ),
                    }
                )
        sheet_tcins = {int(t) for t in bm_frame.loc[bm_frame["tcin"].notna(), "tcin"]}
        for t in sorted(set(item_master.tcins) - sheet_tcins):
            ex.append(
                {
                    "tcin": int(t),
                    "sku": attr_sku.get(int(t)),
                    "issue": "BM_SHEET_NO_BLOCK",
                    "detail": "in the item master but not in the B&M Target Schedule; BPD only, no ramp",
                }
            )
    b.frames["exceptions"] = pd.DataFrame(ex, columns=["tcin", "sku", "issue", "detail"])
    b.frames["legend"] = grading.legend(labels)

    # ---- 8. readme -------------------------------------------------------------------
    fresh_bd, plan_end = plan_anchor.plan_horizon(
        plan if plan is not None else pd.DataFrame(), as_of
    )
    b.readme.update(
        {
            "as_of": as_of.isoformat(),
            "run_week": calendar.week_start(as_of).isoformat(),
            "plan_snapshot_used": fresh_bd.isoformat() if fresh_bd else "none",
            "plan_horizon_last_order_day": plan_end.isoformat() if plan_end else "none",
            "orders_latest_snapshot": str(pd.to_datetime(orders["snapshot_d"]).max().date())
            if orders is not None and not orders.empty
            else "none",
            "sales_last_week_end": str(pd.to_datetime(sales["week_end_d"]).max().date())
            if not sales.empty
            else "none",
            "inventory_last_week_end": str(pd.to_datetime(inv["week_end_d"]).max().date())
            if not inv.empty
            else "none",
            "dfe": cand_notes["dfe"],
            "pos_forecast": pos_notes["pos"],
            "pos_forecast_estimator": (
                "dist_velocity = units-per-selling-store-per-week x projected selling stores "
                "x days/7. Single estimator, no blend: errors correlate 0.686 with runrate_8wk "
                "so 1/WAPE^2 weighting scored WORSE than dist_velocity alone (0.2035 vs 0.1998). "
                "runrate_8wk is still scored on the Accuracy sheet as the reference. "
                "Measured 2026-09-08: dist_velocity WAPE 0.1998 / bias +5.8% vs runrate_8wk "
                "0.2962 / -9.4% and the retired owner_velocity 0.3422 / -4.1%"
            ),
            "season_index": (
                f"applied to months {pos_notes['pos'].get('season_applied_months')}; "
                f"{pos_notes['pos'].get('season_basis')}"
            ),
            "controller": f"band {controller.band} weeks, k {controller.k} weeks; {controller.basis}",
            "receipt_lag_weeks": receipt_lag,
            "horizon": f"{horizon_weeks} PO weeks; {months} months",
            "grades": "A/B/C weekly by plan lead days (measured); monthly B when >=75% plan-covered, C model to 6 months, D beyond 6, E beyond 12 months; one grade worse when the POS row is thin (few selling weeks, store count capped, curated, or under 100 selling stores) and one grade worse when the B&M store ramp shaped it; E always for a month only the sheet describes (stated_only)",
            "bands": "weekly: leave-one-week-out P10/P90 of log ratio from the backtest panel where fitted; otherwise symmetric ±band from the grade legend",
            "not_forecast": "launch volume Target has not yet planned, unless a curated assumption exists in biom_admin.seed_target_launch_velocity; monthly velocity for a TCIN with no selling history (reported as NEW_TCIN_NO_POS_HISTORY, never as zero); item-level weekly timing beyond one week; realised shipments before ASN reconciliation",
            "inputs": (
                "BigQuery only in the scheduled path. No Google Drive call and no manually "
                "placed file anywhere in check|pull|run: curated human assumptions come "
                "from biom_admin.seed_target_launch_velocity, and the channel owner's "
                "Brick & Mortar Master Forecast (Target Schedule) comes from "
                "biom_admin.bm_target_schedule_snapshot, landed by "
                "ingest/bm_schedule_ingest.py, the one job with a Drive grant, and read "
                "as the newest snapshot on or before as_of. `run --bm PATH` parses a local "
                "copy instead, for a hand-run only. The RDZ supply sheet was removed "
                "2026-09-08"
            ),
            "bm_schedule": {
                **bm_meta,
                "shaping": shape_note,
                "ramp_cap": bm_ramp_cap,
                "material_pct": bm_material,
                "load_tolerance": bm_tol,
                "stated_only_months_shown": bool(bm_stated_months),
                "stated_only_rows": int(len(stated)),
                "stated_only_units": float(stated["units"].sum()) if not stated.empty else 0.0,
            },
            "store_ramp": (
                "ramp(t, M) = B&M stores(t, M) / max(B&M stores(t, anchor month), BPD stocked "
                "stores(t) today), a RATIO never a level, so the sheet's understatement of the "
                "present never becomes growth; projected selling stores(t, M) = max(BPD's own "
                "projection, BPD anchor stores x ramp); POS = UPSPW x stores x days/7 x season. "
                "Applied BEFORE the "
                "weekly simulation so replenishment follows the ramp with the inventory lag. "
                "Facts (booked forward, planned forward, planned launch, curated loads) are "
                "never scaled. Refused (ramp 1.0) when the sheet has no anchor-month stores, "
                "is a placeholder block, or exceeds ramp_cap. Rows the ramp moved carry "
                "provenance measured_shaped_by_plan and one grade worse; months only the sheet "
                "describes carry stated_only at grade E and the flag BM_ONLY_NO_BPD_SIGNAL. "
                "Load_Orders is a cross-check (Load order check sheet), never a source"
            ),
            "supply_layer": (
                "not applied. Biom-side ability-to-ship was never enforced in v1 (the RDZ "
                "read only produced an Exceptions row) and its modules were removed with "
                "the sheet. No forecast cell is capped by Biom supply"
            ),
            "item_state": item_state_meta,
            "bootstrap_draws": int(
                _cfg(cfg, "gate", "bootstrap", field_name="n_boot", default=1000)
            ),
            "po_units_vs_shipments": PO_VS_SHIP_NOTE,
            "exceptions_rows": int(len(b.frames["exceptions"])),
        }
    )
    return b


__all__ = ["ForecastBundle", "run_forecast"]
