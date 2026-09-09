"""The channel owner's store plan as SHAPE for the measured forecast, never as its LEVEL.

THE ONE IDEA. `dist_velocity` (model/consumption.py) measures the level of demand from
Target's own data and beats every alternative on WAPE. What it cannot see is the forward
SHAPE of distribution: every BPD feed is historical except the PO plan, which reaches
about two months out, so beyond that the engine holds an item's store count flat at its
own observed peak. The Brick & Mortar Master Forecast (Target Schedule tab) states a store
rollout to Dec-2028, and 30 of its 39 SKUs move after Nov-2026. That plan exists nowhere
in BigQuery except as the snapshot this module reads.

So the sheet supplies the shape, BPD supplies the level, and the sheet's own store count
is never allowed to become the level. Measured 2026-09-09, the sheet's `Stores` row sits
3-7% below the live selling-store count on every mature item and is flatly wrong on five
(K-DIS-2BAB-PUR says 1 door against 1,411 stocked; P-20WIP-SAN-STL-TRV says 0 doors and 0
UPSPW while selling 11,459 units in eight weeks). A sheet that wrong about the present is
not allowed to set the present.

    ramp(t, M)          = bm_stores(t, M) / max(bm_stores(t, anchor), bpd_stocked(t))   a RATIO
    stores_shaped(t, M) = max(stores_bpd_projection(t, M), stores_bpd(t, anchor) x ramp(t, M))
    pos(t, M)           = upspw(t) x stores_shaped(t, M) x days(M) / 7 x season(M)

The DENOMINATOR is the larger of what the sheet says today and what BPD measures today
(store locations holding inventory in the latest week). Measured 2026-09-09 the sheet
says 900-1,001 doors for three baby-wipes items that BPD stocks in 1,521-1,534; read as
a ratio from the sheet's own anchor those plans would have been x2.0-x2.2, read from the
measured base they are x1.27-x1.30. The sheet supplies the SHAPE of the rollout; BPD
supplies the level, including the level the ramp starts from. `BM_RAMP_ANCHOR_FROM_BPD`
marks the rows where BPD's count was the denominator.

`shape_pos_forecast` applies the ramp to the POS FORECAST BEFORE the weekly simulation, so
the drawdown controller sees the ramp week by week and replenishment follows it with the
inventory lag the simulation already models. The `max` is what stops double counting:
`dist_velocity` projects its own 3-week store trend (capped at 1.15x the item's peak) and
the sheet projects a rollout; whichever claims more distribution in a month wins, and the
`stores_source` column says which one did.

Three refusals, each because the ratio is undefined or untrustworthy rather than because
the answer is inconvenient:

  - `bm_stores(t, anchor) == 0`  -> ramp 1.0, flag BM_RAMP_NO_ANCHOR. There is no
    denominator. Live this catches the pre-launch TCINs and the item the sheet says has
    no doors while BPD watches it sell.
  - the block is a placeholder (`Stores == 1` wherever it is live) -> ramp 1.0, flag
    BM_RAMP_REFUSED_PLACEHOLDER.
  - ramp above `ramp_cap` -> clipped, flag BM_RAMP_CAPPED. The sheet's own live maximum
    is 1,950/1,575 = 1.238, so the cap is a guard against a future edit.

ONLY THE MEASURED REPLENISHMENT FORECAST IS SHAPED. `booked_forward`, `planned_forward`
and `planned_launch` are POs Target has cut or planned -- facts, not rates -- and scaling
a fact by a door plan would invent units.

LOAD_ORDERS IS A CROSS-CHECK, NOT A SOURCE. Measured 2026-09-09 the sheet's `Load_Orders`
agrees with what the engine already carries on some launches and is systematically one
month late and materially under on others. So it never moves a unit; it produces a
verdict per TCIN over the months both sources cover, and the disagreements are reported.

Months only the sheet describes (beyond the engine horizon, or pre-launch items with no
BPD row at all) are carried as `stated_only` rows at grade E, so the planner sees the
sheet's full reach and exactly how much of it rests on nothing measured.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import numpy as np
import pandas as pd

# Grades, best-first, matching config/target.yaml `grades.labels`.
GRADE_ORDER = ("A", "B", "C", "D", "E")

# Provenance of a row's units. Not a new grade vocabulary -- a column beside the grade.
AUTH_MEASURED = "measured"  # BPD only; the sheet did not move it
AUTH_SHAPED = "measured_shaped_by_plan"  # BPD level, sheet's store ramp
AUTH_STATED = "stated_only"  # the sheet alone; no BPD signal exists
AUTH_ORDER = (AUTH_MEASURED, AUTH_SHAPED, AUTH_STATED)  # strongest first

# Streams whose units are POs Target has cut or planned: facts, never shaped.
FACT_STREAMS = ("booked_forward", "planned_forward", "planned_launch", "created")

# A ramp inside this band is not a material contribution, so the row keeps its BPD
# grade. Operational choice, not measured (config bm_schedule.material_pct).
RAMP_MATERIAL = 0.02

# Guard against a future sheet edit. The sheet's own live maximum is 1,950/1,575 = 1.238.
RAMP_CAP = 3.0

# Load_Orders vs BPD: |relative gap| at or below this is agreement. Operational choice,
# not measured; chosen so a 26-unit gap in 102,698 (0.03%) reads as agreement and a
# 5,544-unit gap in 30,600 (18%) does not.
LOAD_TOLERANCE = 0.02

STREAM_REPLEN = "replenishment"
FLAG_SHAPED = "BM_SHAPED"
FLAG_STATED = "BM_ONLY_NO_BPD_SIGNAL"

STORES_FROM_BPD = "bpd_trend"
STORES_FROM_PLAN = "bm_plan"


def one_worse(grade: Any, steps: int = 1) -> str:
    """One (or `steps`) grade worse, clamped at E."""
    g = str(grade or "E").strip().upper()[:1]
    i = GRADE_ORDER.index(g) if g in GRADE_ORDER else len(GRADE_ORDER) - 1
    return GRADE_ORDER[min(i + steps, len(GRADE_ORDER) - 1)]


def weakest_authority(values: Iterable[Any]) -> str:
    """The weakest provenance among `values` (stated_only < shaped < measured)."""
    vals = [str(v) for v in values if str(v) in AUTH_ORDER]
    return max(vals, key=AUTH_ORDER.index) if vals else ""


def _days(m: pd.Timestamp) -> int:
    return calendar.monthrange(int(m.year), int(m.month))[1]


# --------------------------------------------------------------------------------------
# the ramp
# --------------------------------------------------------------------------------------


def store_ramp(
    bm: pd.DataFrame,
    anchor: pd.Timestamp,
    *,
    ramp_cap: float = RAMP_CAP,
    bpd_stocked: Mapping[int, float] | None = None,
) -> pd.DataFrame:
    """The sheet's store plan as a ratio to the anchor month, per TCIN x month.

    The denominator is max(sheet stores at anchor, BPD stocked stores today) when
    `bpd_stocked` is given -- the sheet's understatement of the present must not become
    growth. Returns tcin, month_start, bm_stores, bm_stores_anchor, bm_stores_anchor_used,
    bm_upspw, ramp, ramp_flag.
    """
    b = bm.loc[bm["tcin"].notna()].copy()
    b["tcin"] = b["tcin"].astype("int64")
    b["month_start"] = pd.to_datetime(b["month_start"])
    anchor_ts = pd.Timestamp(anchor)
    if "bm_placeholder" not in b:
        b["bm_placeholder"] = False
    base = (
        b.loc[b["month_start"] == anchor_ts, ["tcin", "bm_stores", "bm_placeholder"]]
        .drop_duplicates("tcin")
        .rename(columns={"bm_stores": "bm_stores_anchor"})
    )
    out = b.merge(base, on="tcin", how="left", suffixes=("", "_anchor_row"))
    out["bm_stores_anchor"] = out["bm_stores_anchor"].fillna(0.0)
    if "bm_placeholder_anchor_row" in out:
        out["bm_placeholder"] = (
            out["bm_placeholder_anchor_row"].fillna(out["bm_placeholder"]).fillna(False)
        )

    stocked = {int(k): float(v) for k, v in (bpd_stocked or {}).items()}
    ramp: list[float] = []
    flag: list[str] = []
    used: list[float] = []
    for r in out.itertuples(index=False):
        a = float(r.bm_stores_anchor or 0.0)
        s = float(r.bm_stores or 0.0)
        measured = float(stocked.get(int(r.tcin), 0.0) or 0.0)
        d = max(a, measured) if a > 0 else 0.0
        used.append(d)
        if bool(r.bm_placeholder):
            ramp.append(1.0)
            flag.append("BM_RAMP_REFUSED_PLACEHOLDER")
        elif a <= 0:
            # the sheet does not know the item is live today; its plan has no anchor
            ramp.append(1.0)
            flag.append("BM_RAMP_NO_ANCHOR")
        else:
            v = s / d
            f = "BM_RAMP_ANCHOR_FROM_BPD" if measured > a else ""
            if v > ramp_cap:
                ramp.append(float(ramp_cap))
                flag.append("|".join(x for x in (f, "BM_RAMP_CAPPED") if x))
            else:
                ramp.append(v)
                flag.append(f)
    out["ramp"] = ramp
    out["ramp_flag"] = flag
    out["bm_stores_anchor_used"] = used
    if "bm_upspw" not in out:
        out["bm_upspw"] = np.nan
    return out[
        [
            "tcin",
            "month_start",
            "bm_stores",
            "bm_stores_anchor",
            "bm_stores_anchor_used",
            "bm_upspw",
            "ramp",
            "ramp_flag",
        ]
    ].reset_index(drop=True)


def shape_pos_forecast(
    pos_fc: pd.DataFrame,
    ramp: pd.DataFrame,
    *,
    anchor: pd.Timestamp,
    material: float = RAMP_MATERIAL,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Shape the monthly POS forecast by the sheet's store ramp, before the simulation.

    `pos_fc` is `consumption.pos_forecast`'s frame (tcin, month_start, pos_units,
    stores_fwd, upspw, season_factor, flags, pos_candidates, ...). For every row with a
    ramp: the projected store count becomes max(BPD projection, BPD anchor stores x ramp)
    and POS scales with it. Rows with no usable store count (curated seeds) and rows the
    sheet does not cover pass through unchanged.

    Adds `pos_units_bpd` (before shaping), `bm_ramp`, `bm_stores`, `ramp_flag`,
    `stores_source` (bpd_trend | bm_plan) and `authority`; appends BM_SHAPED to `flags`
    where the sheet moved the row by more than `material`. Returns `(frame, note)`.
    """
    added = ["pos_units_bpd", "bm_ramp", "bm_stores", "ramp_flag", "stores_source", "authority"]
    note: dict[str, Any] = {
        "applied": False,
        "anchor": pd.Timestamp(anchor).strftime("%Y-%m-%d"),
        "rows_shaped": 0,
        "tcins_shaped": 0,
        "pos_units_added": 0.0,
    }
    if pos_fc is None or pos_fc.empty:
        out = pos_fc.copy() if pos_fc is not None else pd.DataFrame()
        for c in added:
            out[c] = pd.Series(dtype="object")
        return out, note
    p = pos_fc.copy()
    p["month_start"] = pd.to_datetime(p["month_start"])
    p["tcin"] = p["tcin"].astype("int64")
    p["pos_units_bpd"] = pd.to_numeric(p["pos_units"], errors="coerce").astype(float)
    if ramp is None or ramp.empty:
        p["bm_ramp"] = np.nan
        p["bm_stores"] = np.nan
        p["ramp_flag"] = ""
        p["stores_source"] = np.where(
            pd.to_numeric(p.get("stores_fwd"), errors="coerce").notna(), STORES_FROM_BPD, ""
        )
        p["authority"] = AUTH_MEASURED
        return p, note

    r = ramp[["tcin", "month_start", "ramp", "ramp_flag", "bm_stores"]].copy()
    r["month_start"] = pd.to_datetime(r["month_start"])
    r["tcin"] = r["tcin"].astype("int64")
    p = p.merge(r.rename(columns={"ramp": "bm_ramp"}), on=["tcin", "month_start"], how="left")
    p["ramp_flag"] = p["ramp_flag"].fillna("")

    own = pd.to_numeric(p["stores_fwd"], errors="coerce")
    anchor_ts = pd.Timestamp(anchor)
    at_anchor = (
        p.loc[p["month_start"] == anchor_ts, ["tcin", "stores_fwd"]]
        .drop_duplicates("tcin")
        .set_index("tcin")["stores_fwd"]
    )
    first = p.sort_values("month_start").drop_duplicates("tcin").set_index("tcin")["stores_fwd"]
    base = p["tcin"].map(at_anchor)
    base = base.where(base.notna(), p["tcin"].map(first))
    base = pd.to_numeric(base, errors="coerce")
    stores_plan = base * pd.to_numeric(p["bm_ramp"], errors="coerce")
    usable = own.notna() & (own > 0) & stores_plan.notna() & (base > 0)
    shaped = usable & (stores_plan > own * (1.0 + float(material)))
    new_stores = own.where(~shaped, stores_plan)
    scale = (new_stores / own).where(shaped, 1.0).fillna(1.0)
    p["pos_units"] = p["pos_units_bpd"] * scale
    p["stores_fwd"] = new_stores.round(1).where(own.notna(), p["stores_fwd"])
    p["stores_source"] = np.where(
        shaped, STORES_FROM_PLAN, np.where(own.notna(), STORES_FROM_BPD, "")
    )
    p["authority"] = np.where(shaped, AUTH_SHAPED, AUTH_MEASURED)
    flags = p["flags"].fillna("").astype(str) if "flags" in p else pd.Series("", index=p.index)
    p["flags"] = [
        "|".join([x for x in f.split("|") if x] + ([FLAG_SHAPED] if s else []))
        for f, s in zip(flags, shaped, strict=True)
    ]
    note.update(
        {
            "applied": True,
            "rows_shaped": int(shaped.sum()),
            "tcins_shaped": int(p.loc[shaped, "tcin"].nunique()),
            "pos_units_added": float((p["pos_units"] - p["pos_units_bpd"]).sum()),
        }
    )
    return p, note


