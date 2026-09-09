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

The monthly POS forecast is `dist_velocity` — ONE estimator, no blend.

    upspw(t)      = sum(units over the trailing W weeks) / sum(selling stores over the same weeks)
    S_fwd(t, M)   = min(S_last + max(0, 3-week store slope) * weeks_ahead, ramp_cap * max S)
    dist_velocity = upspw * S_fwd * days_in_month / 7 * season_index(M)

i.e. Target's own `Stores x UPSPW = Velocity` identity, estimated from BPD
instead of read off a spreadsheet. Measured leak-free, 2026-09-08:

    dist_velocity  WAPE 0.1998  bias  +5.8%   (Jun/Jul/Aug 2026, 69 TCIN-months, actual > 200)
    runrate_8wk    WAPE 0.2962  bias  -9.4%   (same panel, reproduced)
    owner_velocity WAPE 0.3422  bias  -4.1%   (published 4 Sep run, quoted - see below)
    dist_velocity  WAPE 0.3054  bias  -4.7%   (Sep-25..Aug-26, 179 TCIN-months, 12 origins)
    runrate_8wk    WAPE 0.4341  bias -28.1%   (same 12-origin panel)

P(dist_velocity < runrate_8wk) = 1.000 on a month-block paired bootstrap on both
panels. The forward store ramp is what removes the bias: without it the 12-origin
bias is -14.3%, with it -4.7%. `runrate_8wk` is still SCORED every run and printed
on the Accuracy sheet as the reference, but it carries no weight.

WHY NO BLEND. The errors of the two estimators correlate at 0.686, so 1/WAPE^2
weighting - which assumes independence - over-weights the weaker one: the blend
scores 0.2035 against dist_velocity's 0.1998, i.e. WORSE than the single
candidate, and the best achievable fixed weight on runrate_8wk is 0.06-0.08.
`blend_pos_forecast` and the weight machinery are therefore RETIRED, not
re-tuned. Three transformations of one weekly POS series are not three signals.

WHY NO SEASONALITY TODAY. `seasonal_index` computes a shrunk month-of-year index
on distribution-normalised UPSPW and admits a month only on n >= 4 observations
AND >= 2 distinct calendar years. Live, 2026-09-08: no month qualifies, so every
factor is exactly 1.000 and the diagnostics are printed with their `n_years` so
the reason is visible. BPD holds 20 months (1.67 cycles) at chain level and <= 6.2
months for the items carrying 70% of current volume, which rules out STL; a
year-over-year ratio scored WAPE 2.20-3.04 with +190% to +304% bias because it
reads the 20x distribution ramp as a season. The gate turns the index on by
itself around Apr 2027. Full measurement: scratchpad/inhouse_velocity_forecast_design.md.

De-biased DFE is no longer a candidate (stale since 2026-07-27 and it failed the
admission gate on merit at WAPE 1.391 / bias +98%); `dfe_by_month` is kept only
to produce the freshness note and the DFE exception row.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from pipelines.target_shipment_forecast.channels.target.calendar import TargetCalendar, sunday_week
from pipelines.target_shipment_forecast.model.grade import worse

CAND_DIST = "dist_velocity"
CAND_RUNRATE = "runrate_8wk"
CAND_CURATED = "curated_launch"
CAND_DFE = "dfe_debiased"

STREAM_REPLEN = "replenishment"
STREAM_BOOKED = "booked_forward"
STREAM_PLANNED_FORWARD = "planned_forward"
STREAM_PLANNED_LAUNCH = "planned_launch"

# `source` values written by `simulate` and read by `monthly_from_simulation`.
SRC_PLAN = "plan"
SRC_PLAN_FORWARD = "plan_forward"
SRC_PLAN_LAUNCH = "plan_launch"
SRC_CONTROLLER = "controller"

POS_THIN = "POS_HISTORY_THIN"
POS_NO_HISTORY = "NEW_TCIN_NO_POS_HISTORY"
POS_NO_STORES = "POS_NO_SELLING_STORES"
POS_DIST_CAPPED = "DIST_CAPPED"
POS_CURATED = "CURATED_LAUNCH_SEED"


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


