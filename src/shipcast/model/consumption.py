"""Monthly consumption model: the S&OP view.

Over a month, units are conserved at the retailer:

    receipts(month) = sales(month) + on_hand(end) - on_hand(start)

so expected monthly shipments are a sales (POS) forecast plus the change in
Target's chain inventory. Target has been selling roughly twice what it orders
since May 2026 because it is working down launch inventory (chain weeks of
supply 17 -> 10, measured 2026-09-04); items that have reached their normal
holding band already order what they sell.

The weekly path is simulated per TCIN:

    inside Target's plan horizon:  orders_t = plan-anchored weekly forecast
    beyond it (controller):        orders_t = max(0, sales_t + (band * sales_t - oh_t) / k)
    receipts_t = orders_{t - lag} + booked forward receipts_t
    oh_{t+1}   = max(0, oh_t + receipts_t - sales_t)

`band` is the weeks-of-supply Target converges to and `k` the number of weeks
over which it closes the gap. Both are CALIBRATED each run on trailing history
(`calibrate_controller`: grid over band x k minimising WAPE + |bias| of implied
weekly orders against actual replenishment POs) and printed in Accuracy with
their basis. Measured 2026-09-04 on 252 TCIN-weeks Jun 7 - Aug 23: band 8,
k 12 gives WAPE 0.685 and bias -7%; band 6, k 12 gives 0.664 / -27%. That is
worse than the plan at the weekly grain and better than "orders = sales"
(0.943); it is used only where the plan does not reach.

POS forecast candidates are gated on measured monthly accuracy against actual
POS (trailing months): trailing 8-week run-rate (WAPE 0.217, bias -13% on
Jul-Aug 2026), the channel owner's velocity forecast from the Brick & Mortar
Master Forecast (WAPE 0.286, bias -15%), and de-biased DFE when the feed is
fresh (stale since 2026-07-27). Weights are 1/WAPE^2 normalised over admitted
candidates; the default when history is too thin is 0.7 run-rate / 0.3 owner.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from shipcast.channels.target.calendar import TargetCalendar, sunday_week
from shipcast.model.grade import worse

CAND_RUNRATE = "runrate_8wk"
CAND_OWNER = "owner_velocity"
CAND_DFE = "dfe_debiased"

STREAM_REPLEN = "replenishment"
STREAM_BOOKED = "booked_forward"
STREAM_PLANNED_LAUNCH = "planned_launch"

DEFAULT_WEIGHTS: Mapping[str, float] = {CAND_RUNRATE: 0.7, CAND_OWNER: 0.3}


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def month_start(ts: pd.Series | pd.Timestamp) -> Any:
    """First day of the month for a Timestamp or Series."""
    if isinstance(ts, pd.Series):
        return pd.to_datetime(ts).dt.to_period("M").dt.to_timestamp()
    return pd.Timestamp(ts).to_period("M").to_timestamp()


def months_from(start: date, n: int) -> list[pd.Timestamp]:
    """`n` month starts beginning with the month containing `start`."""
    m0 = month_start(pd.Timestamp(start))
    return [m0 + pd.DateOffset(months=i) for i in range(int(n))]


def days_in_month(m: pd.Timestamp) -> int:
    """Calendar days in the month of `m`."""
    return int(pd.Timestamp(m).days_in_month)


def week_days_by_month(week_start: pd.Timestamp) -> dict[pd.Timestamp, int]:
    """How many of the 7 days of a Sunday-anchored week fall in each month."""
    out: dict[pd.Timestamp, int] = {}
    for i in range(7):
        d = pd.Timestamp(week_start) + pd.Timedelta(days=i)
        m = month_start(d)
        out[m] = out.get(m, 0) + 1
    return out


def weekly_to_monthly(
    weekly: pd.DataFrame,
    *,
    week_col: str,
    value_cols: Iterable[str],
    keys: Iterable[str] = ("tcin",),
) -> pd.DataFrame:
    """Allocate weekly values to months proportionally to the days of the week in each month."""
    value_cols = list(value_cols)
    keys = list(keys)
    rows = []
    for r in weekly.itertuples(index=False):
        ws = pd.Timestamp(getattr(r, week_col))
        for m, nd in week_days_by_month(ws).items():
            share = nd / 7.0
            row = {k: getattr(r, k) for k in keys}
            row["month_start"] = m
            for v in value_cols:
                row[v] = float(getattr(r, v) or 0.0) * share
            rows.append(row)
    if not rows:
        return pd.DataFrame(columns=[*keys, "month_start", *value_cols])
    return pd.DataFrame(rows).groupby([*keys, "month_start"], as_index=False)[value_cols].sum()


# --------------------------------------------------------------------------------------
# POS run-rate and history
# --------------------------------------------------------------------------------------


def weekly_pos(sales_weekly: pd.DataFrame) -> pd.DataFrame:
    """`week_start, tcin, units` from `signals.sales_weekly` rows (Sunday labels)."""
    s = sales_weekly.copy()
    s["week_start"] = pd.to_datetime(s["week_start_d"])
    return s.groupby(["week_start", "tcin"], as_index=False)["units"].sum()


def runrate(sales_weekly: pd.DataFrame, as_of: date, *, weeks: int = 8) -> pd.DataFrame:
    """Per TCIN mean weekly POS over the last `weeks` complete weeks before `as_of`.

    Returns `tcin, pos_wk, weeks_used, last_week`.
    """
    w = weekly_pos(sales_weekly)
    cutoff = pd.Timestamp(
        sunday_week(pd.Series([pd.Timestamp(as_of)])).iloc[0]
    )  # current week excluded
    w = w[w["week_start"] < cutoff]
    if w.empty:
        return pd.DataFrame(columns=["tcin", "pos_wk", "weeks_used", "last_week"])
    last_weeks = sorted(w["week_start"].unique())[-int(weeks) :]
    w = w[w["week_start"].isin(last_weeks)]
    grid = pd.MultiIndex.from_product(
        [last_weeks, w["tcin"].unique()], names=["week_start", "tcin"]
    ).to_frame(index=False)
    g = grid.merge(w, on=["week_start", "tcin"], how="left").fillna({"units": 0.0})
    out = g.groupby("tcin", as_index=False).agg(
        pos_wk=("units", "mean"), weeks_used=("units", "size")
    )
    out["last_week"] = last_weeks[-1]
    return out


def chain_on_hand_latest(inventory_weekly: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """Latest chain on-hand (stores + DCs) per TCIN on or before `as_of`: `tcin, on_hand, as_of_week`."""
    inv = inventory_weekly.copy()
    inv["week_end"] = pd.to_datetime(inv["week_end_d"])
    inv = inv[inv["week_end"] <= pd.Timestamp(as_of)]
    if inv.empty:
        return pd.DataFrame(columns=["tcin", "on_hand", "as_of_week"])
    latest = inv["week_end"].max()
    g = inv[inv["week_end"] == latest].groupby("tcin", as_index=False)["on_hand"].sum()
    g["as_of_week"] = latest
    return g


# --------------------------------------------------------------------------------------
# POS forecast candidates, scoring and blend
# --------------------------------------------------------------------------------------


def owner_velocity_by_month(owner: pd.DataFrame | None) -> pd.DataFrame:
    """`tcin, month_start, owner_velocity` from parsed owner rows (`forecast_basis == 'velocity'`)."""
    if owner is None or owner.empty:
        return pd.DataFrame(columns=["tcin", "month_start", "owner_velocity"])
    o = owner[(owner["forecast_basis"] == "velocity") & owner["tcin"].notna()].copy()
    o["month_start"] = month_start(o["period_start"])
    o["tcin"] = o["tcin"].astype("int64")
    return (
        o.groupby(["tcin", "month_start"], as_index=False)["forecast_units"]
        .sum()
        .rename(columns={"forecast_units": "owner_velocity"})
    )


def owner_load_orders_by_month(owner: pd.DataFrame | None) -> pd.DataFrame:
    """`tcin, month_start, load_units` from parsed owner rows (`forecast_basis == 'load_orders'`)."""
    if owner is None or owner.empty:
        return pd.DataFrame(columns=["tcin", "month_start", "load_units"])
    o = owner[(owner["forecast_basis"] == "load_orders") & owner["tcin"].notna()].copy()
    o["month_start"] = month_start(o["period_start"])
    o["tcin"] = o["tcin"].astype("int64")
    return (
        o.groupby(["tcin", "month_start"], as_index=False)["forecast_units"]
        .sum()
        .rename(columns={"forecast_units": "load_units"})
    )


def dfe_by_month(
    dfe: pd.DataFrame | None, *, ratio: float, as_of: date, max_age_days: int
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """De-biased DFE by month, only if the newest snapshot is fresh. Returns `(frame, note)`."""
    note: dict[str, Any] = {"used": False, "last_update_d": None, "ratio": ratio}
    if dfe is None or dfe.empty:
        return pd.DataFrame(columns=["tcin", "month_start", "dfe_debiased"]), note
    last = pd.to_datetime(dfe["last_update_d"]).max()
    note["last_update_d"] = last.date().isoformat()
    age = (pd.Timestamp(as_of) - last).days
    note["age_days"] = int(age)
    if age > max_age_days:
        note["reason"] = f"stale: {age} d > {max_age_days} d"
        return pd.DataFrame(columns=["tcin", "month_start", "dfe_debiased"]), note
    d = dfe.copy()
    d["week_start"] = pd.to_datetime(d["fiscal_week_begin_d"])
    d["units"] = pd.to_numeric(d["forecast_units"], errors="coerce").fillna(0.0) / float(ratio)
    m = weekly_to_monthly(d, week_col="week_start", value_cols=["units"])
    note["used"] = True
    return m.rename(columns={"units": "dfe_debiased"}), note


def pos_forecast_candidates(
    sales_weekly: pd.DataFrame,
    owner: pd.DataFrame | None,
    dfe: pd.DataFrame | None,
    *,
    as_of: date,
    months: Iterable[pd.Timestamp],
    trailing_weeks: int = 8,
    dfe_ratio: float = 1.2,
    dfe_max_age_days: int = 21,
    tcins: Iterable[int] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Long frame `tcin, month_start, candidate, pos_units` for every candidate that exists.

    Run-rate rows exist for TCINs with sales history; owner rows for TCINs in the
    owner sheet; DFE rows only while the feed is fresh. `notes` records why a
    candidate is absent.
    """
    months = list(months)
    rr = runrate(sales_weekly, as_of, weeks=trailing_weeks)
    rows: list[pd.DataFrame] = []
    if not rr.empty:
        grid = pd.MultiIndex.from_product(
            [rr["tcin"], months], names=["tcin", "month_start"]
        ).to_frame(index=False)
        g = grid.merge(rr[["tcin", "pos_wk"]], on="tcin")
        g["pos_units"] = g["pos_wk"] * g["month_start"].map(days_in_month) / 7.0
        g["candidate"] = CAND_RUNRATE
        rows.append(g[["tcin", "month_start", "candidate", "pos_units"]])
    ov = owner_velocity_by_month(owner)
    if not ov.empty:
        o = ov[ov["month_start"].isin(months)].rename(columns={"owner_velocity": "pos_units"})
        o["candidate"] = CAND_OWNER
        rows.append(o[["tcin", "month_start", "candidate", "pos_units"]])
    dfe_m, dfe_note = dfe_by_month(dfe, ratio=dfe_ratio, as_of=as_of, max_age_days=dfe_max_age_days)
    if not dfe_m.empty:
        d = dfe_m[dfe_m["month_start"].isin(months)].rename(columns={"dfe_debiased": "pos_units"})
        d["candidate"] = CAND_DFE
        rows.append(d[["tcin", "month_start", "candidate", "pos_units"]])
    out = (
        pd.concat(rows, ignore_index=True)
        if rows
        else pd.DataFrame(columns=["tcin", "month_start", "candidate", "pos_units"])
    )
    if tcins is not None:
        out = out[out["tcin"].isin({int(t) for t in tcins}) | out["candidate"].eq(CAND_OWNER)]
    notes = {
        "dfe": dfe_note,
        "runrate_weeks": int(trailing_weeks),
        "runrate_tcins": int(rr["tcin"].nunique()) if not rr.empty else 0,
    }
    return out, notes


