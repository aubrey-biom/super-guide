"""Model layer tests on hand-built frames and the committed panel."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from shipcast.channels.target.calendar import TargetCalendar
from shipcast.config import load_config
from shipcast.model import consumption, grade, intervals, plan_anchor


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
    from shipcast.backtest.rolling import panel_to_long
    from shipcast.config import repo_root

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


def test_grade_month_rules() -> None:
    assert consumption.grade_month(0, 1.0, owner_only_pos=False, has_history=True) == "B"
    assert consumption.grade_month(1, 0.5, owner_only_pos=False, has_history=True) == "C"
    assert consumption.grade_month(4, 0.0, owner_only_pos=False, has_history=True) == "C"
    assert consumption.grade_month(8, 0.0, owner_only_pos=False, has_history=True) == "D"
    assert consumption.grade_month(13, 0.0, owner_only_pos=False, has_history=True) == "E"
    assert consumption.grade_month(4, 0.0, owner_only_pos=True, has_history=False) == "D"
