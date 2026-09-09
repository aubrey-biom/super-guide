from __future__ import annotations

import math

import pandas as pd
import pytest

from pipelines.target_shipment_forecast.backtest.scoring import (
    bias,
    exact_share,
    median_ape,
    score,
    score_table,
    wape,
    week_block_bootstrap_ci,
    within_share,
)

A = pd.Series([100.0, 0.0, 50.0, 0.0, 200.0])
F = pd.Series([90.0, 10.0, 50.0, 0.0, 250.0])


def test_wape_hand_computed() -> None:
    # |90-100| + |10-0| + 0 + 0 + |250-200| = 70 ; sum actual = 350
    assert wape(A, F) == pytest.approx(70 / 350)


def test_bias_hand_computed() -> None:
    # (400 - 350) / 350
    assert bias(A, F) == pytest.approx(50 / 350)


def test_median_ape_only_where_actual_positive() -> None:
    # APEs over a>0: 0.1, 0.0, 0.25 -> median 0.1
    assert median_ape(A, F) == pytest.approx(0.1)


def test_shares_exclude_zero_zero_rows() -> None:
    # scored rows: 4 (the 0/0 row is excluded); exact: the 50/50 row only -> 1/4
    assert exact_share(A, F) == pytest.approx(0.25)
    # within 10%: 90 vs 100 (yes), 10 vs 0 (no), 50 (yes), 250 vs 200 (no) -> 2/4
    assert within_share(A, F) == pytest.approx(0.5)


def test_nan_when_no_actuals() -> None:
    assert math.isnan(wape(pd.Series([0.0, 0.0]), pd.Series([1.0, 2.0])))
    assert math.isnan(median_ape(pd.Series([0.0]), pd.Series([1.0])))
    assert math.isnan(exact_share(pd.Series([0.0]), pd.Series([0.0])))


def test_score_and_table() -> None:
    s = score(A, F)
    assert s["n"] == 5 and s["n_scored"] == 4
    df = pd.DataFrame({"a": A, "f": F, "g": ["x", "x", "y", "y", "y"]})
    t = score_table(df, actual_col="a", forecast_col="f", by=["g"])
    assert set(t["g"]) == {"x", "y"}
    assert t.loc[t["g"] == "x", "wape"].iloc[0] == pytest.approx(20 / 100)


def test_week_block_bootstrap_ci_brackets_point() -> None:
    weeks = pd.to_datetime(
        ["2026-05-10"] * 3 + ["2026-05-17"] * 3 + ["2026-05-24"] * 3 + ["2026-05-31"] * 3
    )
    df = pd.DataFrame(
        {
            "wk": weeks,
            "a": [100, 50, 20] * 4,
            "f": [110, 40, 20, 90, 60, 25, 100, 50, 30, 120, 45, 15],
        }
    )
    point, lo, hi = week_block_bootstrap_ci(
        df, week_col="wk", actual_col="a", forecast_col="f", n_boot=200, seed=1
    )
    assert point == pytest.approx(wape(df["a"], df["f"]))
    assert lo <= point <= hi
    assert lo >= 0