def score_pos_candidates(
    sales_weekly: pd.DataFrame,
    owner: pd.DataFrame | None,
    *,
    as_of: date,
    months_back: int = 3,
    trailing_weeks: int = 8,
    min_rows: int = 20,
) -> pd.DataFrame:
    """Monthly WAPE and bias of each POS candidate against actual POS over trailing complete months.

    Run-rate is recomputed as of each month's start (no leakage). Owner values
    are the sheet's numbers for those months as they stand today (the sheet may
    have been revised since; the Accuracy sheet says so).
    """
    wp = weekly_pos(sales_weekly)
    if wp.empty:
        return pd.DataFrame(columns=["candidate", "n", "wape", "bias", "weight"])
    actual_m = weekly_to_monthly(wp, week_col="week_start", value_cols=["units"]).rename(
        columns={"units": "actual_pos"}
    )
    m_now = month_start(pd.Timestamp(as_of))
    months = [m_now - pd.DateOffset(months=i) for i in range(1, int(months_back) + 1)]
    recs: list[dict[str, Any]] = []
    ov = owner_velocity_by_month(owner)
    for m in months:
        rr = runrate(sales_weekly, m.date(), weeks=trailing_weeks)
        if rr.empty:
            continue
        a = actual_m[actual_m["month_start"] == m]
        j = a.merge(rr[["tcin", "pos_wk"]], on="tcin", how="inner")
        j["runrate_8wk"] = j["pos_wk"] * days_in_month(m) / 7.0
        if not ov.empty:
            j = j.merge(
                ov[ov["month_start"] == m][["tcin", "owner_velocity"]], on="tcin", how="left"
            )
        else:
            j["owner_velocity"] = np.nan
        j = j[j["actual_pos"] > 200]
        for cand in (CAND_RUNRATE, CAND_OWNER):
            sub = j[j[cand].notna()] if cand in j else j.iloc[0:0]
            for r in sub.itertuples(index=False):
                recs.append(
                    {
                        "month_start": m,
                        "tcin": r.tcin,
                        "candidate": cand,
                        "actual": r.actual_pos,
                        "forecast": getattr(r, cand),
                    }
                )
    if not recs:
        return pd.DataFrame(columns=["candidate", "n", "wape", "bias", "weight"])
    df = pd.DataFrame(recs)
    out = []
    for cand, g in df.groupby("candidate"):
        denom = g["actual"].sum()
        out.append(
            {
                "candidate": cand,
                "n": len(g),
                "wape": float((g["forecast"] - g["actual"]).abs().sum() / denom)
                if denom
                else np.nan,
                "bias": float((g["forecast"].sum() - denom) / denom) if denom else np.nan,
            }
        )
    tbl = pd.DataFrame(out)
    ok = tbl[(tbl["n"] >= min_rows) & tbl["wape"].notna() & (tbl["wape"] > 0)]
    if ok.empty:
        tbl["weight"] = tbl["candidate"].map(DEFAULT_WEIGHTS).fillna(0.0)
        tbl["weight_basis"] = "default (history too thin)"
    else:
        inv = 1.0 / ok["wape"] ** 2
        w = inv / inv.sum()
        tbl["weight"] = tbl["candidate"].map(dict(zip(ok["candidate"], w, strict=True))).fillna(0.0)
        tbl["weight_basis"] = (
            f"1/WAPE^2 over {int(months_back)} trailing months, as_of {as_of.isoformat()}"
        )
    return tbl


