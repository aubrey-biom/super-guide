"""The supply seam: on-hand and inbound frames every supply source must provide.

`on_hand(as_of)` columns: `item, available, physical, allocated, as_of`
  * `item`      — RDZ Item # (Biom SKU; ` (PDQ)` display-pack lines are separate items)
  * `available` — RDZ "Qty Remaining": = Received + Adjustments - Allocated - Shipped,
                  i.e. available-to-promise, may be negative
  * `physical`  — Remaining + Allocated/Pending: what is physically in the building
  * `allocated` — units reserved to open B2B orders not yet shipped
  * `as_of`     — the sheet's banner date

`inbound(as_of)` columns: `shipment_id, item, qty, eta, confidence, status`
  * `confidence` in {`dated`, `po_placed`, `tbd`, `unknown`}; only `dated` rows
    count as supply (config `supply.inbound_confidence_counted`)
  * `status` is `open` for every emitted row; non-inbound tokens (KIT BUILT IN
    DC, Old Stock, Healthy INV, ...) produce no row.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

import pandas as pd

ON_HAND_COLUMNS: tuple[str, ...] = ("item", "available", "physical", "allocated", "as_of")
INBOUND_COLUMNS: tuple[str, ...] = ("shipment_id", "item", "qty", "eta", "confidence", "status")
INBOUND_CONFIDENCE: frozenset[str] = frozenset({"dated", "po_placed", "tbd", "unknown"})


@runtime_checkable
class SupplyAdapter(Protocol):
    """A source of Biom finished-goods supply."""

    def on_hand(self, as_of: date) -> pd.DataFrame:
        """On-hand by item with both `available` and `physical` measures."""
        ...

    def inbound(self, as_of: date) -> pd.DataFrame:
        """Expected receipts by shipment with a confidence grade."""
        ...
