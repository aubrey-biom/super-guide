"""DOSS supply adapter — stub.

DOSS replaces the RDZ Google Sheet as the supply source. The seam is
`shipcast.supply.base.SupplyAdapter`; nothing else in shipcast changes.

THE ONE CONTRACT REQUIREMENT: DOSS SKU identifiers must equal the RDZ `Item #`
exactly, INCLUDING the ` (PDQ)` display-pack lines (`P-40WIP-LIT-FRA (PDQ)`,
`P-40WIP-6IN-SAN-STL (PDQ)`), which are separate stock lines from their base
SKUs. If DOSS keys differ, add the mapping to `data/sku_aliases_target.tsv`
rather than special-casing it here.
"""

from __future__ import annotations

from datetime import date

import pandas as pd


class DossAdapter:
    """Placeholder satisfying `SupplyAdapter`; every call raises NotImplementedError."""

    def __init__(self, endpoint: str | None = None) -> None:
        self.endpoint = endpoint

    def on_hand(self, as_of: date) -> pd.DataFrame:
        """Not implemented: DOSS integration is a later phase."""
        raise NotImplementedError(
            "DOSS adapter not implemented. Contract: DOSS SKU ids == RDZ Item # incl. PDQ lines."
        )

    def inbound(self, as_of: date) -> pd.DataFrame:
        """Not implemented: DOSS integration is a later phase."""
        raise NotImplementedError(
            "DOSS adapter not implemented. Contract: DOSS SKU ids == RDZ Item # incl. PDQ lines."
        )