def blend_pos_forecast(candidates: pd.DataFrame, weights: Mapping[str, float]) -> pd.DataFrame:
    """Weighted blend per (tcin, month) over the candidates present, renormalising weights.

    Returns `tcin, month_start, pos_units, pos_candidates` (e.g. "runrate_8wk 0.70 | owner_velocity 0.30").
    """
    if candidates.empty:
        return pd.DataFrame(columns=["tcin", "month_start", "pos_units", "pos_candidates"])
    c = candidates.copy()
    c["w"] = c["candidate"].map(lambda k: float(weights.get(k, 0.0)))
    c = c[c["w"] > 0]
    if c.empty:  # nothing admitted: fall back to equal weights over whatever exists
        c = candidates.copy()
        c["w"] = 1.0
    c["wsum"] = c.groupby(["tcin", "month_start"])["w"].transform("sum")
    c["w"] = c["w"] / c["wsum"]
    c["contrib"] = c["w"] * c["pos_units"]
    out = c.groupby(["tcin", "month_start"], as_index=False).agg(pos_units=("contrib", "sum"))
    desc = (
        c.assign(txt=lambda d: d["candidate"] + " " + d["w"].round(2).astype(str))
        .groupby(["tcin", "month_start"])["txt"]
        .agg(" | ".join)
        .rename("pos_candidates")
        .reset_index()
    )
    return out.merge(desc, on=["tcin", "month_start"])