def apply_ramp(
    monthly: pd.DataFrame, ramp: pd.DataFrame, *, material: float = RAMP_MATERIAL
) -> pd.DataFrame:
    """Scale a finished monthly replenishment frame by the ramp (post-hoc form).

    Kept for the self-check and for scoring experiments; the engine shapes the POS path
    before the simulation with `shape_pos_forecast` instead. `monthly` needs tcin,
    month_start, stream, units, grade.
    """
    m = monthly.copy()
    m["tcin"] = m["tcin"].astype("int64")
    r = ramp[["tcin", "month_start", "ramp", "ramp_flag", "bm_stores", "bm_stores_anchor"]]
    out = m.merge(r, on=["tcin", "month_start"], how="left")
    out["ramp"] = out["ramp"].astype(float).fillna(1.0)
    out["ramp_flag"] = out["ramp_flag"].fillna("")
    scalable = out["stream"].isin((STREAM_REPLEN, "forecast"))
    out["units_bpd"] = out["units"].astype(float)
    out["units"] = out["units_bpd"].where(~scalable, out["units_bpd"] * out["ramp"])
    moved = scalable & ((out["ramp"] - 1.0).abs() > material)
    out["authority"] = AUTH_MEASURED
    out.loc[moved, "authority"] = AUTH_SHAPED
    out["grade"] = out["grade"].where(~moved, out["grade"].map(one_worse))
    return out


