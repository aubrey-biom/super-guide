"""Model layer tests on hand-built frames and the committed panel."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from pipelines.target_shipment_forecast.channels.target.calendar import TargetCalendar
from pipelines.target_shipment_forecast.config import load_config
from pipelines.target_shipment_forecast.model import consumption, grade, intervals, plan_anchor


@pytest.fixture
def calendar() -> TargetCalendar:
    return TargetCalendar.from_config(load_config())


def _plan(rows: list[tuple[str, int, str, int, int]]) -> pd.DataFrame:
    return pd.DataFrame(
        rows, columns=["business_d", "tcin", "order_d", "receiving_location_id", "ordered_q"]
    ).assign(
        business_d=lambda d: pd.to_datetime(d["business_d"]),
        order_d=lambda d: pd.to_datetime(d["order_d"]),
    )


def test_freshest_snapshot_never_after_order_day_or_as_of() -> None:
    plan = _plan(
        [
            ("2026-09-04", 1, "2026-09-06", 100, 48),  # Fri file for Sun order: lead 2
            ("2026-09-05", 1, "2026-09-06", 100, 60),  # Sat file: lead 1 -> chosen
            (
                "2026-09-07",
                1,
                "2026-09-06",
                100,
                0,
            ),  # Mon file after the order day: ignored (order already cut)
            ("2026-09-05", 1, "2026-09-13", 100, 24),  # next week: lead 8 from Sat file
        ]
    )
    best = plan_anchor.freshest_plan_by_order_day(plan, as_of=date(2026, 9, 7))
    row = best[best["order_d"] == "2026-09-06"].iloc[0]
    assert row["ordered_q"] == 60 and row["lead_days"] == 1
    assert best[best["order_d"] == "2026-09-13"].iloc[0]["lead_days"] == 8
    # as_of before the Saturday file: only the Friday file is eligible
    best2 = plan_anchor.freshest_plan_by_order_day(plan, as_of=date(2026, 9, 4))
    assert best2[best2["order_d"] == "2026-09-06"].iloc[0]["ordered_q"] == 48


def test_expected_po_units_rungs_and_flags(calendar: TargetCalendar) -> None:
    plan = _plan(
        [
            ("2026-09-05", 1, "2026-09-06", 100, 120),
            ("2026-09-05", 1, "2026-09-06", 200, 60),
            ("2026-09-05", 3, "2026-09-07", 100, 5000),
        ]
    )
    actuals = pd.DataFrame(
        {
            "wk": pd.to_datetime(["2026-08-09", "2026-08-16", "2026-08-23", "2026-08-30"] * 2),
            "tcin": [1, 1, 1, 1, 2, 2, 2, 2],
            "act_rep": [100, 110, 90, 100, 40, 0, 50, 45],
            "act_fwd": 0,
        }
    )
    out = plan_anchor.expected_po_units(
        plan, actuals, as_of=date(2026, 9, 6), horizon_weeks=2, calendar=calendar
    )
    wk1 = out[out["po_week"] == "2026-09-06"].set_index("tcin")
    assert (
        wk1.loc[1, "expected_po_units"] == 180
        and wk1.loc[1, "fallback_rung"] == "plan"
        and wk1.loc[1, "lead_days"] == 1
    )
    assert (
        wk1.loc[2, "fallback_rung"] == "plan_zero_history_positive"
        and wk1.loc[2, "expected_po_units"] == 0
    )
    assert "NEW_TCIN_NO_HISTORY" in wk1.loc[3, "flags"]
    wk2 = out[out["po_week"] == "2026-09-13"].set_index("tcin")
    assert (
        wk2.loc[1, "fallback_rung"] == "beyond_plan_horizon"
        and wk2.loc[1, "expected_po_units"] == 100
    )  # mean4


def test_grades_from_lead_and_rung() -> None:
    buckets = load_config()["grades"]["by_lead_days"]
    assert grade.grade_for_lead_days(0, buckets) == "A"
    assert grade.grade_for_lead_days(3, buckets) == "B"
    assert grade.grade_for_lead_days(9, buckets) == "C"
    df = pd.DataFrame(
        {
            "lead_days": [1, 5, None],
            "fallback_rung": ["plan", "plan", "beyond_plan_horizon"],
            "expected_po_units": [100, 100, 100],
        }
    )
    g = grade.grade_rows(df, buckets)
    assert list(g["grade"]) == ["A", "C", "D"]
    c = grade.attach_confidence(
        g, grade.labels_from_config(load_config()), value_col="expected_po_units"
    )
    assert (
        c.loc[0, "low"] == 85 and c.loc[0, "high"] == 115 and c.loc[0, "confidence"].startswith("A")
    )


def test_intervals_fit_and_coverage_on_panel() -> None:
    from pipelines.target_shipment_forecast.backtest.rolling import panel_to_long
    from pipelines.target_shipment_forecast.config import repo_root

    panel = pd.read_csv(repo_root() / "tests" / "fixtures" / "signal_panel.csv")
    rows = panel_to_long(panel)
    rows = rows[rows["signal"] == "plan_sat_ordered"]
    fitted = intervals.fit_ratio_quantiles(rows, min_pool_rows=100)
    assert set(fitted["bucket"]) == {"pooled", "A", "B", "C"}
    pooled = fitted[fitted["bucket"] == "pooled"].iloc[0]
    assert pooled["q_lo"] < 0 < pooled["q_hi"]
    cov = intervals.lowo_coverage(rows, min_pool_rows=100)
    p = cov[cov["bucket"] == "pooled"].iloc[0]
    assert 0.6 <= p["coverage"] <= 0.95  # an 80% band should land near 80% out of sample


def test_weekly_to_monthly_splits_by_days() -> None:
    weekly = pd.DataFrame(
        {"tcin": [1], "week_start": pd.to_datetime(["2026-08-30"]), "units": [70.0]}
    )
    m = consumption.weekly_to_monthly(weekly, week_col="week_start", value_cols=["units"])
    aug = m[m["month_start"] == "2026-08-01"]["units"].iloc[0]
    sep = m[m["month_start"] == "2026-09-01"]["units"].iloc[0]
    assert aug == 20.0 and sep == 50.0  # Aug 30-31 = 2 days, Sep 1-5 = 5 days


def test_controller_calibration_picks_low_objective() -> None:
    rng = np.random.default_rng(0)
    weeks = pd.date_range("2026-05-10", periods=14, freq="7D")
    rows = []
    for t in range(1, 6):
        pos = 1000 + 100 * t
        oh = 12 * pos
        for w in weeks:
            po = max(0.0, pos + (8 * pos - oh) / 6) * (1 + rng.normal(0, 0.05))
            rows.append(
                {"tcin": t, "week_start": w, "pos": pos, "pos4": pos, "oh_prev": oh, "po": po}
            )
            oh = max(0.0, oh + po - pos)
    panel = pd.DataFrame(rows)
    ctrl, grid = consumption.calibrate_controller(
        panel, bands=(6, 8, 10), ks=(3, 6, 12), min_rows=20
    )
    assert (ctrl.band, ctrl.k) == (8.0, 6.0)
    assert grid.iloc[0]["objective"] == pytest.approx(grid["objective"].min())


def test_simulation_steps_up_when_band_reached(calendar: TargetCalendar) -> None:
    weekly = pd.DataFrame(columns=["po_week", "tcin", "expected_po_units", "fallback_rung"])
    pos_month = pd.DataFrame(
        {
            "tcin": [1] * 4,
            "month_start": pd.to_datetime(["2026-09-01", "2026-10-01", "2026-11-01", "2026-12-01"]),
            "pos_units": [4000.0] * 4,
        }
    )
    rr = pd.DataFrame(
        {
            "tcin": [1],
            "pos_wk": [1000.0],
            "weeks_used": [8],
            "last_week": [pd.Timestamp("2026-08-30")],
        }
    )
    oh = pd.DataFrame(
        {"tcin": [1], "on_hand": [20000.0], "as_of_week": [pd.Timestamp("2026-08-29")]}
    )
    actuals = pd.DataFrame(
        {
            "wk": pd.to_datetime(["2026-08-16", "2026-08-23", "2026-08-30"]),
            "tcin": 1,
            "act_rep": [200, 200, 200],
        }
    )
    ctrl = consumption.Controller(8.0, 4.0, np.nan, np.nan, 0, "test")
    sim = consumption.simulate(
        weekly_forecast=weekly,
        pos_month=pos_month,
        runrate_tbl=rr,
        on_hand=oh,
        actuals=actuals,
        booked_receipts=pd.DataFrame(columns=["tcin", "week_start", "units"]),
        controller=ctrl,
        as_of=date(2026, 9, 6),
        weeks_ahead=16,
        calendar=calendar,
    )
    assert (sim["source"] == "controller").all()
    first, last = sim.iloc[0], sim.iloc[-1]
    assert first["orders"] < first["sales"] * 0.5  # overhang: orders well below sales
    assert last["orders"] > last["sales"] * 0.85  # band reached: orders converge to sales
    assert last["oh_end"] < first["oh_start"]


def test_create_month_vs_ship_month_identity(calendar: TargetCalendar) -> None:
    """create_month = ship_month - carried in + carried out, on the real Sep-26 numbers.

    biom_sql fix (a): the upstream guide described the create-month figure as the
    ship-month figure PLUS the last week of the month (6,480 + 8,904 = 15,384), which
    is wrong by the 552 units created in the week of 30 Aug and shipped 4 Sep. This
    pins the code's actual behaviour so no document can drift from it again.
    """
    # The 4 Oct week is present with zero orders because `monthly_from_simulation`
    # bases its frame on the CREATE-month groupby and left-joins ship months: a month
    # with no create weeks gets no row at all. Production never hits that (the
    # simulation runs `months * 4.5 + 4` weeks, deliberately past the displayed months)
    # but a fixture has to supply it.
    weeks = pd.to_datetime(
        ["2026-08-30", "2026-09-06", "2026-09-13", "2026-09-20", "2026-09-27", "2026-10-04"]
    )
    orders = [552.0, 1656.0, 1872.0, 2400.0, 8904.0, 0.0]
    sim = pd.DataFrame(
        {
            "tcin": 94928292,
            "week_start": weeks,
            "sales": 0.0,
            "orders": orders,
            "receipts": 0.0,
            "oh_start": 0.0,
            "oh_end": 0.0,
            "source": "plan",
        }
    )
    months = consumption.months_from(date(2026, 9, 1), 2)
    out = consumption.monthly_from_simulation(
        sim,
        months=months,
        calendar=calendar,
        ship_offset_days={94928292: 5},   # cleaning: Sunday + 5 d
        pos_blend=pd.DataFrame(),
        runrate_tbl=pd.DataFrame(),
    )
    sep = out[out["month_start"] == pd.Timestamp("2026-09-01")].iloc[0]
    assert sep["expected_ship_units"] == 6480      # 552 (created Aug, ships 4 Sep) + 3 plan weeks
    assert sep["expected_po_units"] == 14832       # 4 weeks whose Sunday falls in September
    carried_in, carried_out = 552, 8904
    assert sep["expected_po_units"] == sep["expected_ship_units"] - carried_in + carried_out
    assert sep["expected_po_units"] != sep["expected_ship_units"] + carried_out  # the guide's error
    oct_ = out[out["month_start"] == pd.Timestamp("2026-10-01")].iloc[0]
    assert oct_["expected_ship_units"] == carried_out   # the 27 Sep week ships 2 Oct
    assert oct_["expected_po_units"] == 0               # the 4 Oct week orders nothing here
    # Conserved apart from the horizon edge: the two bucketings redistribute the same
    # units, but the 552's CREATE month is August, which the displayed months exclude —
    # so the create column is under-inclusive by exactly the carry-in at month 1.
    # (15,384 = the displayed ship total, and is also the number the upstream guide
    # published as Sept's create-month figure.)
    assert out["expected_ship_units"].sum() == 15384
    assert out["expected_po_units"].sum() == out["expected_ship_units"].sum() - carried_in


def test_grade_month_rules() -> None:
    assert consumption.grade_month(0, 1.0, pos_thin=False, has_history=True) == "B"
    assert consumption.grade_month(1, 0.5, pos_thin=False, has_history=True) == "C"
    assert consumption.grade_month(4, 0.0, pos_thin=False, has_history=True) == "C"
    assert consumption.grade_month(8, 0.0, pos_thin=False, has_history=True) == "D"
    assert consumption.grade_month(13, 0.0, pos_thin=False, has_history=True) == "E"
    # `pos_thin` replaced `owner_only_pos` on 2026-09-08 and carries the same penalty:
    # one grade worse, floored at C inside 2 months and at D beyond.
    assert consumption.grade_month(4, 0.0, pos_thin=True, has_history=False) == "D"
    assert consumption.grade_month(1, 0.0, pos_thin=True, has_history=True) == "C"


# --------------------------------------------------------------------------------------
# dist_velocity: the BPD-derived monthly POS forecast that replaced owner_velocity
# --------------------------------------------------------------------------------------


def _sales_weekly(rows: list[tuple[str, int, float, float]]) -> pd.DataFrame:
    """`week_start_d, week_end_d, tcin, units, locations` from (week_start, tcin, units, locs)."""
    return pd.DataFrame(
        [
            {
                "week_start_d": pd.Timestamp(w),
                "week_end_d": pd.Timestamp(w) + pd.Timedelta(days=6),
                "tcin": t,
                "units": u,
                "locations": loc,
            }
            for w, t, u, loc in rows
        ]
    )


def test_dist_velocity_is_stores_times_rate_and_projects_the_ramp() -> None:
    """The identity, the forward ramp, and the cap — on numbers checkable by hand.

    Eight complete weeks ending Sun 24 Aug (week_start), origin 1 Sep 2026. One TCIN
    ramping 100 -> 800 selling stores at a flat 2.0 units per selling store per week.
    """
    weeks = [pd.Timestamp("2026-06-28") + pd.Timedelta(days=7 * i) for i in range(9)]
    stores = [100, 200, 300, 400, 500, 600, 700, 800, 900]
    rows = [(w.date().isoformat(), 111, 2.0 * s, s) for w, s in zip(weeks[:8], stores[:8], strict=True)]
    sales = _sales_weekly(rows)
    as_of = date(2026, 9, 1)

    state = consumption.distribution_state(sales, as_of, weeks=8, slope_weeks=3)
    assert len(state) == 1
    st = state.iloc[0]
    # 2 units/store/week held exactly, because the ratio is summed over the window
    assert st["upspw"] == pytest.approx(2.0)
    assert st["stores_last"] == 800          # week of 16 Aug is the last COMPLETE week
    assert st["last_week"] == pd.Timestamp("2026-08-16")
    assert st["slope_per_week"] == pytest.approx(100.0)   # (800 - 500) / 3
    assert st["stores_peak"] == 800

    fc, _ = consumption.dist_velocity(
        sales, as_of=as_of, months=[pd.Timestamp("2026-09-01")], trailing_weeks=8, ramp_cap=1.15
    )
    r = fc.iloc[0]
    # ramp from the 16 Aug week to mid-September is 4.0 weeks: 800 + 100*4 = 1200,
    # capped at 1.15 * 800 = 920. The cap is what stops a launch ramp leaving the chain.
    assert r["stores_fwd"] == pytest.approx(920.0)
    assert bool(r["dist_capped"]) is True
    assert consumption.POS_DIST_CAPPED in r["flags"]
    assert r["pos_units"] == pytest.approx(2.0 * 920.0 * 30 / 7)


def test_dist_velocity_emits_no_row_rather_than_zero_for_a_new_tcin() -> None:
    """The NEW_TCIN_NO_POS_HISTORY contract: absence, never a zero.

    A zero would flow through `monthly_from_simulation`'s coalesce as a real forecast
    of no sales; an absent row falls back to the simulated path and is reported on
    Exceptions instead. This is the whole reason the fallback is a category.
    """
    weeks = [pd.Timestamp("2026-06-28") + pd.Timedelta(days=7 * i) for i in range(8)]
    sales = _sales_weekly(
        [(w.date().isoformat(), 111, 200.0, 100) for w in weeks]
        + [(w.date().isoformat(), 222, 0.0, 0) for w in weeks]   # never sold anywhere
    )
    fc, _ = consumption.dist_velocity(
        sales,
        as_of=date(2026, 9, 1),
        months=[pd.Timestamp("2026-09-01")],
        trailing_weeks=8,
    )
    assert set(fc["tcin"]) == {111}, "a TCIN with no selling stores must get NO row"
    assert (fc["pos_units"] > 0).all()


def _spiky_sales(n_weeks: int) -> pd.DataFrame:
    """One TCIN, flat 500 selling stores, a deliberate 2x August spike, from 2025-01-05."""
    rows = []
    for i in range(n_weeks):
        w = pd.Timestamp("2025-01-05") + pd.Timedelta(days=7 * i)
        rows.append((w.date().isoformat(), 111, 1000.0 * (2.0 if w.month == 8 else 1.0), 500))
    return _sales_weekly(rows)


def test_seasonal_index_refuses_a_single_year_but_fires_on_two() -> None:
    """The `n_years >= 2` gate is the honesty mechanism, so it is pinned in BOTH directions.

    A month observed in one year only has an index that cannot be separated from that
    year's trend — which is exactly BIOM's position today, with a Target footprint that
    grew ~20x over the 20 months bpd_raw holds. So one year must yield exactly 1.000
    while still PRINTING the spike it refused to use; two years must admit it.
    """
    # one year (2025 only), as-of the following January
    idx, diag = consumption.seasonal_index(
        _spiky_sales(52), date(2026, 1, 1), min_obs=1, min_years=2
    )
    assert set(idx.values()) == {1.0}, f"no month may be applied on one year: {idx}"
    assert not diag.empty and not diag["applied"].any()
    assert (diag["n_years"] == 1).all()
    aug = diag[diag["month_of_year"] == 8]
    assert not aug.empty and float(aug["raw_index"].iloc[0]) > 1.2, "the spike must still be shown"

    # two years (2025 + 2026), as-of 2027: August now has n_years 2 and is admitted
    idx2, diag2 = consumption.seasonal_index(
        _spiky_sales(104), date(2027, 1, 1), min_obs=2, min_years=2
    )
    aug2 = diag2[diag2["month_of_year"] == 8].iloc[0]
    assert int(aug2["n_years"]) == 2 and bool(aug2["applied"]) is True
    assert idx2[8] > 1.0 and idx2[8] == pytest.approx(float(aug2["shrunk_index"]))
    # shrinkage pulls it toward 1: the admitted factor is smaller than the raw ratio
    assert idx2[8] < float(aug2["raw_index"])


def test_planned_launch_and_planned_forward_are_mutually_exclusive(
    calendar: TargetCalendar,
) -> None:
    """The V-2 split: one plan row can be a launch fill OR a forward buy, never both.

    Before 2026-09-08 both tests OR-ed into `planned_forward`, so a launch pipeline-fill
    was labelled a forward buy. Two TCINs, same 20,000-unit plan week: 777 sells 100/wk
    with a PO history (a genuine 3x forward buy), 888 has never sold (a launch).
    """
    plan = _plan(
        [
            ("2026-09-05", 777, "2026-09-06", 551, 20000),
            ("2026-09-05", 888, "2026-09-06", 551, 20000),
        ]
    )
    hist_weeks = ["2026-08-09", "2026-08-16", "2026-08-23", "2026-08-30"]
    actuals = pd.DataFrame(
        {
            "wk": pd.to_datetime(hist_weeks),
            "tcin": [777] * 4,
            "act_rep": [500.0] * 4,
            "act_fwd": 0,
        }
    )
    out = plan_anchor.expected_po_units(
        plan,
        actuals,
        as_of=date(2026, 9, 6),
        horizon_weeks=1,
        calendar=calendar,
        tcins=[777, 888],
        item_state={},                      # neither is READY_FOR_ORDER: history decides
        pos_runrate={777: 100.0},           # 888 has no POS at all
        planned_forward_multiple=3.0,
    )
    row = {int(r.tcin): r for r in out[out["expected_po_units"] > 0].itertuples(index=False)}
    assert row[777].stream == plan_anchor.STREAM_PLANNED_FORWARD
    assert plan_anchor.FLAG_PLANNED_FORWARD in row[777].flags
    assert row[888].stream == plan_anchor.STREAM_PLANNED_LAUNCH
    assert plan_anchor.FLAG_LAUNCH_FILL in row[888].flags
    # mutually exclusive, in both directions
    assert plan_anchor.FLAG_LAUNCH_FILL not in row[777].flags
    assert plan_anchor.FLAG_PLANNED_FORWARD not in row[888].flags
