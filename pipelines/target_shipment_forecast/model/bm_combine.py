"""Combine the B&M master forecast with the BPD-measured `dist_velocity` forecast.

THE ONE IDEA. `dist_velocity` measures the LEVEL of demand and beats every alternative
on WAPE (0.1887 vs runrate_8wk 0.2878 vs the retired owner_velocity 0.3422, live
2026-09-08). What it cannot see is the forward SHAPE of distribution: every BPD feed is
historical except `dly_po_plan_tcin.ORDER_D`, which reaches 2026-11-06, so beyond ~2
months `dist_velocity` holds the store count flat at the item's own peak. The B&M sheet
states a store rollout to Dec-2028 and 30 of its 39 SKUs move after Nov-2026.

So: BPD supplies the level, B&M supplies the shape, and the sheet's own store count is
never allowed to become the level. Measured 2026-09-09, B&M's `Stores` row is 3-7% below
the live authorised count on every mature item and flatly wrong on five (K-DIS-2BAB-PUR
says 1 door against 1,411 stocked; P-20WIP-SAN-STL-TRV says 0 doors and 0 UPSPW while
selling 11,459 units in eight weeks). A sheet that wrong about the present is not
allowed to set the present.

    ramp(t, M)  = bm_stores(t, M) / bm_stores(t, anchor)          # a RATIO, never a level
    units(t, M) = shipcast_replenishment(t, M) * ramp(t, M)

Three refusals, each because the ratio is undefined or untrustworthy rather than because
the answer is inconvenient:

  - `bm_stores(t, anchor) == 0`  -> ramp 1.0, flag BM_RAMP_NO_ANCHOR. There is no
    denominator. Live this catches the six pre-launch TCINs and 94979716, the item the
    sheet says has no doors while BPD watches it sell.
  - the block is a placeholder (`Stores == 1` wherever it is live) -> ramp 1.0, flag
    BM_RAMP_REFUSED_PLACEHOLDER. Five blocks live.
  - ramp above `ramp_cap` -> clipped, flag BM_RAMP_CAPPED. The sheet's own maximum is
    1,950/1,575 = 1.238, so the cap is a guard against a future edit, not a live filter.

ONLY THE REPLENISHMENT STREAM IS SCALED. `booked_forward`, `planned_forward` and
`planned_launch` are POs Target has cut or planned -- facts, not rates -- and scaling a
fact by a door plan would invent units. This is the same reason `dist_velocity` divides
by store count in the first place.

  ==> JUDGMENT CALL, FLAGGED: scaling monthly replenishment units linearly in the store
  ramp assumes the drawdown controller is linear in the POS level over a month. It is
  linear in STEADY STATE (the controller converges orders -> sales) but not through a
  ramp, where on-hand lags. The exact treatment is to re-run `simulate` with a ramped
  POS path; that needs the live BQ pull and would change the production monthly grid,
  which is out of scope here (see scratchpad/bm_target_forecast_combined.md).

LOAD_ORDERS IS A CROSS-CHECK, NOT A SOURCE. Measured three ways on 2026-09-09, B&M's
`Load_Orders` agrees with what Shipcast already carries -- exactly, on units, for the
two flushable launches (26,016 and 15,080 against `ENDING_ON_PURCHASE_Q` and against
`booked_forward`) and to 26 units in 102,698 for the bergamot go-pack. It is also
systematically ONE MONTH LATE and materially under on three of six. So it never moves a
unit that BPD already carries; it produces a verdict per (TCIN, month) and the
disagreements are reported, exactly as load_cost_files.py blocks a BOM pair whose two
sources disagree rather than averaging them.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

import pandas as pd

# Grades, worst-first, matching config/target.yaml `grades.labels`.
GRADE_ORDER = ("A", "B", "C", "D", "E")

# Authority of a row's units. Not a new grade vocabulary -- a provenance column.
AUTH_MEASURED = "measured"  # BPD only; B&M did not move it
AUTH_SHAPED = "measured_shaped_by_plan"  # BPD level, B&M store ramp
AUTH_STATED = "stated_only"  # B&M alone; no BPD signal exists

# A ramp inside this band is not a material B&M contribution, so the row keeps its
# BPD grade. Operational choice, not measured.
RAMP_MATERIAL = 0.02

# Guard against a future sheet edit. The sheet's own live maximum is 1,950/1,575 = 1.238.
RAMP_CAP = 3.0

STREAM_REPLEN = "replenishment"
SCALABLE_STREAMS = (STREAM_REPLEN, "forecast")

# Load_Orders vs BPD: |relative gap| at or below this is agreement. Operational choice,
# not measured; chosen so the 26-unit gap in 102,698 (0.03%) reads as agreement and the
# 5,544-unit gap in 30,600 (18%) does not.
LOAD_TOLERANCE = 0.02


def _worse(grade: str, steps: int = 1) -> str:
    """One (or `steps`) grade worse, clamped at E."""
    g = str(grade or "E").strip().upper()[:1]
    i = GRADE_ORDER.index(g) if g in GRADE_ORDER else len(GRADE_ORDER) - 1
    return GRADE_ORDER[min(i + steps, len(GRADE_ORDER) - 1)]


def _days(m: pd.Timestamp) -> int:
    return calendar.monthrange(int(m.year), int(m.month))[1]


@dataclass(frozen=True)
class Anchor:
    """The month whose store count is the ramp's denominator."""

    month: pd.Timestamp
    reason: str


