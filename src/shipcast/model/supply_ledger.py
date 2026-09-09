"""Single-count supply ledger: what Biom can actually ship against the forecast.

Per RDZ item s, in ship-week order:

    supply_s(w) = physical_onhand_s
                + sum inbound_s (eta <= ship_date(w) - buffer, confidence == dated)
                - non_target_allocations_s
                - sum_{v < w} expected_ship_s(v)
    demand_s(w) = open Target PO lines shipping in w  (replen created-unshipped + booked forward;
                                                       qty = revised - received - cancel_remaining)
                + forecast PO units for w
    expected_ship[t, w] = min(demand[t, w], supply_s(w) pro rata across TCINs sharing s)

Rules:
* physical on-hand = RDZ `Qty Remaining` + `Qty Allocated/Pending` (the identity
  Remaining = Received + Adj - Allocated - Shipped holds on 127/127 rows, so
  Remaining is ATP, not physical).
* only `dated` inbound counts (po_placed / tbd are shown as upside, never summed).
* open Target PO lines count ONCE, as demand in their ship week; nothing is
  subtracted from supply for Target allocations.
* lapsed lines (ship window passed, `forward.classify_lines`) never count as demand.
* kit multipliers (`rdz_base_qty_multiplier`) convert Target units to base units
  for shared pools (P-60WIP-FLU-FRA feeds the -2PK and -3PK TCINs).
Coverage flags OK / TIGHT / SHORT at 1.5x / 1.0x cumulative need are operational choices.
"""

from __future__ import annotations

from datetime import date

import pandas as pd


def build_ledger(
    forecast: pd.DataFrame,
    open_lines: pd.DataFrame,
    on_hand: pd.DataFrame,
    inbound: pd.DataFrame,
    item_master: pd.DataFrame,
    *,
    as_of: date,
    inbound_buffer_days: int = 3,
    counted_confidence: frozenset[str] = frozenset({"dated"}),
) -> pd.DataFrame:
    """Walk ship weeks and allocate supply to demand once.

    Returns per (ship_week, tcin): `demand_units, expected_ship_units, supply_before,
    supply_after, coverage_flag, flags` plus a per-item summary attribute.
    """
    raise NotImplementedError("v1: implemented in the model pass")