# --------------------------------------------------------------------------------------
# Inventory controller calibration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Controller:
    """Calibrated drawdown controller: `band` weeks of supply, gap closed over `k` weeks."""

    band: float
    k: float
    wape: float
    bias: float
    n: int
    basis: str


def history_panel(
    sales_weekly: pd.DataFrame,
    inventory_weekly: pd.DataFrame,
    actuals: pd.DataFrame,
    *,
    as_of: date,
    min_week: date | None = None,
) -> pd.DataFrame:
    """Weekly `tcin, week_start, pos, oh_prev, pos4, po` for calibration (weeks before `as_of`).

    `min_week` drops the launch pipeline-fill weeks (Apr 26 - May 17 2026 ran
    36-64k units against a 22k steady state) so they do not set the band.
    """
    wp = weekly_pos(sales_weekly)
    inv = inventory_weekly.copy()
    inv["week_start"] = pd.to_datetime(inv["week_start_d"])
    oh = inv.groupby(["week_start", "tcin"], as_index=False)["on_hand"].sum()
    a = actuals.copy()
    a["week_start"] = pd.to_datetime(a["wk"])
    po = (
        a.groupby(["week_start", "tcin"], as_index=False)["act_rep"]
        .sum()
        .rename(columns={"act_rep": "po"})
    )
    p = wp.merge(oh, on=["week_start", "tcin"], how="left").merge(
        po, on=["week_start", "tcin"], how="left"
    )
    p["po"] = p["po"].fillna(0.0)
    p = p[p["week_start"] < pd.Timestamp(sunday_week(pd.Series([pd.Timestamp(as_of)])).iloc[0])]
    p = p.sort_values(["tcin", "week_start"])
    p["pos4"] = p.groupby("tcin")["units"].transform(
        lambda s: s.rolling(4, min_periods=2).mean().shift(1)
    )
    p["oh_prev"] = p.groupby("tcin")["on_hand"].shift(1)
    p = p.rename(columns={"units": "pos"}).dropna(subset=["pos4", "oh_prev"])
    if min_week is not None:
        p = p[p["week_start"] >= pd.Timestamp(min_week)]
    return p


