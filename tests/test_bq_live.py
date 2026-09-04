"""Live BigQuery probes. Bill bytes; skipped without a credential."""

from __future__ import annotations

from datetime import date

import pytest

pytestmark = pytest.mark.bq_live


@pytest.fixture(scope="module")
def bq_client():  # type: ignore[no-untyped-def]
    from shipcast import bq

    return bq.client()


def test_session_user(bq_client) -> None:  # type: ignore[no-untyped-def]
    from shipcast import bq

    user = bq.session_user(bq_client)
    assert "@" in user


def test_orders_dedup_reduces_to_thousands_not_hundreds_of_thousands(bq_client) -> None:  # type: ignore[no-untyped-def]
    from shipcast import bq
    from shipcast.channels.target import signals

    n = signals.orders_line_count(run=lambda sql, **kw: bq.query(sql, bq_client=bq_client, **kw))
    lines = int(n["n_lines"].iloc[0])
    assert 5_000 <= lines <= 20_000, lines  # ~7.8k on 2026-09-03; raw feed is ~150k


def test_plan_snapshot_is_one_business_d(bq_client) -> None:  # type: ignore[no-untyped-def]
    from shipcast import bq
    from shipcast.channels.target import signals

    run = lambda sql, **kw: bq.query(sql, bq_client=bq_client, **kw)  # noqa: E731
    dates = signals.plan_business_dates(date.today(), limit=1, run=run)
    assert dates
    plan = signals.plan_snapshot(dates[0], run=run)
    assert plan["business_d"].nunique() == 1
    assert len(plan) > 0
