from __future__ import annotations

import re
from datetime import date

import pandas as pd
import pytest

from pipelines.target_shipment_forecast.channels.target import signals

LOGICAL_BODIES = {
    "orders_daily",
    "po_plan_daily",
    "sales_weekly",
    "inventory_weekly",
    "forecast_weekly",
    "item_attr_extended",
}


# Statements with no logical body, and why. `launch_seed` reads a biom_sql-owned seed
# (biom_admin.seed_target_launch_velocity), not a BPD feed, so there is nothing to
# inject; it is named here rather than left to pass this test by an incidental mention
# of `orders_daily` in its comment.
NO_LOGICAL_BODY = {"launch_seed"}


@pytest.mark.parametrize("name", sorted(signals.ALL_SQL))
def test_every_sql_names_its_logical_body(name: str) -> None:
    sql = signals.ALL_SQL[name]()
    comment_lines = [ln for ln in sql.splitlines() if ln.strip().startswith("--")]
    assert comment_lines, f"{name} has no SQL comment"
    joined = " ".join(comment_lines)
    if name in NO_LOGICAL_BODY:
        assert "biom_admin" in sql, f"{name} claims no logical body but reads no seed either"
        return
    assert any(f"`{b}`" in joined or b in joined for b in LOGICAL_BODIES), (
        f"{name} comment names no logical body"
    )


def test_launch_seed_is_as_of_filtered_and_totally_ordered() -> None:
    """An append-only seed is only an archive if the read is as-of filtered."""
    sql = signals.launch_seed_sql()
    assert "snapshot_date <= @as_of" in sql
    assert "QUALIFY ROW_NUMBER() OVER" in sql
    # snapshot_date alone is not a total order within a snapshot; source_row breaks ties
    assert "ORDER BY snapshot_date DESC, source_row DESC" in sql
    assert signals.launch_seed_sql() == signals.ALL_SQL["launch_seed"]()


def test_raw_exceptions_are_filtered() -> None:
    assert "@business_dates" in signals.plan_snapshot_sql()
    assert "dly_po_plan_tcin" in signals.plan_snapshot_sql()
    assert "LAST_UPDATE_D <= @as_of" in signals.dfe_asof_sql()
    assert "QUALIFY" in signals.dfe_asof_sql()
    with pytest.raises(ValueError):
        signals.plan_snapshots([])


def test_logical_injection_inherits_orders_qualify() -> None:
    from pipelines.target_shipment_forecast import bq

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