def calibrate_controller(
    panel: pd.DataFrame,
    *,
    bands: Iterable[float] = (4, 5, 6, 7, 8, 9, 10, 12),
    ks: Iterable[float] = (2, 4, 6, 8, 12, 16),
    min_rows: int = 60,
    default: tuple[float, float] = (8.0, 12.0),
) -> tuple[Controller, pd.DataFrame]:
    """Grid-search (band, k) minimising WAPE + |bias| of implied weekly orders vs actual replenishment POs.

    Returns the winner and the full grid. Falls back to `default` (band 8, k 12:
    WAPE 0.685 / bias -7%, measured 2026-09-04) when the panel is too thin.
    """
    p = panel[(panel["pos4"] > 100)]
    grid_rows = []
    for band in bands:
        for k in ks:
            f = np.maximum(0.0, p["pos4"] + (band * p["pos4"] - p["oh_prev"]) / k)
            denom = p["po"].sum()
            wape = float((p["po"] - f).abs().sum() / denom) if denom else np.nan
            bias = float((f.sum() - denom) / denom) if denom else np.nan
            grid_rows.append(
                {
                    "band": float(band),
                    "k": float(k),
                    "wape": wape,
                    "bias": bias,
                    "objective": (wape + abs(bias)) if denom else np.nan,
                }
            )
    grid = pd.DataFrame(grid_rows).sort_values("objective")
    if len(p) < min_rows or grid["objective"].isna().all():
        c = Controller(
            default[0],
            default[1],
            np.nan,
            np.nan,
            len(p),
            "default (history too thin): band 8 / k 12 measured 2026-09-04 WAPE 0.685 bias -7%",
        )
        return c, grid
    b = grid.iloc[0]
    c = Controller(
        float(b["band"]),
        float(b["k"]),
        float(b["wape"]),
        float(b["bias"]),
        len(p),
        f"grid search on {len(p)} TCIN-weeks ending {pd.Timestamp(p['week_start'].max()).date()}: min WAPE+|bias|",
    )
    return c, grid