def bm_only_rows(
    bm: pd.DataFrame,
    covered: set[tuple[int, pd.Timestamp]],
    *,
    months: list[pd.Timestamp] | None = None,
) -> pd.DataFrame:
    """The sheet's own `Velocity` for TCIN-months no engine row reaches. Graded E, always.

    A stated plan for an item or month with no measured signal must not inherit the grade
    of a measured row; it is shown so the planner sees the sheet's full reach and exactly
    how much of it rests on nothing measured.
    """
    b = bm.loc[bm["tcin"].notna()].copy()
    b["tcin"] = b["tcin"].astype("int64")
    b["month_start"] = pd.to_datetime(b["month_start"])
    if months is not None:
        b = b.loc[b["month_start"].isin([pd.Timestamp(m) for m in months])]
    keep = [
        (int(r.tcin), pd.Timestamp(r.month_start)) not in covered for r in b.itertuples(index=False)
    ]
    b = b.loc[keep]
    b = b.loc[pd.to_numeric(b["bm_velocity"], errors="coerce").fillna(0.0) > 0]
    cols = [
        "tcin",
        "month_start",
        "stream",
        "units",
        "units_bpd",
        "grade",
        "authority",
        "ramp",
        "ramp_flag",
        "bm_stores",
        "bm_stores_anchor",
    ]
    if b.empty:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(
        {
            "tcin": b["tcin"].to_numpy(),
            "month_start": b["month_start"].to_numpy(),
            "stream": STREAM_REPLEN,
            "units": b["bm_velocity"].astype(float).to_numpy(),
            "units_bpd": 0.0,
            "grade": "E",
            "authority": AUTH_STATED,
            "ramp": np.nan,
            "ramp_flag": FLAG_STATED,
            "bm_stores": b["bm_stores"].astype(float).to_numpy(),
            "bm_stores_anchor": np.nan,
        }
    )[cols]


