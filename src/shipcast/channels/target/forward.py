"""Replenishment vs forward/launch PO lines, open units, lapsed lines.

Definitions (docs/PLAN.md sections 3.1 and 3.6; thresholds in config/target.yaml):

* `ship_lag_days = revised_ship_begin_d - purchase_order_create_d` (original
  ship begin as fallback).
* stream = `replenishment` when `ship_lag_days <= forward_threshold_days` (14),
  else `forward`. Forward lines are never forecast; they are carried as booked
  demand in their ship week.
* `open_units = revised_order_q - item_received_q - cancel_remaining_order_q`,
  floored at 0 (a negative would mean received > revised; flagged, not summed).
* `lapsed = ship_end + grace < as_of`: the ship window has passed. Lapsed lines
  never count as demand.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import numpy as np
import pandas as pd

REPLENISHMENT = "replenishment"
FORWARD = "forward"


def _coalesce_dates(df: pd.DataFrame, primary: str, fallback: str) -> pd.Series:
    p = (
        pd.to_datetime(df[primary], errors="coerce")
        if primary in df
        else pd.Series(pd.NaT, index=df.index)
    )
    f = (
        pd.to_datetime(df[fallback], errors="coerce")
        if fallback in df
        else pd.Series(pd.NaT, index=df.index)
    )
    return p.fillna(f)


def _numeric_or_zero(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df:
        return pd.Series(0.0, index=df.index)
    return pd.to_numeric(df[col], errors="coerce").fillna(0)


def classify_lines(
    orders: pd.DataFrame,
    *,
    as_of: date,
    forward_threshold_days: int = 14,
    lapsed_grace_days: int = 7,
) -> pd.DataFrame:
    """Add `ship_begin_d, ship_end_d, ship_lag_days, stream, open_units, lapsed, negative_open` columns.

    `orders` is the frame from `signals.orders_latest()` (or any frame with the
    bullseye `orders_daily` column contract plus the ship-window columns).
    """
    df = orders.copy()
    create = pd.to_datetime(df["purchase_order_create_d"], errors="coerce")
    df["ship_begin_d"] = _coalesce_dates(df, "revised_ship_begin_d", "original_ship_begin_d")
    df["ship_end_d"] = _coalesce_dates(df, "revised_ship_end_d", "original_ship_end_d")
    df["ship_lag_days"] = (df["ship_begin_d"] - create).dt.days
    df["stream"] = np.where(df["ship_lag_days"] > forward_threshold_days, FORWARD, REPLENISHMENT)
    # Lines with no ship date at all cannot be classified as forward; keep them replen but mark.
    df["ship_lag_missing"] = df["ship_lag_days"].isna()

    revised = pd.to_numeric(df["revised_order_q"], errors="coerce").fillna(0)
    received = _numeric_or_zero(df, "item_received_q")
    cancel = _numeric_or_zero(df, "cancel_remaining_order_q")
    raw_open = revised - received - cancel
    df["negative_open"] = raw_open < 0
    df["open_units"] = raw_open.clip(lower=0)

    cutoff = pd.Timestamp(as_of) - pd.Timedelta(days=lapsed_grace_days)
    df["lapsed"] = df["ship_end_d"].notna() & (df["ship_end_d"] < cutoff)
    return df


def split_streams(lines: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """`(replenishment_lines, forward_lines)` from a classified frame."""
    return lines[lines["stream"] == REPLENISHMENT], lines[lines["stream"] == FORWARD]


def open_lines(lines: pd.DataFrame) -> pd.DataFrame:
    """Lines that still count as demand: open units > 0 and not lapsed."""
    return lines[(lines["open_units"] > 0) & ~lines["lapsed"]]


def weekly_by_stream(
    lines: pd.DataFrame, week_start: Callable[[pd.Series], pd.Series]
) -> pd.DataFrame:
    """PO units by (Sunday week of creation, TCIN): all / replenishment / forward.

    Columns: `wk, tcin, act_all, act_rep, act_fwd, act_orig_all, n_po, n_dc, n_fwd_lines`.
    """
    df = lines.copy()
    df["wk"] = week_start(df["purchase_order_create_d"])
    revised = pd.to_numeric(df["revised_order_q"], errors="coerce").fillna(0)
    df["_rep"] = np.where(df["stream"] == REPLENISHMENT, revised, 0.0)
    df["_fwd"] = np.where(df["stream"] == FORWARD, revised, 0.0)
    df["_all"] = revised
    df["_orig"] = (
        pd.to_numeric(df["original_order_q"], errors="coerce").fillna(0)
        if "original_order_q" in df
        else revised
    )
    df["_is_fwd"] = (df["stream"] == FORWARD).astype(int)
    out = (
        df.groupby(["wk", "tcin"], as_index=False)
        .agg(
            act_all=("_all", "sum"),
            act_rep=("_rep", "sum"),
            act_fwd=("_fwd", "sum"),
            act_orig_all=("_orig", "sum"),
            n_po=("purchase_order_id", "nunique"),
            n_dc=("receiving_location_id", "nunique"),
            n_fwd_lines=("_is_fwd", "sum"),
        )
        .sort_values(["wk", "tcin"])
        .reset_index(drop=True)
    )
    return out