def store_ramp(bm: pd.DataFrame, anchor: pd.Timestamp, *, ramp_cap: float = RAMP_CAP) -> pd.DataFrame:
    """B&M's store plan expressed as a ratio to its own anchor month, per TCIN x month.

    Returns tcin, month_start, bm_stores, bm_stores_anchor, ramp, ramp_flag.
    """
    b = bm.loc[bm["tcin"].notna()].copy()
    b["tcin"] = b["tcin"].astype("int64")
    anchor_ts = pd.Timestamp(anchor)
    base = (
        b.loc[b["month_start"] == anchor_ts, ["tcin", "bm_stores", "bm_placeholder"]]
        .drop_duplicates("tcin")
        .rename(columns={"bm_stores": "bm_stores_anchor"})
    )
    out = b.merge(base, on="tcin", how="left")
    out["bm_stores_anchor"] = out["bm_stores_anchor"].fillna(0.0)
    out["bm_placeholder"] = out["bm_placeholder_y"].fillna(out["bm_placeholder_x"]).fillna(False)

    ramp = []
    flag = []
    for r in out.itertuples(index=False):
        a = float(r.bm_stores_anchor or 0.0)
        s = float(r.bm_stores or 0.0)
        if bool(r.bm_placeholder):
            ramp.append(1.0)
            flag.append("BM_RAMP_REFUSED_PLACEHOLDER")
        elif a <= 0:
            ramp.append(1.0)
            flag.append("BM_RAMP_NO_ANCHOR")
        else:
            v = s / a
            if v > ramp_cap:
                ramp.append(ramp_cap)
                flag.append("BM_RAMP_CAPPED")
            else:
                ramp.append(v)
                flag.append("")
    out["ramp"] = ramp
    out["ramp_flag"] = flag
    return out[
        ["tcin", "month_start", "bm_stores", "bm_stores_anchor", "bm_upspw", "ramp", "ramp_flag"]
    ]


def apply_ramp(monthly: pd.DataFrame, ramp: pd.DataFrame) -> pd.DataFrame:
    """Scale the replenishment stream of a Shipcast monthly frame by the B&M store ramp.

    `monthly` needs tcin, month_start, stream, units, grade. Every other stream passes
    through untouched, and so does any (tcin, month) the ramp has no row for.
    """
    m = monthly.copy()
    m["tcin"] = m["tcin"].astype("int64")
    r = ramp[["tcin", "month_start", "ramp", "ramp_flag", "bm_stores", "bm_stores_anchor"]]
    out = m.merge(r, on=["tcin", "month_start"], how="left")
    out["ramp"] = out["ramp"].astype(float).fillna(1.0)
    out["ramp_flag"] = out["ramp_flag"].fillna("")
    scalable = out["stream"].isin(SCALABLE_STREAMS)
    out["units_bpd"] = out["units"].astype(float)
    out["units"] = out["units_bpd"].where(~scalable, out["units_bpd"] * out["ramp"])
    moved = scalable & ((out["ramp"] - 1.0).abs() > RAMP_MATERIAL)
    out["authority"] = AUTH_MEASURED
    out.loc[moved, "authority"] = AUTH_SHAPED
    out["grade"] = out["grade"].where(~moved, out["grade"].map(_worse))
    return out