# --------------------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------------------


def load_order_verdicts(
    bm: pd.DataFrame,
    bpd_launch: pd.DataFrame,
    *,
    months: list[pd.Timestamp],
    tolerance: float = LOAD_TOLERANCE,
) -> pd.DataFrame:
    """Cross-check the sheet's `Load_Orders` against the launch/forward units the engine carries.

    `bpd_launch` needs tcin, month_start, bpd_units (booked_forward + planned_launch +
    planned_forward for that month). `months` is the OVERLAP WINDOW and is required: the
    sheet carries 48 months back to Jan-2025 while a run carries 16 forward, and comparing
    the two unwindowed reports the sheet's whole launch history as units BPD is missing.

    The verdict is per TCIN, on the TOTAL over that window, because the sheet is
    systematically one month late; a month-by-month comparison would call a timing
    difference a units disagreement. Timing is reported separately (BM_LOAD_MONTH_SHIFTED).
    """
    window = {pd.Timestamp(m) for m in months}
    if not window:
        raise ValueError("load_order_verdicts needs a non-empty overlap window")
    b_all = bm.loc[
        bm["tcin"].notna(), ["tcin", "bm_sku", "month_start", "bm_load_orders", "bm_velocity"]
    ].copy()
    b_all["tcin"] = b_all["tcin"].astype("int64")
    b_all["month_start"] = pd.to_datetime(b_all["month_start"])
    b_all = b_all.loc[b_all["month_start"].isin(window)]
    velocity_in_window = b_all.groupby("tcin")["bm_velocity"].sum()
    b = b_all.loc[b_all["bm_load_orders"].astype(float) > 0]
    p = bpd_launch.copy()
    if p.empty:
        p = pd.DataFrame(columns=["tcin", "month_start", "bpd_units"])
    p["tcin"] = p["tcin"].astype("int64")
    p["month_start"] = pd.to_datetime(p["month_start"])
    p = p.loc[p["month_start"].isin(window) & (p["bpd_units"].astype(float) > 0)]

    cols = [
        "tcin",
        "bm_sku",
        "bm_load_units",
        "bpd_load_units",
        "gap_pct",
        "bm_months",
        "bpd_months",
        "verdict",
        "note",
        "bm_velocity_units",
    ]
    rows: list[dict[str, Any]] = []
    for t in sorted(set(b["tcin"]) | set(p["tcin"])):
        bt = b.loc[b["tcin"] == t]
        pt = p.loc[p["tcin"] == t]
        bu = float(bt["bm_load_orders"].sum())
        pu = float(pt["bpd_units"].sum())
        bm_months = sorted(pd.Timestamp(x).strftime("%Y-%m") for x in bt["month_start"])
        pd_months = sorted(pd.Timestamp(x).strftime("%Y-%m") for x in pt["month_start"])
        sku = bt["bm_sku"].iloc[0] if len(bt) else ""
        if pu <= 0:
            verdict, note = "BM_LOAD_ONLY", "sheet states a load Target carries nothing for"
        elif bu <= 0:
            verdict, note = "BPD_LOAD_ONLY", "Target carries a load the sheet does not state"
        else:
            gap = (bu - pu) / pu
            if abs(gap) <= tolerance:
                verdict = "BM_LOAD_AGREES" if bm_months == pd_months else "BM_LOAD_MONTH_SHIFTED"
                note = (
                    "units agree within tolerance"
                    if bm_months == pd_months
                    else f"units agree; sheet months {bm_months} vs Target {pd_months}"
                )
            else:
                verdict = "BM_LOAD_DISAGREES"
                note = f"sheet {bu:,.0f} vs Target {pu:,.0f} ({gap:+.1%})"
        rows.append(
            {
                "tcin": t,
                "bm_sku": sku,
                "bm_load_units": bu,
                "bpd_load_units": pu,
                "gap_pct": (bu - pu) / pu if pu else None,
                "bm_months": ",".join(bm_months),
                "bpd_months": ",".join(pd_months),
                "verdict": verdict,
                "note": note,
                # the sheet's ongoing Velocity over the same window: for a pre-launch item
                # the engine's planned_launch carries Target's WHOLE plan while the sheet
                # splits fill (Load_Orders) from run-rate (Velocity), so a reader compares
                # bpd_load_units with bm_load_units + bm_velocity_units there
                "bm_velocity_units": float(velocity_in_window.get(t, 0.0)),
            }
        )
    return pd.DataFrame(rows, columns=cols)


