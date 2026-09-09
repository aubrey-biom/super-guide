from __future__ import annotations

import pandas as pd
import pytest

from pipelines.target_shipment_forecast.backtest.leakage import LeakageError, assert_no_leakage, find_leaks
from pipelines.target_shipment_forecast.backtest.rolling import panel_to_long


def test_leak_detected() -> None:
    df = pd.DataFrame(
        {
            "origin": pd.to_datetime(["2026-05-09", "2026-05-09", "2026-05-16"]),
            "source_ts": pd.to_datetime(["2026-05-09", "2026-05-10", None]),
            "signal": ["plan", "plan", "lag1"],
        }
    )
    leaks = find_leaks(df)
    assert len(leaks) == 1 and leaks.iloc[0]["source_ts"] == pd.Timestamp("2026-05-10")
    with pytest.raises(LeakageError):
        assert_no_leakage(df)


def test_committed_panel_has_no_leakage(fixtures_dir) -> None:  # type: ignore[no-untyped-def]
    panel = pd.read_csv(fixtures_dir / "signal_panel.csv")
    rows = panel_to_long(panel)
    assert len(rows) > 0
    assert_no_leakage(rows)
    # the plan snapshot used for week W is the Saturday before it (lead 1 day for horizon 1)
    plan_h1 = rows[(rows["signal"] == "plan_sat_ordered") & (rows["horizon"] == 1)]
    assert (plan_h1["lead_days"] >= 1).all()
    assert plan_h1["lead_days"].mode().iloc[0] == 1
