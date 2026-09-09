"""Plan-anchored weekly PO forecast (the primary weekly signal).

Expected PO units for a TCIN in PO week W = Target's daily PO plan `ORDERED_Q`
for each order day in W, taken from the FRESHEST plan snapshot (`BUSINESS_D`)
that is (a) on or before the order day and (b) on or before the run origin,
summed over receiving DCs, used AS-IS. No rescaling and no intercept: a
plan-only OLS made leave-one-week-out WAPE worse (0.368 vs 0.168) because an
intercept blurs the many exact matches.

Measured accuracy by lead days (order day minus BUSINESS_D), replenishment
stream, 16 complete weeks 2026-05-17..2026-08-23, measured 2026-09-04:

    lead 0 -> WAPE 0.123 (49.8% exact)   lead 3 -> 0.305
    lead 1 -> WAPE 0.180 (13.3% exact)   lead 4 -> 0.416
    lead 2 -> WAPE 0.251                 lead 5-10 -> 0.54-0.62
    bias -1% overall; chain-level WAPE 0.11 -> 0.28 over the same leads

A snapshot dated AFTER an order day shows ORDERED_Q = 0 for that day (the order
has been cut), which is why rule (a) exists and why past order days in the
current week come from the order feed as stream `created` instead.

Fallback rungs (never blended, always labelled):
    plan                        plan row found, used as-is
    created                     the PO already exists in the order feed
    plan_zero_history_positive  plan says 0 but the TCIN had a PO in the trailing
                                4 weeks -> 0, flagged. Target ordered anyway in
                                5 of 34 such cases (2026-09-04); reported as a
                                statistic in Accuracy, not used as a forecast.
    no_signal                   plan 0 and no PO in 8 weeks -> 0
    beyond_plan_horizon         order week past the last planned ORDER_D ->
                                trailing 4-week mean, grade D
    no_plan_snapshot            no usable snapshot at all -> trailing 4-week mean
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta

import numpy as np
import pandas as pd

from pipelines.target_shipment_forecast.channels.target.calendar import TargetCalendar, sunday_week

RUNG_PLAN = "plan"
RUNG_CREATED = "created"
RUNG_PLAN_ZERO = "plan_zero_history_positive"
RUNG_NO_SIGNAL = "no_signal"
RUNG_BEYOND = "beyond_plan_horizon"
RUNG_NO_PLAN = "no_plan_snapshot"

STREAM_FORECAST = "forecast"
STREAM_CREATED = "created"
STREAM_PLANNED_FORWARD = "planned_forward"
STREAM_PLANNED_LAUNCH = "planned_launch"

FLAG_NEW_TCIN = "NEW_TCIN_NO_HISTORY"
FLAG_PLAN_ZERO = "PLAN_ZERO_HISTORY_POSITIVE"
FLAG_NO_SIGNAL = "NO_SIGNAL"
FLAG_PLANNED_FORWARD = "PLANNED_FORWARD"
FLAG_LAUNCH_FILL = "LAUNCH_FILL"
FLAG_BEYOND = "BEYOND_PLAN_HORIZON"
FLAG_STALE_PLAN = "STALE_PLAN"
FLAG_NO_PO_8WK = "NO_PO_8WK"

OUTPUT_COLUMNS: tuple[str, ...] = (
    "po_week",
    "tcin",
    "expected_po_units",
    "lead_days",
    "plan_business_d",
    "n_dc",
    "stream",
    "primary_signal",
    "fallback_rung",
    "flags",
    "lag1_rep",
    "mean4_rep",
    "mean8_rep",
    "weeks_ordered_8",
)


def freshest_plan_by_order_day(plan: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """Per (tcin, order_d): ORDERED_Q summed over DCs from the freshest eligible snapshot.

    Eligible means `business_d <= min(as_of, order_d)`. Returns
    `tcin, order_d, business_d, ordered_q, n_dc, lead_days`.
    """
    if plan is None or plan.empty:
        return pd.DataFrame(
            columns=["tcin", "order_d", "business_d", "ordered_q", "n_dc", "lead_days"]
        )
    p = plan.copy()
    p["business_d"] = pd.to_datetime(p["business_d"])
    p["order_d"] = pd.to_datetime(p["order_d"])
    p = p[p["business_d"] <= pd.Timestamp(as_of)]
    p = p[p["business_d"] <= p["order_d"]]
    if p.empty:
        return pd.DataFrame(
            columns=["tcin", "order_d", "business_d", "ordered_q", "n_dc", "lead_days"]
        )
    agg = p.groupby(["business_d", "tcin", "order_d"], as_index=False).agg(
        ordered_q=("ordered_q", "sum"), n_dc=("receiving_location_id", "nunique")
    )
    idx = agg.groupby(["tcin", "order_d"])["business_d"].idxmax()
    best = agg.loc[idx].copy()
    best["lead_days"] = (best["order_d"] - best["business_d"]).dt.days.astype(int)
    return best.reset_index(drop=True)


def _weekly_from_order_days(best: pd.DataFrame) -> pd.DataFrame:
    """Sum order-day rows into PO weeks; lead/business_d taken from the largest order day."""
    if best.empty:
        return pd.DataFrame(
            columns=["po_week", "tcin", "plan_units", "lead_days", "plan_business_d", "n_dc"]
        )
    b = best.copy()
    b["po_week"] = sunday_week(b["order_d"])
    b = b.sort_values(["po_week", "tcin", "ordered_q"], ascending=[True, True, False])
    head = b.groupby(["po_week", "tcin"], as_index=False).first()[
        ["po_week", "tcin", "lead_days", "business_d", "n_dc"]
    ]
    sums = b.groupby(["po_week", "tcin"], as_index=False).agg(
        plan_units=("ordered_q", "sum"), n_dc_max=("n_dc", "max")
    )
    out = sums.merge(head, on=["po_week", "tcin"])
    out["n_dc"] = out["n_dc_max"]
    return out.rename(columns={"business_d": "plan_business_d"})[
        ["po_week", "tcin", "plan_units", "lead_days", "plan_business_d", "n_dc"]
    ]


def history_features(actuals: pd.DataFrame, current_week: pd.Timestamp) -> pd.DataFrame:
    """Per TCIN naive features from weeks strictly before `current_week`.

    `lag1_rep`, `mean4_rep`, `mean8_rep` (zero-filled means), `weeks_ordered_8`.
    """
    cols = ["tcin", "lag1_rep", "mean4_rep", "mean8_rep", "weeks_ordered_8"]
    if actuals is None or actuals.empty:
        return pd.DataFrame(columns=cols)
    a = actuals.copy()
    a["wk"] = pd.to_datetime(a["wk"])
    a = a[a["wk"] < current_week]
    if a.empty:
        return pd.DataFrame(columns=cols)
    weeks = sorted(a["wk"].unique())
    last8 = weeks[-8:]
    last4 = weeks[-4:]
    grid = pd.MultiIndex.from_product([last8, a["tcin"].unique()], names=["wk", "tcin"]).to_frame(
        index=False
    )
    g = grid.merge(a[["wk", "tcin", "act_rep"]], on=["wk", "tcin"], how="left").fillna(
        {"act_rep": 0.0}
    )
    feats = g.groupby("tcin").agg(
        mean8_rep=("act_rep", "mean"), weeks_ordered_8=("act_rep", lambda s: int((s > 0).sum()))
    )
    m4 = g[g["wk"].isin(last4)].groupby("tcin")["act_rep"].mean().rename("mean4_rep")
    l1 = g[g["wk"] == weeks[-1]].set_index("tcin")["act_rep"].rename("lag1_rep")
    out = feats.join(m4).join(l1).reset_index()
    return out[cols]


def expected_po_units(
    plan: pd.DataFrame,
    actuals: pd.DataFrame,
    *,
    as_of: date,
    horizon_weeks: int,
    calendar: TargetCalendar,
    tcins: Iterable[int] | None = None,
    item_state: Mapping[int, str] | None = None,
    pos_runrate: Mapping[int, float] | None = None,
    planned_forward_multiple: float = 3.0,
    stale_plan_days: int = 4,
) -> pd.DataFrame:
    """Forecast PO units by (po_week, tcin) from the freshest plan snapshot, with rungs and flags.

    Args:
        plan: `signals.plan_snapshots` rows (one or more BUSINESS_D <= as_of).
        actuals: `signals.po_actuals_weekly` rows (`wk, tcin, act_rep, act_fwd, ...`).
        as_of: run origin. No plan row with business_d > as_of is used.
        horizon_weeks: PO weeks to emit, starting with the week containing as_of.
        calendar: Sunday-anchored calendar.
        tcins: extra TCINs to keep in the universe (e.g. the item master); the
            plan and the trailing 8 weeks of actuals are always included.
        item_state: TCIN -> Target ITEM_STATE; `READY_FOR_ORDER` tags LAUNCH_FILL.
        pos_runrate: TCIN -> trailing weekly POS. An item with sales but no POs in 8
            weeks is a drawdown item (NO_PO_8WK), not a new one; the PLANNED_FORWARD
            test compares the plan to max(trailing PO mean, weekly POS) so a step-up
            to sales level is not mistaken for a launch load.
        planned_forward_multiple: plan > multiple x trailing-8-week mean flags
            PLANNED_FORWARD (config `streams.planned_forward_multiple`).
        stale_plan_days: lead > this on the current week's rows adds STALE_PLAN.

    Returns:
        One row per (po_week, tcin) with `OUTPUT_COLUMNS`.
    """
    current_week = pd.Timestamp(calendar.week_start(as_of))
    weeks = [current_week + pd.Timedelta(days=7 * i) for i in range(int(horizon_weeks))]

    best = freshest_plan_by_order_day(plan, as_of)
    weekly_plan = _weekly_from_order_days(best)
    plan_horizon_end = pd.Timestamp(best["order_d"].max()) if not best.empty else pd.NaT

    feats = history_features(actuals, current_week)

    universe: set[int] = set()
    if not best.empty:
        universe |= {int(t) for t in best["tcin"].unique()}
    if not feats.empty:
        universe |= {int(t) for t in feats["tcin"].unique()}
    if tcins is not None:
        universe |= {int(t) for t in tcins}
    if not universe:
        return pd.DataFrame(columns=list(OUTPUT_COLUMNS))

    grid = pd.MultiIndex.from_product(
        [weeks, sorted(universe)], names=["po_week", "tcin"]
    ).to_frame(index=False)
    df = grid.merge(weekly_plan, on=["po_week", "tcin"], how="left").merge(
        feats, on="tcin", how="left"
    )
    for c in ("lag1_rep", "mean4_rep", "mean8_rep"):
        df[c] = df[c].fillna(0.0)
    df["weeks_ordered_8"] = df["weeks_ordered_8"].fillna(0).astype(int)
    has_history = df["weeks_ordered_8"] > 0

    # Created stream: POs already in the order feed for weeks >= current week.
    created = pd.DataFrame(columns=["po_week", "tcin", "act_rep"])
    if actuals is not None and not actuals.empty:
        a = actuals.copy()
        a["wk"] = pd.to_datetime(a["wk"])
        created = a[(a["wk"] >= current_week) & (a["act_rep"] > 0)][
            ["wk", "tcin", "act_rep"]
        ].rename(columns={"wk": "po_week"})
    df = df.merge(created, on=["po_week", "tcin"], how="left")

    plan_present = df["plan_units"].notna()
    plan_positive = plan_present & (df["plan_units"] > 0)
    beyond = (
        pd.Series(False, index=df.index)
        if pd.isna(plan_horizon_end)
        else df["po_week"] > sunday_week(pd.Series([plan_horizon_end])).iloc[0]
    )
    no_plan_at_all = weekly_plan.empty

    expected = np.where(plan_present, df["plan_units"], 0.0).astype(float)
    rung = np.full(len(df), RUNG_PLAN, dtype=object)
    rung[~plan_positive.to_numpy()] = np.where(
        has_history[~plan_positive], RUNG_PLAN_ZERO, RUNG_NO_SIGNAL
    )
    if no_plan_at_all:
        rung[:] = RUNG_NO_PLAN
        expected = df["mean4_rep"].to_numpy(dtype=float)
    else:
        b = beyond.to_numpy()
        rung[b] = RUNG_BEYOND
        expected[b] = df.loc[beyond, "mean4_rep"].to_numpy(dtype=float)

    created_mask = df["act_rep"].notna()
    expected[created_mask.to_numpy()] = df.loc[created_mask, "act_rep"].to_numpy(dtype=float)
    rung[created_mask.to_numpy()] = RUNG_CREATED

    df["expected_po_units"] = np.round(expected, 0)
    df["fallback_rung"] = rung
    df["stream"] = np.where(created_mask, STREAM_CREATED, STREAM_FORECAST)
    df["primary_signal"] = np.select(
        [created_mask, np.isin(rung, [RUNG_PLAN]), np.isin(rung, [RUNG_BEYOND, RUNG_NO_PLAN])],
        ["order_feed", "target_po_plan", "trailing_4wk_mean"],
        default="none",
    )
    df.loc[created_mask, "lead_days"] = 0
    df.loc[created_mask, "plan_business_d"] = pd.NaT

    flags: list[list[str]] = [[] for _ in range(len(df))]
    states = item_state or {}
    pos = pos_runrate or {}
    fwd_stream = np.zeros(len(df), dtype=bool)
    launch_stream = np.zeros(len(df), dtype=bool)
    for i, row in enumerate(df.itertuples(index=False)):
        r = row.fallback_rung
        f = flags[i]
        if r == RUNG_PLAN_ZERO:
            f.append(FLAG_PLAN_ZERO)
        elif r == RUNG_NO_SIGNAL:
            f.append(FLAG_NO_SIGNAL)
        elif r == RUNG_BEYOND:
            f.append(FLAG_BEYOND)
        pos_wk = float(pos.get(int(row.tcin), 0.0) or 0.0)
        if r == RUNG_PLAN and row.weeks_ordered_8 == 0 and row.expected_po_units > 0:
            f.append(FLAG_NEW_TCIN if pos_wk <= 0 else FLAG_NO_PO_8WK)
        # LAUNCH takes precedence over FORWARD, and the two are mutually exclusive.
        # Before 2026-09-08 both tests OR-ed into `planned_forward`, which silently put
        # launch pipeline-fills in the forward-buy stream: TCIN 1011824789's 43,968-unit
        # pre-launch load was labelled `planned_forward|LAUNCH_FILL` in the 4 Sep run,
        # then fell to plain `replenishment` once fix (d) made item_state live and Target
        # said READY_FOR_LAUNCH rather than READY_FOR_ORDER. Both labels are wrong for a
        # 43,968-unit load on an item that has never sold. The state test alone is too
        # narrow for exactly that reason, so a launch is state OR no-selling-history.
        is_launch = r == RUNG_PLAN and row.expected_po_units > 0 and (
            str(states.get(int(row.tcin), "")).upper() == "READY_FOR_ORDER"
            or (row.weeks_ordered_8 == 0 and pos_wk <= 0)
        )
        baseline = max(float(row.mean8_rep), pos_wk)
        if is_launch:
            f.append(FLAG_LAUNCH_FILL)
            launch_stream[i] = True
        elif (
            r == RUNG_PLAN
            and baseline > 0
            and row.expected_po_units > planned_forward_multiple * baseline
        ):
            f.append(FLAG_PLANNED_FORWARD)
            fwd_stream[i] = True
        if (
            r == RUNG_PLAN
            and row.po_week == current_week
            and pd.notna(row.lead_days)
            and row.lead_days > stale_plan_days
        ):
            f.append(FLAG_STALE_PLAN)
    df["flags"] = ["|".join(f) for f in flags]
    df.loc[fwd_stream & (df["stream"] == STREAM_FORECAST), "stream"] = STREAM_PLANNED_FORWARD
    df.loc[launch_stream & (df["stream"] == STREAM_FORECAST), "stream"] = STREAM_PLANNED_LAUNCH
    df["lead_days"] = df["lead_days"].astype("Int64")
    df["n_dc"] = df["n_dc"].astype("Int64")
    return df[list(OUTPUT_COLUMNS)].sort_values(["po_week", "tcin"]).reset_index(drop=True)


def plan_horizon(plan: pd.DataFrame, as_of: date) -> tuple[date | None, date | None]:
    """`(freshest business_d, last planned order_d)` visible at `as_of`, or Nones."""
    best = freshest_plan_by_order_day(plan, as_of)
    if best.empty:
        return None, None
    return pd.Timestamp(best["business_d"].max()).date(), pd.Timestamp(best["order_d"].max()).date()


def weeks_between(a: date, b: date) -> int:
    """Whole weeks from `a` to `b` (Sunday labels), negative if b < a."""
    return (
        sunday_week(pd.Series([pd.Timestamp(b)])).iloc[0]
        - sunday_week(pd.Series([pd.Timestamp(a)])).iloc[0]
    ).days // 7


__all__ = [
    "OUTPUT_COLUMNS",
    "RUNG_BEYOND",
    "RUNG_CREATED",
    "RUNG_NO_PLAN",
    "RUNG_NO_SIGNAL",
    "RUNG_PLAN",
    "RUNG_PLAN_ZERO",
    "STREAM_PLANNED_FORWARD",
    "STREAM_PLANNED_LAUNCH",
    "expected_po_units",
    "freshest_plan_by_order_day",
    "history_features",
    "plan_horizon",
    "timedelta",
]