# --------------------------------------------------------------------------------------
# Weekly simulation and monthly aggregation
# --------------------------------------------------------------------------------------


def _weekly_pos_path(
    pos_month: pd.DataFrame, weeks: list[pd.Timestamp], tcin: int, fallback_wk: float
) -> pd.Series:
    """Weekly sales for one TCIN from monthly POS forecast (days-proportional); fallback rate elsewhere."""
    pm = (
        pos_month[pos_month["tcin"] == tcin].set_index("month_start")["pos_units"]
        if not pos_month.empty
        else pd.Series(dtype=float)
    )
    vals = []
    for w in weeks:
        total = 0.0
        known = False
        for m, nd in week_days_by_month(w).items():
            if m in pm.index:
                total += float(pm.loc[m]) * nd / days_in_month(m)
                known = True
        vals.append(total if known else fallback_wk)
    return pd.Series(vals, index=weeks, dtype=float)


def simulate(
    *,
    weekly_forecast: pd.DataFrame,
    pos_month: pd.DataFrame,
    runrate_tbl: pd.DataFrame,
    on_hand: pd.DataFrame,
    actuals: pd.DataFrame,
    booked_receipts: pd.DataFrame,
    controller: Controller,
    as_of: date,
    weeks_ahead: int,
    calendar: TargetCalendar,
    receipt_lag_weeks: int = 3,
    plan_rungs: Iterable[str] = ("plan", "created"),
) -> pd.DataFrame:
    """Weekly path per TCIN: `tcin, week_start, sales, orders, receipts, oh_start, oh_end, source`.

    `weekly_forecast` is the plan-anchor output; rows whose `fallback_rung` is in
    `plan_rungs` drive `orders` inside the plan horizon, the controller drives
    the rest. `booked_receipts` is `tcin, week_start, units` (open forward PO
    lines placed at ship_begin + transit).
    """
    current = pd.Timestamp(calendar.week_start(as_of))
    weeks = [current + pd.Timedelta(days=7 * i) for i in range(int(weeks_ahead))]
    plan_rungs = set(plan_rungs)
    wf = weekly_forecast.copy()
    wf["po_week"] = pd.to_datetime(wf["po_week"])
    plan_rows = wf[wf["fallback_rung"].isin(plan_rungs)]
    plan_orders = plan_rows.set_index(["tcin", "po_week"])["expected_po_units"]
    fwd_keys = (
        set(
            plan_rows.loc[
                plan_rows["stream"].eq("planned_forward"), ["tcin", "po_week"]
            ].itertuples(index=False, name=None)
        )
        if "stream" in plan_rows
        else set()
    )
    rr = (
        runrate_tbl.set_index("tcin")["pos_wk"] if not runrate_tbl.empty else pd.Series(dtype=float)
    )
    oh0 = on_hand.set_index("tcin")["on_hand"] if not on_hand.empty else pd.Series(dtype=float)
    a = actuals.copy()
    a["wk"] = pd.to_datetime(a["wk"])
    hist_orders = a.set_index(["tcin", "wk"])["act_rep"] if not a.empty else pd.Series(dtype=float)
    br = (
        booked_receipts.copy()
        if booked_receipts is not None
        else pd.DataFrame(columns=["tcin", "week_start", "units"])
    )
    if not br.empty:
        br["week_start"] = pd.to_datetime(br["week_start"])
    br_idx = (
        br.set_index(["tcin", "week_start"])["units"] if not br.empty else pd.Series(dtype=float)
    )

    tcins = sorted(
        set(wf["tcin"].unique())
        | set(pos_month["tcin"].unique() if not pos_month.empty else [])
        | set(oh0.index)
    )
    out_rows = []
    for t in tcins:
        t = int(t)
        fallback_wk = float(rr.get(t, 0.0))
        sales = _weekly_pos_path(pos_month, weeks, t, fallback_wk)
        oh = float(oh0.get(t, 0.0))
        orders_hist: dict[pd.Timestamp, float] = {}
        for i in range(1, receipt_lag_weeks + 1):
            wk = current - pd.Timedelta(days=7 * i)
            orders_hist[wk] = float(hist_orders.get((t, wk), 0.0))
        orders_by_week: dict[pd.Timestamp, float] = dict(orders_hist)
        for w in weeks:
            s = float(sales.loc[w])
            key = (t, w)
            if key in plan_orders.index:
                o = float(plan_orders.loc[key])
                src = "plan_forward" if key in fwd_keys else "plan"
            else:
                o = max(0.0, s + (controller.band * s - oh) / controller.k)
                src = "controller"
            orders_by_week[w] = o
            lag_wk = w - pd.Timedelta(days=7 * receipt_lag_weeks)
            receipts = float(orders_by_week.get(lag_wk, 0.0)) + float(br_idx.get((t, w), 0.0))
            oh_end = max(0.0, oh + receipts - s)
            out_rows.append(
                {
                    "tcin": t,
                    "week_start": w,
                    "sales": s,
                    "orders": o,
                    "receipts": receipts,
                    "oh_start": oh,
                    "oh_end": oh_end,
                    "source": src,
                }
            )
            oh = oh_end
    return pd.DataFrame(out_rows)


