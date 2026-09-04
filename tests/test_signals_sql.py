from __future__ import annotations

import re
from datetime import date

import pandas as pd
import pytest

from shipcast.channels.target import signals

BULLSEYE_BODIES = {
    "orders_daily",
    "po_plan_daily",
    "sales_weekly",
    "inventory_weekly",
    "forecast_weekly",
}


@pytest.mark.parametrize("name", sorted(signals.ALL_SQL))
def test_every_sql_names_its_bullseye_body(name: str) -> None:
    sql = signals.ALL_SQL[name]()
    comment_lines = [ln for ln in sql.splitlines() if ln.strip().startswith("--")]
    assert comment_lines, f"{name} has no SQL comment"
    joined = " ".join(comment_lines)
    assert any(f"`{b}`" in joined or b in joined for b in BULLSEYE_BODIES), (
        f"{name} comment names no bullseye body"
    )


def test_raw_exceptions_are_filtered() -> None:
    assert "@business_dates" in signals.plan_snapshot_sql()
    assert "dly_po_plan_tcin" in signals.plan_snapshot_sql()
    assert "LAST_UPDATE_D <= @as_of" in signals.dfe_asof_sql()
    assert "QUALIFY" in signals.dfe_asof_sql()
    with pytest.raises(ValueError):
        signals.plan_snapshots([])


def test_logical_injection_inherits_orders_qualify() -> None:
    pytest.importorskip("bpd_mcp")
    from shipcast import bq

    built = bq.logical(signals.orders_latest_sql())
    assert re.search(r"WITH\s+orders_daily\s+AS", built), "orders_daily CTE not injected"
    assert "QUALIFY ROW_NUMBER() OVER" in built
    assert "TO_JSON_STRING(o) ASC" in built
    # raw exceptions pass through untouched
    assert bq.logical(signals.plan_snapshot_sql()) == signals.plan_snapshot_sql()
    assert bq.logical(signals.dfe_asof_sql()) == signals.dfe_asof_sql()
    names = bq.logical_names()
    assert {"orders_daily", "po_plan_daily", "sales_weekly", "inventory_weekly"} <= names


def test_runner_injection_and_params() -> None:
    seen: dict[str, object] = {}

    def fake(sql: str, params: dict[str, object] | None = None) -> pd.DataFrame:
        seen["sql"], seen["params"] = sql, params
        return pd.DataFrame({"business_d": pd.to_datetime(["2026-08-29"])})

    out = signals.plan_business_dates(date(2026, 8, 30), limit=3, run=fake)
    assert out == [date(2026, 8, 29)]
    assert seen["params"] == {"as_of": date(2026, 8, 30), "limit": 3}
    signals.plan_snapshots([date(2026, 8, 29), date(2026, 8, 22)], run=fake)
    assert seen["params"] == {"business_dates": [date(2026, 8, 22), date(2026, 8, 29)]}