def bm_only_rows(
    bm: pd.DataFrame,
    covered: set[tuple[int, pd.Timestamp]],
    *,
    months: list[pd.Timestamp] | None = None,
) -> pd.DataFrame:
    """B&M's own `Velocity` for TCIN-months no BPD row reaches. Graded E, always.

    This is the §7 forward-reach regression of the dist_velocity design filled in from a
    stated plan. It is E and never better for the reason that design doc gave the curated
    seed: a human assumption about an item with no history must not inherit the D grade
    the retired owner sheet's rows carried, because that authority level is precisely
    what the change removed.
    """
    b = bm.loc[bm["tcin"].notna()].copy()
    b["tcin"] = b["tcin"].astype("int64")
    if months is not None:
        b = b.loc[b["month_start"].isin(months)]
    keep = [
        not ((int(r.tcin), pd.Timestamp(r.month_start)) in covered)
        for r in b.itertuples(index=False)
    ]
    b = b.loc[keep]
    b = b.loc[b["bm_velocity"].astype(float) > 0]
    if b.empty:
        return pd.DataFrame(
            columns=["tcin", "month_start", "stream", "units", "grade", "authority", "ramp_flag"]
        )
    out = pd.DataFrame(
        {
            "tcin": b["tcin"].to_numpy(),
            "month_start": b["month_start"].to_numpy(),
            "stream": STREAM_REPLEN,
            "units": b["bm_velocity"].astype(float).to_numpy(),
            "units_bpd": 0.0,
            "grade": "E",
            "authority": AUTH_STATED,
            "ramp": pd.Series([float("nan")] * len(b), dtype="float64").to_numpy(),
            "ramp_flag": "BM_ONLY_NO_BPD_SIGNAL",
            "bm_stores": b["bm_stores"].astype(float).to_numpy(),
            "bm_stores_anchor": pd.Series([float("nan")] * len(b), dtype="float64").to_numpy(),
        }
    )
    return out


def load_order_verdicts(
    bm: pd.DataFrame,
    bpd_launch: pd.DataFrame,
    *,
    months: list[pd.Timestamp],
    tolerance: float = LOAD_TOLERANCE,
) -> pd.DataFrame:
    """Cross-check B&M `Load_Orders` against the BPD-carried launch/forward units.

    `bpd_launch` needs tcin, month_start, bpd_units (booked_forward + planned_launch +
    planned_forward for that month). `months` is the OVERLAP WINDOW and is required, not
    optional: the sheet carries 48 months back to Jan-2025 while a published run carries
    16 forward, and comparing the two unwindowed reports the sheet's whole launch history
    as units BPD is missing. Live that was 21 spurious BM_LOAD_ONLY verdicts, including
    a 318,458-unit Mar-2026 load that shipped long before the run's first month.

    The verdict is per TCIN, on the TOTAL over that window, because B&M is systematically
    one month late; a month-by-month comparison would call a timing difference a units
    disagreement. Timing is reported separately, by name (BM_LOAD_MONTH_SHIFTED).
    """
    window = set(pd.Timestamp(m) for m in months)
    if not window:
        raise ValueError("load_order_verdicts needs a non-empty overlap window")
    b = bm.loc[bm["tcin"].notna(), ["tcin", "bm_sku", "month_start", "bm_load_orders"]].copy()
    b["tcin"] = b["tcin"].astype("int64")
    b = b.loc[b["month_start"].isin(window) & (b["bm_load_orders"].astype(float) > 0)]
    p = bpd_launch.copy()
    p["tcin"] = p["tcin"].astype("int64")
    p = p.loc[p["month_start"].isin(window) & (p["bpd_units"].astype(float) > 0)]

    verdict_cols = [
        "tcin", "bm_sku", "bm_load_units", "bpd_load_units", "gap_pct",
        "bm_months", "bpd_months", "verdict", "note",
    ]
    tcins = sorted(set(b["tcin"]) | set(p["tcin"]))
    rows: list[dict] = []
    for t in tcins:
        bt = b.loc[b["tcin"] == t]
        pt = p.loc[p["tcin"] == t]
        bu = float(bt["bm_load_orders"].sum())
        pu = float(pt["bpd_units"].sum())
        bm_months = sorted(pd.Timestamp(x).date().isoformat()[:7] for x in bt["month_start"])
        pd_months = sorted(pd.Timestamp(x).date().isoformat()[:7] for x in pt["month_start"])
        sku = bt["bm_sku"].iloc[0] if len(bt) else ""
        if pu <= 0:
            verdict, note = "BM_LOAD_ONLY", "B&M states a load BPD carries nothing for"
        elif bu <= 0:
            verdict, note = "BPD_LOAD_ONLY", "BPD carries a load B&M does not state"
        else:
            gap = (bu - pu) / pu
            if abs(gap) <= tolerance:
                verdict = "BM_LOAD_AGREES" if bm_months == pd_months else "BM_LOAD_MONTH_SHIFTED"
                note = (
                    "units agree within tolerance"
                    if bm_months == pd_months
                    else f"units agree; B&M months {bm_months} vs BPD {pd_months}"
                )
            else:
                verdict = "BM_LOAD_DISAGREES"
                note = f"B&M {bu:,.0f} vs BPD {pu:,.0f} ({gap:+.1%})"
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
            }
        )
    return pd.DataFrame(rows, columns=verdict_cols)


