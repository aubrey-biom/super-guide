"""Casepack rounding.

Plan-sourced rows are ALREADY casepack multiples (100% of ordered/received
quantities are `VENDOR_CASEPACK_Q` multiples: 1/3/4/6/12/24; median
replenishment line = 48 units = 6 cases) and are never re-rounded. Derived
rows (fallback rungs, owner extension, interval bounds) round to the casepack
of record = mode of orders `VENDOR_CASEPACK_Q` per TCIN, falling back to the
plan's `VENDOR_CASE_PACK_Q`, then RDZ Units per Case
(`data/item_master_target.csv` casepack / casepack_source). Point rounds to
the nearest case; P10 rounds down and P90 up. Known conflicts (P-DIS-BLK
Target 6 vs RDZ 12; K-DIS-2BAB-PUR 6|4 vs 4) carry CASEPACK_CONFLICT.
"""

from __future__ import annotations

import pandas as pd


def round_to_casepack(units: pd.Series, casepack: pd.Series, *, mode: str = "nearest") -> pd.Series:
    """Round `units` to multiples of `casepack` (`nearest` | `down` | `up`); NaN casepack passes through."""
    raise NotImplementedError("v1: implemented in the model pass")


def apply_casepacks(forecast: pd.DataFrame, item_master: pd.DataFrame) -> pd.DataFrame:
    """Add `casepack, expected_cases` and round derived rows (not plan-sourced ones)."""
    raise NotImplementedError("v1: implemented in the model pass")
