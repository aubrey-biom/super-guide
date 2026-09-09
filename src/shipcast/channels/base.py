"""The channel seam: what every retail channel must provide to the model.

A channel adapter exposes four things. The model never imports a channel
module directly; it receives an object satisfying `ChannelAdapter`.

Signal panel (long form) columns: `item_id, week, signal, value, source_ts`.
`source_ts` is the timestamp of the data the value came from (a plan
`business_d`, an order `snapshot_d`, a DFE `last_update_d`) and is what the
leakage test compares against the backtest origin.

Actuals columns: `week, item_id, act_all, act_rep, act_fwd, n_po, n_dc,
n_fwd_lines` — replenishment and forward streams are never pooled.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

import pandas as pd

TIERS: frozenset[str] = frozenset({"retailer_plan", "retailer_forecast", "pos", "naive", "owner"})
"""Signal tiers a channel may populate. Target populates all five; Amazon 1P
would omit `retailer_plan` so the cascade starts at owner/naive explicitly."""


@runtime_checkable
class ChannelCalendar(Protocol):
    """Week anchor, PO->ship offsets and DC transit for one channel."""

    def week_start(self, d: date) -> date:
        """The anchor day (Sunday for Target) of the week containing `d`."""
        ...

    def ship_begin(self, create_d: date, item_group: str) -> date:
        """First ship day for a replenishment PO created on `create_d`."""
        ...

    def eta(self, ship_begin: date, dc: int) -> date:
        """Expected DC arrival for a shipment leaving on `ship_begin`."""
        ...


@runtime_checkable
class ChannelAdapter(Protocol):
    """Everything the model needs from one retail channel."""

    tiers: frozenset[str]

    def calendar(self) -> ChannelCalendar:
        """The channel's calendar."""
        ...

    def signals(self, as_of: date) -> pd.DataFrame:
        """Long signal panel as known on `as_of` (no source_ts after `as_of`)."""
        ...

    def actuals(self, start: date, end: date) -> pd.DataFrame:
        """Weekly PO actuals by item, split into replenishment and forward streams."""
        ...

    def item_master(self) -> pd.DataFrame:
        """Item master keyed by the channel's item id (TCIN for Target)."""
        ...