def store_check(
    bm: pd.DataFrame,
    ramp: pd.DataFrame,
    anchor: pd.Timestamp,
    pos_fc: pd.DataFrame | None,
    *,
    sku_for: Mapping[int, Any] | Callable[[int], Any] | None = None,
    stocked_stores: Mapping[int, float] | None = None,
) -> pd.DataFrame:
    """What the sheet says about doors versus what BPD measures, per item, at the anchor month.

    Columns: tcin, sku, bm_stores_anchor, bpd_stocked_stores, stores_gap_pct,
    bpd_selling_stores, bm_upspw, bpd_upspw, bm_stores_plan_peak, bm_peak_month,
    bm_ramp_to_peak, ramp_distinct_values, ramp_flag.

    The sheet's `Stores` is a DOOR count, so it is compared with `stocked_stores` (BPD
    store locations holding inventory in the latest week, from weekly_inv_tcin_loc), not
    with the SELLING-store count `dist_velocity` uses as its denominator -- a dispenser is
    stocked in ~1,600 doors and sells in ~900 of them in any one week, and calling that
    gap a disagreement would be wrong. `bpd_selling_stores` is still printed beside it.
    `stores_gap_pct` is the sheet's count relative to BPD's stocked count; negative means
    the sheet understates today's distribution. Without `stocked_stores` the gap falls
    back to the selling-store count and says so via NaN in `bpd_stocked_stores`.
    """
    anchor_ts = pd.Timestamp(anchor)
    if ramp is None or ramp.empty:
        return pd.DataFrame()
    a_cols = ["tcin", "bm_stores", "bm_upspw"] + (
        ["bm_stores_anchor_used"] if "bm_stores_anchor_used" in ramp else []
    )
    a = ramp.loc[ramp["month_start"] == anchor_ts, a_cols].rename(
        columns={"bm_stores": "bm_stores_anchor"}
    )
    if "bm_stores_anchor_used" not in a:
        a["bm_stores_anchor_used"] = a["bm_stores_anchor"]
    peak = (
        ramp.groupby("tcin", as_index=False)["bm_stores"]
        .max()
        .rename(columns={"bm_stores": "bm_stores_plan_peak"})
    )
    peak_month = (
        ramp.sort_values(["tcin", "bm_stores", "month_start"])
        .groupby("tcin", as_index=False)
        .last()[["tcin", "month_start"]]
        .rename(columns={"month_start": "bm_peak_month"})
    )
    out = a.merge(peak, on="tcin", how="outer").merge(peak_month, on="tcin", how="left")
    denom = pd.to_numeric(out["bm_stores_anchor_used"], errors="coerce")
    denom = denom.where(denom > 0)
    out["bm_ramp_to_peak"] = (out["bm_stores_plan_peak"].astype(float) / denom).round(4)
    if pos_fc is not None and not pos_fc.empty and "stores_fwd" in pos_fc:
        pf = pos_fc.copy()
        pf["month_start"] = pd.to_datetime(pf["month_start"])
        at = pf.loc[
            pf["month_start"] == anchor_ts, ["tcin", "stores_fwd", "upspw"]
        ].drop_duplicates("tcin")
        at = at.rename(columns={"stores_fwd": "bpd_selling_stores", "upspw": "bpd_upspw"})
        out = out.merge(at, on="tcin", how="left")
    else:
        out["bpd_selling_stores"] = np.nan
        out["bpd_upspw"] = np.nan
    stocked = {int(k): float(v) for k, v in (stocked_stores or {}).items()}
    out["bpd_stocked_stores"] = [stocked.get(int(t), np.nan) for t in out["tcin"]]
    basis = pd.to_numeric(out["bpd_stocked_stores"], errors="coerce")
    if basis.isna().all():
        basis = pd.to_numeric(out["bpd_selling_stores"], errors="coerce")
    out["stores_gap_pct"] = (
        (out["bm_stores_anchor"].astype(float) - basis) / basis.where(basis > 0)
    ).round(4)
    moves = ramp.groupby("tcin")["ramp"].nunique().rename("ramp_distinct_values")
    out = out.merge(moves, on="tcin", how="left")
    flags = (
        ramp.loc[ramp["ramp_flag"] != ""]
        .groupby("tcin")["ramp_flag"]
        .agg(lambda s: "|".join(sorted(set(s))))
        .rename("ramp_flag")
    )
    out = out.merge(flags, on="tcin", how="left")
    out["ramp_flag"] = out["ramp_flag"].fillna("")
    # a refused ramp has no peak ratio worth printing (1 placeholder door / 1,401 stocked)
    refused = out["ramp_flag"].str.contains("REFUSED|NO_ANCHOR", regex=True)
    out.loc[refused, "bm_ramp_to_peak"] = np.nan
    out["sku"] = [_lookup(sku_for, int(t)) for t in out["tcin"]]
    out["bm_peak_month"] = pd.to_datetime(out["bm_peak_month"]).dt.strftime("%b-%y")
    cols = [
        "tcin",
        "sku",
        "bm_stores_anchor",
        "bpd_stocked_stores",
        "stores_gap_pct",
        "bpd_selling_stores",
        "bm_stores_anchor_used",
        "bm_upspw",
        "bpd_upspw",
        "bm_stores_plan_peak",
        "bm_peak_month",
        "bm_ramp_to_peak",
        "ramp_distinct_values",
        "ramp_flag",
    ]
    return out[cols].sort_values("bm_stores_plan_peak", ascending=False).reset_index(drop=True)


