"""Leakage discipline: no feature may come from data stamped after its origin.

Every backtest row carries `origin` (the Saturday before the PO week, or the
run's as-of) and `source_ts` (the plan BUSINESS_D, order SNAPSHOT_D, DFE
LAST_UPDATE_D, inventory/sales date the value came from). `assert_no_leakage`
fails loudly if any `source_ts > origin`. Verified fact usable as a fixture:
post-week plan snapshots show ORDERED_Q = 0 for past order dates, so a leaked
snapshot would make the plan look worse, not better — the assertion is still
mandatory because the direction of a leak is not something to rely on.
"""

from __future__ import annotations

import pandas as pd


class LeakageError(AssertionError):
    """A feature's source timestamp is later than its backtest origin."""


def find_leaks(
    df: pd.DataFrame, *, origin_col: str = "origin", source_ts_col: str = "source_ts"
) -> pd.DataFrame:
    """Rows whose `source_ts` is after `origin` (NaT source_ts is not a leak)."""
    origin = pd.to_datetime(df[origin_col])
    src = pd.to_datetime(df[source_ts_col])
    return df[src.notna() & (src > origin)]


def assert_no_leakage(
    df: pd.DataFrame,
    *,
    origin_col: str = "origin",
    source_ts_col: str = "source_ts",
    max_report: int = 10,
) -> None:
    """Raise `LeakageError` listing the first offending rows."""
    leaks = find_leaks(df, origin_col=origin_col, source_ts_col=source_ts_col)
    if len(leaks):
        head = leaks.head(max_report).to_dict("records")
        raise LeakageError(
            f"{len(leaks)} backtest rows use data after their origin; first {len(head)}: {head}"
        )
