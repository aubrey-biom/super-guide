"""End-to-end forecast assembly: pulled inputs -> frames for the workbook.

`run_forecast` is pure over its inputs (no BigQuery, no files) so it can be
tested on the committed fixtures and re-run on any pulled snapshot. It returns a
`ForecastBundle` whose frames the report renderer lays out according to
`config/report_target.yaml`.

Frames produced (keys used by the report spec):

    weekly          TCIN x PO week: expected PO units, grade, band, stream, flags
    shipments       TCIN x ship week (replenishment forecast + booked forward)
    monthly         TCIN x month x stream: expected shipments for S&OP with grade
    accuracy_*      backtest tables that justify the grades and weights
    exceptions      every item that needs a human eye
    legend          grade -> confidence -> expected error band
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from shipcast.backtest import scoring
from shipcast.backtest.rolling import panel_to_long
from shipcast.channels.target import forward
from shipcast.channels.target.calendar import TargetCalendar, sunday_week
from shipcast.inputs.item_master import ItemMaster
from shipcast.model import consumption, intervals, plan_anchor
from shipcast.model import grade as grading


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
    `dfe_asof`, optional `owner` (parsed owner rows), `owner_meta`,
    `on_hand`/`inbound` (RDZ, used for exceptions only in v1).
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
    owner = inputs.get("owner")
    attrs = _item_attrs(item_master, calendar)
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
    state_map = {
        int(r.tcin): r.item_state for r in attrs.itertuples() if isinstance(r.item_state, str)
    }
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
    rep["stream"] = (
        rep["stream"]
        .map(
            {
                plan_anchor.STREAM_CREATED: "created",
                plan_anchor.STREAM_PLANNED_FORWARD: "planned_forward",
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

    cands, cand_notes = consumption.pos_forecast_candidates(
        sales,
        owner,
        dfe,
        as_of=as_of,
        months=month_list,
        trailing_weeks=trailing,
        dfe_ratio=dfe_ratio,
        dfe_max_age_days=dfe_max_age,
    )
    cand_scores = consumption.score_pos_candidates(
        sales, owner, as_of=as_of, trailing_weeks=trailing
    )
    weights = (
        dict(zip(cand_scores["candidate"], cand_scores["weight"], strict=True))
        if not cand_scores.empty
        else dict(consumption.DEFAULT_WEIGHTS)
    )
    if cand_notes["dfe"].get("used"):
        weights.setdefault(consumption.CAND_DFE, 0.2)
    pos_blend = consumption.blend_pos_forecast(cands, weights)
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
    pf["stream"] = "planned_forward"
    pf["grade"] = "C"
    has_hist = (
        set(rr_tbl.loc[rr_tbl["pos_wk"] > 0, "tcin"].astype(int)) if not rr_tbl.empty else set()
    )
    owner_only = monthly["pos_candidates"].fillna("").str.contains(
        consumption.CAND_OWNER
    ) & ~monthly["pos_candidates"].fillna("").str.contains(consumption.CAND_RUNRATE)
    monthly["grade"] = [
        consumption.grade_month(
            int(mi), float(ps), owner_only_pos=bool(oo), has_history=int(t) in has_hist
        )
        for mi, ps, oo, t in zip(
            monthly["month_index"],
            monthly["plan_share"].fillna(0),
            owner_only,
            monthly["tcin"],
            strict=True,
        )
    ]
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
    # planned launch from owner load orders, net of booked forward in the same month
    loads = consumption.owner_load_orders_by_month(owner)
    if not loads.empty:
        loads = loads[
            loads["month_start"].isin(month_list)
            & (loads["month_start"] >= consumption.month_start(pd.Timestamp(as_of)))
        ]
        covered = pd.concat(
            [
                bm[["tcin", "month_start", "expected_ship_units"]],
                pf[["tcin", "month_start", "expected_ship_units"]],
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
        # owner load orders net of what Target already booked or already carries in its plan that month
        loads["expected_ship_units"] = (
            (loads["load_units"] - loads["booked"]).clip(lower=0).round(0)
        )
        loads = loads[loads["expected_ship_units"] > 0]
        loads["expected_po_units"] = loads["expected_ship_units"]
        loads["stream"] = consumption.STREAM_PLANNED_LAUNCH
        loads["grade"] = "D"
        pl = loads[
            ["tcin", "month_start", "expected_ship_units", "expected_po_units", "stream", "grade"]
        ]
    else:
        pl = pd.DataFrame(
            columns=[
                "tcin",
                "month_start",
                "expected_ship_units",
                "expected_po_units",
                "stream",
                "grade",
            ]
        )
    monthly_all = pd.concat([monthly, pf, bm, pl], ignore_index=True, sort=False)
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
        if r.stream == consumption.STREAM_PLANNED_LAUNCH:
            f.append("PLANNED_LAUNCH")
        if r.stream == "planned_forward":
            f.append("PLANNED_FORWARD")
        if str(getattr(r, "item_state", "")).upper() == "READY_FOR_ORDER":
            f.append("LAUNCH_FILL")
        if r.stream == consumption.STREAM_REPLEN and int(r.tcin) not in has_hist:
            f.append("NEW_TCIN_NO_HISTORY")
        if getattr(r, "step_up_month", "") == "STEP_UP":
            f.append("STEP_UP")
        if (
            isinstance(getattr(r, "pos_candidates", None), str)
            and consumption.CAND_RUNRATE not in r.pos_candidates
            and r.stream == consumption.STREAM_REPLEN
        ):
            f.append("OWNER_ONLY_POS")
        flags.append("|".join(f))
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
    b.frames["monthly"] = monthly_all.sort_values(["tcin", "month_start", "stream"]).reset_index(
        drop=True
    )
    b.frames["weekly_path"] = sim

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
        cis = []
        for s, g in lr[lr["horizon"] == 1].groupby("signal"):
            _pt, lo, hi = scoring.week_block_bootstrap_ci(
                g,
                week_col="week",
                actual_col="actual",
                forecast_col="forecast",
                n_boot=int(_cfg(cfg, "gate", "bootstrap", field_name="n_boot", default=1000)),
            )
            cis.append({"signal": s, "wape_lo80": lo, "wape_hi80": hi})
        sig = sig.merge(pd.DataFrame(cis), on="signal", how="left")
        naive = sig.loc[sig["signal"] == "mean4_rep", "wape"]
        naive_w = float(naive.iloc[0]) if len(naive) else np.nan
        max_bias = float(_cfg(cfg, "gate", "max_abs_bias", default=0.15))
        sig["admitted_weekly"] = (sig["wape"] < naive_w) & (sig["bias"].abs() < max_bias)
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
    flagged = weekly[
        weekly["flags"].str.contains(
            "NEW_TCIN_NO_HISTORY|PLANNED_FORWARD|STALE_PLAN|NO_PO_8WK", regex=True
        )
    ]
    # One row per item and flag set, not one per PO week: over a 16-week horizon the
    # per-week form repeated the same item up to 16 times (59 rows for 13 items on the
    # 4 Sep sample) and buried the items that actually need a human eye.
    for (t, sku, flags), g in flagged.groupby(["tcin", "sku", "flags"], dropna=False, sort=True):
        weeks = pd.to_datetime(g["po_week"])
        ex.append(
            {
                "tcin": int(t),
                "sku": sku,
                "issue": flags,
                "detail": (
                    f"{len(g)} PO week(s) {weeks.min().date()}..{weeks.max().date()}, "
                    f"expected {g['expected_po_units'].sum():,.0f} units, "
                    f"lead {g['lead_days'].min()}..{g['lead_days'].max()}"
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
    om = inputs.get("owner_meta") or {}
    for u in om.get("unresolved", []) or []:
        key = u.get("item_key") or u.get("sku") or u if isinstance(u, dict) else str(u)
        why = u.get("reason", "") if isinstance(u, dict) else ""
        ex.append(
            {
                "tcin": None,
                "sku": str(key),
                "issue": "OWNER_SKU_UNRESOLVED",
                "detail": f"no TCIN via biom_sku, vendor_style or alias; {why}",
            }
        )
    for w in om.get("warnings", []) or []:
        ex.append({"tcin": None, "sku": None, "issue": "OWNER_SHEET_WARNING", "detail": str(w)})
    if inputs.get("on_hand") is None:
        ex.append(
            {
                "tcin": None,
                "sku": None,
                "issue": "SUPPLY_NOT_LOADED",
                "detail": "RDZ sheet not provided; supply layer (v1.5) not applied",
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
            "owner_sheet": om.get("source", "not provided"),
            "pos_forecast_weights": {k: round(float(v), 2) for k, v in weights.items()},
            "controller": f"band {controller.band} weeks, k {controller.k} weeks; {controller.basis}",
            "receipt_lag_weeks": receipt_lag,
            "horizon": f"{horizon_weeks} PO weeks; {months} months",
            "grades": "A/B/C weekly by plan lead days (measured); monthly B when >=75% plan-covered, C model to 6 months, D beyond or owner-only, E beyond 12 months",
            "bands": "weekly: leave-one-week-out P10/P90 of log ratio from the backtest panel where fitted; otherwise symmetric ±band from the grade legend",
            "not_forecast": "launch/forward POs before Target creates them (shown booked or PLANNED_LAUNCH); item-level weekly timing beyond one week; realised shipments before ASN reconciliation",
        }
    )
    return b


__all__ = ["ForecastBundle", "run_forecast"]