def coverage(
    bm: pd.DataFrame,
    unresolved: Iterable[Mapping[str, Any]],
    *,
    item_tcins: Iterable[int],
    run_tcins: Iterable[int],
    item_state: Mapping[int, Any] | None = None,
    sku_for: Mapping[int, Any] | Callable[[int], Any] | None = None,
) -> pd.DataFrame:
    """Scope overlap, stated rather than assumed: every TCIN and every unresolved sheet SKU."""
    bm_t = {int(t) for t in bm.loc[bm["tcin"].notna(), "tcin"]} if not bm.empty else set()
    all_t = {int(t) for t in item_tcins}
    run_t = {int(t) for t in run_tcins}
    st = item_state or {}
    rows: list[dict[str, Any]] = [
        {
            "tcin": t,
            "sku": _lookup(sku_for, t) or "",
            "item_state": st.get(t, ""),
            "in_bm_sheet": t in bm_t,
            "in_item_master": t in all_t,
            "in_run": t in run_t,
            "note": "",
        }
        for t in sorted(all_t | bm_t)
    ]
    for u in unresolved:
        rows.append(
            {
                "tcin": None,
                "sku": str(u.get("bm_sku", "")),
                "item_state": "",
                "in_bm_sheet": True,
                "in_item_master": False,
                "in_run": False,
                "note": str(u.get("reason", "")),
            }
        )
    out = pd.DataFrame(
        rows,
        columns=["tcin", "sku", "item_state", "in_bm_sheet", "in_item_master", "in_run", "note"],
    )
    out["tcin"] = pd.array(out["tcin"].to_numpy(), dtype="Int64")
    return out