def demo() -> None:
    """Self-check: the three refusals, the scaling rule and the load-order verdicts."""
    months = [pd.Timestamp("2026-09-01"), pd.Timestamp("2027-06-01")]
    bm = pd.DataFrame(
        [
            # a live item whose plan ramps 1,575 -> 1,950
            dict(tcin=1, bm_sku="LIVE", month_start=months[0], bm_stores=1575.0, bm_upspw=1.0,
                 bm_velocity=6750.0, bm_load_orders=0.0, bm_placeholder=False),
            dict(tcin=1, bm_sku="LIVE", month_start=months[1], bm_stores=1950.0, bm_upspw=1.0,
                 bm_velocity=8357.0, bm_load_orders=0.0, bm_placeholder=False),
            # a placeholder block: 1 door wherever live
            dict(tcin=2, bm_sku="PLACE", month_start=months[0], bm_stores=1.0, bm_upspw=12.0,
                 bm_velocity=51.0, bm_load_orders=0.0, bm_placeholder=True),
            dict(tcin=2, bm_sku="PLACE", month_start=months[1], bm_stores=1.0, bm_upspw=12.0,
                 bm_velocity=51.0, bm_load_orders=0.0, bm_placeholder=True),
            # pre-launch: no anchor, then 1,500 doors
            dict(tcin=3, bm_sku="PRE", month_start=months[0], bm_stores=0.0, bm_upspw=0.0,
                 bm_velocity=0.0, bm_load_orders=41000.0, bm_placeholder=False),
            dict(tcin=3, bm_sku="PRE", month_start=months[1], bm_stores=1500.0, bm_upspw=1.5,
                 bm_velocity=9642.0, bm_load_orders=0.0, bm_placeholder=False),
        ]
    )
    r = store_ramp(bm, months[0])
    got = {(int(x.tcin), x.month_start): (round(float(x.ramp), 4), x.ramp_flag) for x in r.itertuples()}
    assert got[(1, months[0])] == (1.0, ""), got[(1, months[0])]
    assert got[(1, months[1])] == (round(1950 / 1575, 4), ""), got[(1, months[1])]
    assert got[(2, months[1])] == (1.0, "BM_RAMP_REFUSED_PLACEHOLDER"), got[(2, months[1])]
    assert got[(3, months[1])] == (1.0, "BM_RAMP_NO_ANCHOR"), got[(3, months[1])]

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
    # the replenishment stream is scaled and pays one grade for it
    assert round(float(rep.units), 2) == round(1000 * 1950 / 1575, 2), rep.units
    assert rep.grade == "E" and rep.authority == AUTH_SHAPED, (rep.grade, rep.authority)
    # a booked PO is a fact: never scaled, never regraded
    assert float(bk.units) == 500.0 and bk.grade == "A" and bk.authority == AUTH_MEASURED
    # a refused ramp changes nothing at all
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

    # the window is load-bearing: a load outside it is not a disagreement
    v2 = load_order_verdicts(
        bm,
        pd.DataFrame([dict(tcin=3, month_start=months[0], bpd_units=41000.0)]),
        months=[months[1]],
    )
    assert list(v2["verdict"]) == [], list(v2["verdict"])
    print("bm_combine demo: all assertions passed")


if __name__ == "__main__":
    demo()