def grade_month(
    month_index: int, plan_share: float, *, owner_only_pos: bool, has_history: bool
) -> str:
    """Monthly grade: plan-covered months B, model months C to 6 months out, D beyond; owner-only or new items one grade worse."""
    if plan_share >= 0.75:
        g = "B"
    elif plan_share > 0 or month_index <= 6:
        g = "C"
    elif month_index <= 12:
        g = "D"
    else:
        g = "E"
    if owner_only_pos or not has_history:
        g = worse(g, "D") if month_index > 2 else worse(g, "C")
    return g


def monthly_from_simulation(
    sim: pd.DataFrame,
    *,
    months: list[pd.Timestamp],
    calendar: TargetCalendar,
    ship_offset_days: Mapping[int, int],
    pos_blend: pd.DataFrame,
    runrate_tbl: pd.DataFrame,
) -> pd.DataFrame:
    """Aggregate the weekly path to months: PO units by create month, ship units by ship month, inventory path.

    Returns one row per (tcin, month_start) with `expected_po_units, expected_ship_units,
    pos_units, drawdown_release, on_hand_start, on_hand_end, wos_end, plan_share,
    step_up_month, grade`.
    """
    if sim.empty:
        return pd.DataFrame()
    s = sim.copy()
    s["po_month"] = month_start(s["week_start"])
    s["ship_date"] = s.apply(
        lambda r: r["week_start"] + pd.Timedelta(days=int(ship_offset_days.get(int(r["tcin"]), 5))),
        axis=1,
    )
    s["ship_month"] = month_start(s["ship_date"])
    s["is_plan"] = (s["source"] == "plan").astype(float)
    s["is_fwd"] = s["source"] == "plan_forward"
    s["orders_rep"] = np.where(s["is_fwd"], 0.0, s["orders"])
    s["orders_fwd"] = np.where(s["is_fwd"], s["orders"], 0.0)
    po = (
        s.groupby(["tcin", "po_month"], as_index=False)
        .agg(
            expected_po_units=("orders_rep", "sum"),
            planned_forward_po_units=("orders_fwd", "sum"),
            pos_sim=("sales", "sum"),
            plan_share=("is_plan", "mean"),
            n_weeks=("orders", "size"),
        )
        .rename(columns={"po_month": "month_start"})
    )
    ship = (
        s.groupby(["tcin", "ship_month"], as_index=False)
        .agg(
            expected_ship_units=("orders_rep", "sum"),
            planned_forward_ship_units=("orders_fwd", "sum"),
        )
        .rename(columns={"ship_month": "month_start"})
    )
    first = (
        s.sort_values("week_start")
        .groupby(["tcin", "po_month"], as_index=False)
        .first()[["tcin", "po_month", "oh_start"]]
        .rename(columns={"po_month": "month_start", "oh_start": "on_hand_start"})
    )
    last = (
        s.sort_values("week_start")
        .groupby(["tcin", "po_month"], as_index=False)
        .last()[["tcin", "po_month", "oh_end"]]
        .rename(columns={"po_month": "month_start", "oh_end": "on_hand_end"})
    )
    out = (
        po.merge(ship, on=["tcin", "month_start"], how="left")
        .merge(first, on=["tcin", "month_start"])
        .merge(last, on=["tcin", "month_start"])
    )
    out = out[out["month_start"].isin(months)]
    pb = (
        pos_blend.rename(columns={"pos_units": "pos_forecast_units"})
        if not pos_blend.empty
        else pd.DataFrame(columns=["tcin", "month_start", "pos_forecast_units", "pos_candidates"])
    )
    out = out.merge(pb, on=["tcin", "month_start"], how="left")
    out["pos_units"] = out["pos_forecast_units"].where(
        out["pos_forecast_units"].notna(), out["pos_sim"]
    )
    out["drawdown_release"] = out["expected_po_units"] - out["pos_units"]
    rr = (
        runrate_tbl.set_index("tcin")["pos_wk"] if not runrate_tbl.empty else pd.Series(dtype=float)
    )
    wk_rate = out["tcin"].map(lambda t: float(rr.get(int(t), np.nan)))
    out["wos_end"] = (out["on_hand_end"] / wk_rate.replace(0, np.nan)).round(1)
    # step-up: first month where orders >= 90% of sales after a month below 60%
    out = out.sort_values(["tcin", "month_start"])
    ratio = out["expected_po_units"] / out["pos_units"].replace(0, np.nan)
    out["order_to_sales"] = ratio.round(2)
    step = []
    for _t, g in out.groupby("tcin"):
        below = False
        for r in g.itertuples():
            rv = r.order_to_sales
            if pd.isna(rv):
                step.append((r.Index, ""))
                continue
            if rv < 0.6:
                below = True
                step.append((r.Index, ""))
            elif below and rv >= 0.9:
                step.append((r.Index, "STEP_UP"))
                below = False
            else:
                step.append((r.Index, ""))
    step_map = dict(step)
    out["step_up_month"] = out.index.map(lambda i: step_map.get(i, ""))
    m0 = months[0]
    out["month_index"] = out["month_start"].map(
        lambda m: (m.year - m0.year) * 12 + (m.month - m0.month)
    )
    return out.reset_index(drop=True)


__all__ = [
    "CAND_DFE",
    "CAND_OWNER",
    "CAND_RUNRATE",
    "DEFAULT_WEIGHTS",
    "STREAM_BOOKED",
    "STREAM_PLANNED_LAUNCH",
    "STREAM_REPLEN",
    "Controller",
    "blend_pos_forecast",
    "calibrate_controller",
    "chain_on_hand_latest",
    "grade_month",
    "history_panel",
    "month_start",
    "monthly_from_simulation",
    "months_from",
    "owner_load_orders_by_month",
    "owner_velocity_by_month",
    "pos_forecast_candidates",
    "runrate",
    "score_pos_candidates",
    "simulate",
    "weekly_to_monthly",
]