def _lookup(sku_for: Mapping[int, Any] | Callable[[int], Any] | None, tcin: int) -> Any:
    if sku_for is None:
        return ""
    if callable(sku_for):
        return sku_for(tcin) or ""
    v = sku_for.get(tcin, "")
    return "" if v is None or (isinstance(v, float) and v != v) else v


# --------------------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------------------


def demo() -> None:
    """Self-check: the three refusals, the max rule, the fact-stream rule and the verdicts."""
    months = [pd.Timestamp("2026-09-01"), pd.Timestamp("2027-06-01")]
    bm = pd.DataFrame(
        [
            # a live item whose plan ramps 1,575 -> 1,950
            dict(
                tcin=1,
                bm_sku="LIVE",
                month_start=months[0],
                bm_stores=1575.0,
                bm_upspw=1.0,
                bm_velocity=6750.0,
                bm_load_orders=0.0,
                bm_placeholder=False,
            ),
            dict(
                tcin=1,
                bm_sku="LIVE",
                month_start=months[1],
                bm_stores=1950.0,
                bm_upspw=1.0,
                bm_velocity=8357.0,
                bm_load_orders=0.0,
                bm_placeholder=False,
            ),
            # a placeholder block: 1 door wherever live
            dict(
                tcin=2,
                bm_sku="PLACE",
                month_start=months[0],
                bm_stores=1.0,
                bm_upspw=12.0,
                bm_velocity=51.0,
                bm_load_orders=0.0,
                bm_placeholder=True,
            ),
            dict(
                tcin=2,
                bm_sku="PLACE",
                month_start=months[1],
                bm_stores=1.0,
                bm_upspw=12.0,
                bm_velocity=51.0,
                bm_load_orders=0.0,
                bm_placeholder=True,
            ),
            # pre-launch: no anchor, then 1,500 doors
            dict(
                tcin=3,
                bm_sku="PRE",
                month_start=months[0],
                bm_stores=0.0,
                bm_upspw=0.0,
                bm_velocity=0.0,
                bm_load_orders=41000.0,
                bm_placeholder=False,
            ),
            dict(
                tcin=3,
                bm_sku="PRE",
                month_start=months[1],
                bm_stores=1500.0,
                bm_upspw=1.5,
                bm_velocity=9642.0,
                bm_load_orders=0.0,
                bm_placeholder=False,
            ),
        ]
    )
    r = store_ramp(bm, months[0])
    got = {
        (int(x.tcin), x.month_start): (round(float(x.ramp), 4), x.ramp_flag) for x in r.itertuples()
    }
    assert got[(1, months[0])] == (1.0, ""), got[(1, months[0])]
    assert got[(1, months[1])] == (round(1950 / 1575, 4), ""), got[(1, months[1])]
    assert got[(2, months[1])] == (1.0, "BM_RAMP_REFUSED_PLACEHOLDER"), got[(2, months[1])]
    assert got[(3, months[1])] == (1.0, "BM_RAMP_NO_ANCHOR"), got[(3, months[1])]
    # the denominator rule: BPD stocks 1,620 doors today where the sheet says 1,575, so the
    # plan to 1,950 is read from 1,620; a sheet anchor ABOVE BPD's count is kept as-is; a
    # sheet anchor of 0 still refuses even when BPD stocks the item
    r2 = store_ramp(bm, months[0], bpd_stocked={1: 1620.0, 3: 50.0})
    g2 = {
        (int(x.tcin), x.month_start): (round(float(x.ramp), 4), x.ramp_flag)
        for x in r2.itertuples()
    }
    assert g2[(1, months[1])] == (round(1950 / 1620, 4), "BM_RAMP_ANCHOR_FROM_BPD"), g2[
        (1, months[1])
    ]
    assert g2[(3, months[1])] == (1.0, "BM_RAMP_NO_ANCHOR"), g2[(3, months[1])]
    r3 = store_ramp(bm, months[0], bpd_stocked={1: 1000.0})
    v3 = float(r3.loc[(r3.tcin == 1) & (r3.month_start == months[1]), "ramp"].iloc[0])
    assert round(v3, 4) == round(1950 / 1575, 4), v3

    # the max rule on the POS path: BPD projects 1,300 -> 1,400 doors; the sheet says
    # x1.238 of 1,300 = 1,609 in June, which is more, so the sheet wins there and only there
    pos = pd.DataFrame(
        [
            dict(
                tcin=1,
                month_start=months[0],
                pos_units=1000.0,
                stores_fwd=1300.0,
                upspw=1.0,
                flags="",
            ),
            dict(
                tcin=1,
                month_start=months[1],
                pos_units=1076.9,
                stores_fwd=1400.0,
                upspw=1.0,
                flags="DIST_CAPPED",
            ),
            dict(
                tcin=2, month_start=months[1], pos_units=100.0, stores_fwd=7.0, upspw=1.0, flags=""
            ),
        ]
    )
    shaped, note = shape_pos_forecast(pos, r, anchor=months[0])
    s1 = shaped.loc[(shaped.tcin == 1) & (shaped.month_start == months[1])].iloc[0]
    assert abs(float(s1.stores_fwd) - round(1300 * 1950 / 1575, 1)) < 0.11, s1.stores_fwd
    assert abs(float(s1.pos_units) - 1076.9 * (1300 * 1950 / 1575) / 1400) < 0.01, s1.pos_units
    assert s1.stores_source == STORES_FROM_PLAN and s1.authority == AUTH_SHAPED
    assert "BM_SHAPED" in s1["flags"] and "DIST_CAPPED" in s1["flags"]
    s0 = shaped.loc[(shaped.tcin == 1) & (shaped.month_start == months[0])].iloc[0]
    assert float(s0.pos_units) == 1000.0 and s0.authority == AUTH_MEASURED  # anchor: ramp 1
    s2 = shaped.loc[shaped.tcin == 2].iloc[0]
    assert float(s2.pos_units) == 100.0 and s2.stores_source == STORES_FROM_BPD  # refused ramp
    assert note["rows_shaped"] == 1 and note["tcins_shaped"] == 1

    monthly = pd.DataFrame(
        [
            dict(tcin=1, month_start=months[1], stream="replenishment", units=1000.0, grade="D"),
            dict(tcin=1, month_start=months[1], stream="booked_forward", units=500.0, grade="A"),
            dict(tcin=2, month_start=months[1], stream="replenishment", units=100.0, grade="D"),
        ]
    )
    a = apply_ramp(monthly, r)
    rep = a.loc[(a.tcin == 1) & (a.stream == "replenishment")].iloc[0]
    bk = a.loc[a.stream == "booked_forward"].iloc[0]
    plc = a.loc[a.tcin == 2].iloc[0]
    assert round(float(rep.units), 2) == round(1000 * 1950 / 1575, 2), rep.units
    assert rep.grade == "E" and rep.authority == AUTH_SHAPED, (rep.grade, rep.authority)
    assert float(bk.units) == 500.0 and bk.grade == "A" and bk.authority == AUTH_MEASURED
    assert float(plc.units) == 100.0 and plc.grade == "D" and plc.authority == AUTH_MEASURED

    covered = {(1, months[1]), (2, months[1])}
    only = bm_only_rows(bm, covered)
    assert set(only["tcin"]) == {1, 2, 3}, set(only["tcin"])  # months[0] rows + tcin 3
    assert set(only["grade"]) == {"E"} and set(only["authority"]) == {AUTH_STATED}

    v = load_order_verdicts(
        bm,
        pd.DataFrame(
            [
                dict(tcin=3, month_start=months[0], bpd_units=41000.0),  # exact, same month
                dict(tcin=4, month_start=months[0], bpd_units=30600.0),  # BPD only
            ]
        ),
        months=months,
    )
    byt = {int(x.tcin): x.verdict for x in v.itertuples()}
    assert byt[3] == "BM_LOAD_AGREES", byt
    assert byt[4] == "BPD_LOAD_ONLY", byt
    v2 = load_order_verdicts(
        bm,
        pd.DataFrame([dict(tcin=3, month_start=months[0], bpd_units=41000.0)]),
        months=[months[1]],
    )
    assert list(v2["verdict"]) == [], list(v2["verdict"])

    sc = store_check(bm, r, months[0], shaped, sku_for={1: "LIVE", 2: "PLACE", 3: "PRE"})
    live = sc.loc[sc.tcin == 1].iloc[0]
    assert float(live.bpd_selling_stores) == 1300.0 and round(
        float(live.stores_gap_pct), 4
    ) == round(275 / 1300, 4)
    assert round(float(live.bm_ramp_to_peak), 4) == round(1950 / 1575, 4)
    assert weakest_authority([AUTH_MEASURED, AUTH_STATED, AUTH_SHAPED]) == AUTH_STATED
    print("bm_combine demo: all assertions passed")


if __name__ == "__main__":
    demo()