def weekly_pos_locs(sales_weekly: pd.DataFrame) -> pd.DataFrame:
    """`week_start, tcin, units, locations` - POS plus the SELLING-store count per week.

    `locations` is `signals.sales_weekly`'s `COUNT(DISTINCT location_id)`: the number of
    Target locations that actually rang a sale for that TCIN that week. It is summed
    here because the SQL already reduces to one row per (week, tcin).
    """
    s = sales_weekly.copy()
    s["week_start"] = pd.to_datetime(s["week_start_d"])
    if "locations" not in s:
        s["locations"] = np.nan
    return s.groupby(["week_start", "tcin"], as_index=False)[["units", "locations"]].sum()


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
# POS forecast: dist_velocity (single estimator, no blend)
# --------------------------------------------------------------------------------------


def dfe_by_month(
    dfe: pd.DataFrame | None, *, ratio: float, as_of: date, max_age_days: int
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """De-biased DFE by month, only if the newest snapshot is fresh. Returns `(frame, note)`.

    NOT a forecast candidate any more (see the module docstring): kept so the run can
    still report the feed's freshness and raise `DFE_STALE_OR_MISSING`.
    """
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


def distribution_state(
    sales_weekly: pd.DataFrame, as_of: date, *, weeks: int = 8, slope_weeks: int = 3
) -> pd.DataFrame:
    """Per TCIN store-count state as of `as_of`, from complete weeks only.

    Returns `tcin, upspw, stores_last, stores_peak, slope_per_week, weeks_used,
    weeks_selling, last_week`. `upspw` is units per SELLING store per week over the
    trailing `weeks`: the ratio's denominator counts productive doors, so a store that
    holds stock and sells nothing is excluded rather than diluting the rate. Measured
    2026-09-08, this beats the stocked-store denominator from `inventory_weekly`
    (WAPE 0.1998 vs 0.2295, P(better) = 0.843/0.963 on the two panels).

    `slope_per_week` is CLIPPED AT ZERO on purpose: the ramp projects growth, never
    decline. A falling store count is a drawdown or a delist, and extrapolating it
    downward would zero an item's forecast on a three-week wobble. Decline is handled
    by the flags, not by the trend.
    """
    cols = [
        "tcin",
        "upspw",
        "stores_last",
        "stores_peak",
        "slope_per_week",
        "weeks_used",
        "weeks_selling",
        "last_week",
    ]
    if sales_weekly is None or sales_weekly.empty:
        return pd.DataFrame(columns=cols)
    w = weekly_pos_locs(sales_weekly)
    cutoff = pd.Timestamp(sunday_week(pd.Series([pd.Timestamp(as_of)])).iloc[0])
    w = w[w["week_start"] < cutoff]
    if w.empty:
        return pd.DataFrame(columns=cols)
    all_weeks = sorted(w["week_start"].unique())
    window = all_weeks[-int(weeks) :]
    rows = []
    for tcin, g in w.groupby("tcin"):
        g = g.set_index("week_start").reindex(all_weeks).fillna({"units": 0.0, "locations": 0.0})
        win = g.loc[window]
        loc_sum = float(win["locations"].sum())
        unit_sum = float(win["units"].sum())
        stores_last = float(g["locations"].iloc[-1])
        back = g["locations"].iloc[-1 - min(int(slope_weeks), len(g) - 1)]
        span = min(int(slope_weeks), len(g) - 1) or 1
        rows.append(
            {
                "tcin": int(tcin),
                "upspw": unit_sum / loc_sum if loc_sum > 0 else np.nan,
                "stores_last": stores_last,
                "stores_peak": float(g["locations"].max()),
                "slope_per_week": max(0.0, (stores_last - float(back)) / span),
                "weeks_used": int(len(window)),
                "weeks_selling": int((win["units"] > 0).sum()),
                "last_week": all_weeks[-1],
            }
        )
    return pd.DataFrame(rows, columns=cols)


def seasonal_index(
    sales_weekly: pd.DataFrame,
    as_of: date,
    *,
    min_obs: int = 4,
    min_years: int = 2,
    shrink: float = 8.0,
) -> tuple[dict[int, float], pd.DataFrame]:
    """Multiplicative month-of-year index on distribution-normalised UPSPW.

    Method, named: a MOVING SEASONAL INDEX. Per TCIN-month take
    `upspw = units / mean weekly selling stores / (days/7)`, detrend
    `ln upspw` by that TCIN's own CENTRED 5-month moving mean (the trend here is a
    launch curve, not a line), pool the residuals by calendar month, shrink toward 1
    by `n/(n+shrink)`, and ADMIT a month only on `n >= min_obs` AND
    `n_years >= min_years`.

    The year gate is the whole point: a calendar month observed in one year only has
    an index that is definitionally inseparable from that year's trend, and BIOM's
    Target distribution grew ~20x over the 20 months BPD holds. Live 2026-09-08 NO
    month qualifies, so every returned factor is 1.000; the diagnostics frame carries
    `n`, `n_tcin`, `n_years`, `raw_index`, `shrunk_index` and `applied` so the reason
    is on the Accuracy sheet rather than in a comment. STL (needs >= 2 cycles; BPD has
    1.67, and <= 6.2 months for the items holding 70% of volume) and a year-over-year
    ratio (WAPE 2.20-3.04, bias +190% to +304% - it reads the ramp as a season) were
    both measured and rejected. Expect the gate to open around Apr 2027.
    """
    idx = {m: 1.0 for m in range(1, 13)}
    cols = ["month_of_year", "n", "n_tcin", "n_years", "raw_index", "shrunk_index", "applied"]
    if sales_weekly is None or sales_weekly.empty:
        return idx, pd.DataFrame(columns=cols)
    w = weekly_pos_locs(sales_weekly)
    w = w[w["week_start"] < pd.Timestamp(month_start(pd.Timestamp(as_of)))]
    if w.empty:
        return idx, pd.DataFrame(columns=cols)
    m = weekly_to_monthly(w, week_col="week_start", value_cols=["units", "locations"])
    m["days"] = m["month_start"].map(days_in_month)
    m["stores_wk"] = m["locations"] * 7.0 / m["days"]
    m["upspw"] = m["units"] / m["stores_wk"].replace(0, np.nan) / (m["days"] / 7.0)
    m = m[np.isfinite(m["upspw"]) & (m["upspw"] > 0) & (m["stores_wk"] > 50) & (m["units"] > 200)]
    if m.empty:
        return idx, pd.DataFrame(columns=cols)
    m = m.sort_values(["tcin", "month_start"])
    m["lu"] = np.log(m["upspw"])
    m["trend"] = m.groupby("tcin")["lu"].transform(
        lambda s: s.rolling(5, center=True, min_periods=3).mean()
    )
    m["resid"] = m["lu"] - m["trend"]
    m["moy"] = m["month_start"].dt.month
    d = m.dropna(subset=["resid"])
    if d.empty:
        return idx, pd.DataFrame(columns=cols)
    rows = []
    for moy, g in d.groupby("moy"):
        n = int(len(g))
        years = int(g["month_start"].dt.year.nunique())
        raw = round(float(np.exp(g["resid"].mean())), 4)
        # rounded before it is applied, so the factor PRINTED on the Accuracy sheet is
        # byte-for-byte the factor used. An unrounded index with a rounded diagnostic is
        # a reconciliation puzzle for whoever reads the sheet next.
        shrunk = round(float(np.exp(g["resid"].mean() * n / (n + shrink))), 4)
        applied = bool(n >= int(min_obs) and years >= int(min_years))
        if applied:
            idx[int(moy)] = shrunk
        rows.append(
            {
                "month_of_year": int(moy),
                "n": n,
                "n_tcin": int(g["tcin"].nunique()),
                "n_years": years,
                "raw_index": raw,
                "shrunk_index": shrunk,
                "applied": applied,
            }
        )
    return idx, pd.DataFrame(rows, columns=cols)


def dist_velocity(
    sales_weekly: pd.DataFrame,
    *,
    as_of: date,
    months: Iterable[pd.Timestamp],
    trailing_weeks: int = 8,
    slope_weeks: int = 3,
    ramp_cap: float = 1.15,
    season: Mapping[int, float] | None = None,
    min_weeks: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """`(forecast, state)` - the monthly POS forecast and the per-TCIN state behind it.

    `forecast` is `tcin, month_start, pos_units, stores_fwd, upspw, season_factor,
    dist_capped, flags`; a TCIN with no usable trailing POS gets NO ROW AT ALL rather
    than a zero, so a missing forecast propagates as "unknown" and never as "none"
    (see `NEW_TCIN_NO_POS_HISTORY` in `pipeline`). Leak-free by construction: every
    input week is strictly before `sunday_week(as_of)`.
    """
    months = list(months)
    state = distribution_state(
        sales_weekly, as_of, weeks=trailing_weeks, slope_weeks=slope_weeks
    )
    fcols = [
        "tcin",
        "month_start",
        "pos_units",
        "stores_fwd",
        "upspw",
        "season_factor",
        "dist_capped",
        "flags",
    ]
    if state.empty or not months:
        return pd.DataFrame(columns=fcols), state
    season = dict(season or {})
    need = int(min_weeks if min_weeks is not None else trailing_weeks)
    rows = []
    for r in state.itertuples(index=False):
        if not np.isfinite(r.upspw) or r.upspw <= 0 or r.stores_last <= 0:
            continue  # no productive doors and/or no rate: no number, by design
        flags = []
        if r.weeks_selling < need:
            flags.append(POS_THIN)
        cap = float(r.stores_peak) * float(ramp_cap)
        for m in months:
            mid = pd.Timestamp(m) + pd.Timedelta(days=days_in_month(m) / 2.0)
            ahead = max(0.0, (mid - pd.Timestamp(r.last_week)).days / 7.0)
            raw_fwd = float(r.stores_last) + float(r.slope_per_week) * ahead
            capped = raw_fwd > cap
            s_fwd = min(raw_fwd, cap)
            f = float(season.get(pd.Timestamp(m).month, 1.0))
            rows.append(
                {
                    "tcin": int(r.tcin),
                    "month_start": pd.Timestamp(m),
                    "pos_units": float(r.upspw) * s_fwd * days_in_month(m) / 7.0 * f,
                    "stores_fwd": round(s_fwd, 1),
                    "upspw": round(float(r.upspw), 4),
                    "season_factor": round(f, 4),
                    "dist_capped": capped,
                    "flags": "|".join(flags + ([POS_DIST_CAPPED] if capped else [])),
                }
            )
    return pd.DataFrame(rows, columns=fcols), state


def pos_forecast(
    sales_weekly: pd.DataFrame,
    *,
    as_of: date,
    months: Iterable[pd.Timestamp],
    trailing_weeks: int = 8,
    slope_weeks: int = 3,
    ramp_cap: float = 1.15,
    curated: pd.DataFrame | None = None,
    min_obs: int = 4,
    min_years: int = 2,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """The monthly POS forecast: `dist_velocity`, with the curated launch seed filling gaps.

    Returns `(frame, notes)` where `frame` is `tcin, month_start, pos_units,
    pos_candidates, ...`. `pos_candidates` names the estimator that produced the row
    (there is exactly one per row - the blend is retired), which keeps the column the
    Monthly-detail sheet already prints meaningful.

    The curated seed (`biom_admin.seed_target_launch_velocity`, basis `velocity`) is
    used ONLY where `dist_velocity` has no row, i.e. for a TCIN with no productive
    selling history. It never overrides a measured number and it is always graded E.
    """
    months = list(months)
    idx, season_diag = seasonal_index(
        sales_weekly, as_of, min_obs=min_obs, min_years=min_years
    )
    fc, state = dist_velocity(
        sales_weekly,
        as_of=as_of,
        months=months,
        trailing_weeks=trailing_weeks,
        slope_weeks=slope_weeks,
        ramp_cap=ramp_cap,
        season=idx,
    )
    fc = fc.copy()
    fc["pos_candidates"] = CAND_DIST
    have = set(fc["tcin"].astype(int)) if not fc.empty else set()
    cur = curated_velocity_by_month(curated)
    if not cur.empty:
        cur = cur[cur["month_start"].isin(months) & ~cur["tcin"].isin(have)]
    if not cur.empty:
        cur = cur.rename(columns={"velocity_units": "pos_units"})
        cur["pos_candidates"] = CAND_CURATED
        cur["flags"] = POS_CURATED
        cur["stores_fwd"] = np.nan
        cur["upspw"] = np.nan
        cur["season_factor"] = 1.0
        cur["dist_capped"] = False
        fc = pd.concat([fc, cur[fc.columns]], ignore_index=True)
    notes: dict[str, Any] = {
        "estimator": CAND_DIST,
        "trailing_weeks": int(trailing_weeks),
        "slope_weeks": int(slope_weeks),
        "ramp_cap": float(ramp_cap),
        "tcins_forecast": int(fc["tcin"].nunique()) if not fc.empty else 0,
        "tcins_from_curated_seed": sorted(set(cur["tcin"].astype(int))) if not cur.empty else [],
        "season_applied_months": sorted(m for m, f in idx.items() if f != 1.0),
        "season_basis": (
            f"moving seasonal index on distribution-normalised UPSPW; admitted on "
            f"n >= {min_obs} and n_years >= {min_years}"
        ),
    }
    return fc, {"pos": notes, "season_diag": season_diag, "state": state}


def curated_velocity_by_month(seed: pd.DataFrame | None) -> pd.DataFrame:
    """`tcin, month_start, velocity_units` from the curated launch seed (basis `velocity`)."""
    cols = ["tcin", "month_start", "velocity_units"]
    if seed is None or seed.empty:
        return pd.DataFrame(columns=cols)
    s = seed[seed["basis"].astype(str).str.lower() == "velocity"].copy()
    if s.empty:
        return pd.DataFrame(columns=cols)
    s["month_start"] = month_start(pd.to_datetime(s["period_start"]))
    s["tcin"] = pd.to_numeric(s["tcin"], errors="coerce").astype("int64")
    return (
        s.groupby(["tcin", "month_start"], as_index=False)["units"]
        .sum()
        .rename(columns={"units": "velocity_units"})
    )


def curated_load_orders_by_month(seed: pd.DataFrame | None) -> pd.DataFrame:
    """`tcin, month_start, load_units` from the curated launch seed (basis `load_orders`).

    The one thing BPD cannot supply for `planned_launch`: launch volume a human knows
    about that Target's own PO plan does not carry yet. Netted against booked and
    planned launch/forward exactly as the retired owner-sheet `Load_Orders` row was.
    """
    cols = ["tcin", "month_start", "load_units"]
    if seed is None or seed.empty:
        return pd.DataFrame(columns=cols)
    s = seed[seed["basis"].astype(str).str.lower() == "load_orders"].copy()
    if s.empty:
        return pd.DataFrame(columns=cols)
    s["month_start"] = month_start(pd.to_datetime(s["period_start"]))
    s["tcin"] = pd.to_numeric(s["tcin"], errors="coerce").astype("int64")
    return (
        s.groupby(["tcin", "month_start"], as_index=False)["units"]
        .sum()
        .rename(columns={"units": "load_units"})
    )


def score_pos_candidates(
    sales_weekly: pd.DataFrame,
    *,
    as_of: date,
    months_back: int = 3,
    trailing_weeks: int = 8,
    slope_weeks: int = 3,
    ramp_cap: float = 1.15,
    min_actual: float = 200.0,
) -> pd.DataFrame:
    """Monthly WAPE and bias of `dist_velocity` and `runrate_8wk` vs actual POS.

    EVIDENCE, NOT WEIGHTS. Both estimators are recomputed as of each month's start -
    including the seasonal index, which is refitted on months strictly before the
    origin - so nothing sees the month it forecasts. `used` marks the one the run
    actually publishes. The blend this function used to weight is retired; see the
    module docstring for why (errors correlate 0.686 and the blend scored worse).

    Same panel definition as the retired version: the `months_back` complete months
    before `as_of`, per TCIN, rows with actual POS > `min_actual`.
    """
    cols = ["candidate", "n", "wape", "bias", "median_ape", "used", "basis"]
    if sales_weekly is None or sales_weekly.empty:
        return pd.DataFrame(columns=cols)
    wp = weekly_pos(sales_weekly)
    if wp.empty:
        return pd.DataFrame(columns=cols)
    actual_m = weekly_to_monthly(wp, week_col="week_start", value_cols=["units"]).rename(
        columns={"units": "actual"}
    )
    m_now = month_start(pd.Timestamp(as_of))
    months = [m_now - pd.DateOffset(months=i) for i in range(1, int(months_back) + 1)]
    recs: list[dict[str, Any]] = []
    for m in months:
        origin = pd.Timestamp(m).date()
        rr = runrate(sales_weekly, origin, weeks=trailing_weeks)
        if rr.empty:
            continue
        idx, _ = seasonal_index(sales_weekly, origin)
        dv, _ = dist_velocity(
            sales_weekly,
            as_of=origin,
            months=[m],
            trailing_weeks=trailing_weeks,
            slope_weeks=slope_weeks,
            ramp_cap=ramp_cap,
            season=idx,
        )
        a = actual_m[actual_m["month_start"] == m]
        j = a.merge(rr[["tcin", "pos_wk"]], on="tcin", how="inner")
        j[CAND_RUNRATE] = j["pos_wk"] * days_in_month(m) / 7.0
        dvm = (
            dv[dv["month_start"] == m][["tcin", "pos_units"]].rename(
                columns={"pos_units": CAND_DIST}
            )
            if not dv.empty
            else pd.DataFrame(columns=["tcin", CAND_DIST])
        )
        j = j.merge(dvm, on="tcin", how="left")
        j = j[j["actual"] > float(min_actual)]
        for cand in (CAND_DIST, CAND_RUNRATE):
            sub = j[j[cand].notna()]
            for r in sub.itertuples(index=False):
                recs.append(
                    {
                        "month_start": m,
                        "tcin": r.tcin,
                        "candidate": cand,
                        "actual": float(r.actual),
                        "forecast": float(getattr(r, cand)),
                    }
                )
    if not recs:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(recs)
    basis = (
        f"{int(months_back)} trailing complete months as of {as_of.isoformat()}, "
        f"per TCIN, rows with actual POS > {int(min_actual)}; leak-free rolling origin"
    )
    out = []
    for cand, g in df.groupby("candidate"):
        denom = g["actual"].sum()
        ape = (g["forecast"] - g["actual"]).abs() / g["actual"]
        out.append(
            {
                "candidate": cand,
                "n": len(g),
                "wape": float((g["forecast"] - g["actual"]).abs().sum() / denom)
                if denom
                else np.nan,
                "bias": float((g["forecast"].sum() - denom) / denom) if denom else np.nan,
                "median_ape": float(ape.median()),
                "used": cand == CAND_DIST,
                "basis": basis,
            }
        )
    return pd.DataFrame(out, columns=cols).sort_values("wape").reset_index(drop=True)


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
    def _keys(stream: str) -> set[tuple[Any, Any]]:
        if "stream" not in plan_rows:
            return set()
        return set(
            plan_rows.loc[plan_rows["stream"].eq(stream), ["tcin", "po_week"]].itertuples(
                index=False, name=None
            )
        )

    fwd_keys = _keys(STREAM_PLANNED_FORWARD)
    launch_keys = _keys(STREAM_PLANNED_LAUNCH)
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
                if key in launch_keys:
                    src = SRC_PLAN_LAUNCH
                elif key in fwd_keys:
                    src = SRC_PLAN_FORWARD
                else:
                    src = SRC_PLAN
            else:
                o = max(0.0, s + (controller.band * s - oh) / controller.k)
                src = SRC_CONTROLLER
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
    month_index: int, plan_share: float, *, pos_thin: bool, has_history: bool
) -> str:
    """Monthly grade: plan-covered months B, model months C to 6 months out, D beyond.

    `pos_thin` replaces the retired `owner_only_pos`. The owner sheet is gone, so the
    "one grade worse" penalty now attaches to a POS forecast that is thin on its own
    terms: fewer complete selling weeks than the trailing window, a forward store count
    that hit the ramp cap, a curated-seed row, or fewer than 100 selling stores. All
    four are computable from BPD and all four are printed as columns.
    """
    if plan_share >= 0.75:
        g = "B"
    elif plan_share > 0 or month_index <= 6:
        g = "C"
    elif month_index <= 12:
        g = "D"
    else:
        g = "E"
    if pos_thin or not has_history:
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

    The two unit columns are the SAME weekly `orders` series grouped by two different
    dates — `po_month` = month of the PO week, `ship_month` = month of
    (PO week + the item group's ship offset). They are **not** additive and neither is
    derived from the other: equal in total, different per month by exactly what a week
    carries across a month end.

        create_month(M) = ship_month(M) - (carried in from M-1) + (carried out to M+1)

    Worked example (TCIN 94928292, cleaning, offset 5 d, as-of 2026-09-04):
    the week of 30 Aug orders 552 and ships 4 Sep, so it is an AUGUST create month and a
    SEPTEMBER ship month; the week of 27 Sep orders 8,904 and ships 2 Oct, the mirror
    case. Sep-26 therefore reads ship 6,480 = 552+1,656+1,872+2,400 and create
    14,832 = 1,656+1,872+2,400+8,904, i.e. 14,832 = 6,480 - 552 + 8,904 — never
    6,480 + 8,904. `tests/test_model.py::test_create_month_vs_ship_month_identity` pins
    it, and `pipeline.PO_VS_SHIP_NOTE` states it on the README sheet
    (biom_sql fix (a), 2026-09-07).

    Note the first displayed month is structurally under-inclusive on the create-month
    column: the `months` filter below drops the prior month's create bucket, so units it
    carried into month 1's shipments have no create row on the sheet.

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
    s["is_plan"] = (s["source"] == SRC_PLAN).astype(float)
    s["is_fwd"] = s["source"] == SRC_PLAN_FORWARD
    s["is_launch"] = s["source"] == SRC_PLAN_LAUNCH
    s["orders_rep"] = np.where(s["is_fwd"] | s["is_launch"], 0.0, s["orders"])
    s["orders_fwd"] = np.where(s["is_fwd"], s["orders"], 0.0)
    s["orders_launch"] = np.where(s["is_launch"], s["orders"], 0.0)
    po = (
        s.groupby(["tcin", "po_month"], as_index=False)
        .agg(
            expected_po_units=("orders_rep", "sum"),
            planned_forward_po_units=("orders_fwd", "sum"),
            planned_launch_po_units=("orders_launch", "sum"),
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
            planned_launch_ship_units=("orders_launch", "sum"),
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
    # `pos_blend` is `pos_forecast`'s output. The extra columns travel with it so the
    # Monthly-detail sheet can show the two factors behind every POS number (upspw and
    # the projected store count) instead of an opaque total — which is the whole point
    # of a Stores x UPSPW decomposition.
    pb_cols = [
        "tcin",
        "month_start",
        "pos_forecast_units",
        "pos_candidates",
        "stores_fwd",
        "upspw",
        "season_factor",
        "pos_flags",
    ]
    pb = (
        pos_blend.rename(columns={"pos_units": "pos_forecast_units", "flags": "pos_flags"})
        if not pos_blend.empty
        else pd.DataFrame(columns=pb_cols)
    )
    out = out.merge(pb[[c for c in pb_cols if c in pb]], on=["tcin", "month_start"], how="left")
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
    "CAND_CURATED",
    "CAND_DFE",
    "CAND_DIST",
    "CAND_RUNRATE",
    "POS_CURATED",
    "POS_DIST_CAPPED",
    "POS_NO_HISTORY",
    "POS_NO_STORES",
    "POS_THIN",
    "SRC_CONTROLLER",
    "SRC_PLAN",
    "SRC_PLAN_FORWARD",
    "SRC_PLAN_LAUNCH",
    "STREAM_BOOKED",
    "STREAM_PLANNED_FORWARD",
    "STREAM_PLANNED_LAUNCH",
    "STREAM_REPLEN",
    "Controller",
    "calibrate_controller",
    "chain_on_hand_latest",
    "curated_load_orders_by_month",
    "curated_velocity_by_month",
    "dfe_by_month",
    "dist_velocity",
    "distribution_state",
    "grade_month",
    "history_panel",
    "month_start",
    "monthly_from_simulation",
    "months_from",
    "pos_forecast",
    "runrate",
    "score_pos_candidates",
    "seasonal_index",
    "simulate",
    "weekly_pos",
    "weekly_pos_locs",
    "weekly_to_monthly",
]
