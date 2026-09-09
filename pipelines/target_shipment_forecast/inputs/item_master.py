"""Item master for the Target channel: `data/item_master_target.csv` (43 TCINs).

TCIN is the ONLY join key. Never join Target feeds on MANUFACTURER_STYLE /
VENDOR_STYLE_ID: they carry Target's `""` placeholder on ~95% of PO lines,
10-character truncations, and renamed spellings. Column highlights:

* `biom_sku` — dim_product SKU (missing for 11 TCINs: items not yet in the
  DTC catalogue). `rdz_item` — the RDZ Item # holding the stock, with
  `rdz_base_qty_multiplier` base units consumed per Target unit (kits).
* `casepack` — casepack of record; `casepack_source` says where it came from
  (mode of orders VENDOR_CASEPACK_Q first, plan VENDOR_CASE_PACK_Q, RDZ Units
  per Case). `shippack` — STORE_SHIPPACK_Q where seen.
* `mapping_confidence` in {exact, inferred, unmapped}; `mapping_rule` says why.
* `dpci` encodes department-class-item, which is how item groups are derived.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from pipelines.target_shipment_forecast.config import data_dir
from pipelines.target_shipment_forecast.inputs.aliases import normalise_key

FILENAME = "item_master_target.csv"
KEY_COLUMNS: tuple[str, ...] = (
    "tcin",
    "dpci",
    "vendor_style",
    "biom_sku",
    "rdz_item",
    "rdz_base_qty_multiplier",
    "casepack",
    "casepack_source",
    "shippack",
    "mapping_confidence",
    "item_state",
)


def load_item_master(path: Path | None = None) -> pd.DataFrame:
    """Read the CSV with `tcin` as int64 and the pack columns as floats."""
    p = path or data_dir() / FILENAME
    df = pd.read_csv(p)
    missing = [c for c in KEY_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{p} is missing columns {missing}")
    df["tcin"] = df["tcin"].astype("int64")
    for c in ("casepack", "shippack"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["rdz_base_qty_multiplier"] = (
        pd.to_numeric(df["rdz_base_qty_multiplier"], errors="coerce").fillna(1).astype(int)
    )
    if df["tcin"].duplicated().any():
        raise ValueError("item master has duplicate TCINs")
    return df


@dataclass(frozen=True)
class CasepackOfRecord:
    """Casepack and its provenance for one TCIN."""

    tcin: int
    casepack: float | None
    source: str | None


class ItemMaster:
    """Typed helpers over the item master frame."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame.set_index("tcin", drop=False)

    @classmethod
    def load(cls, path: Path | None = None) -> ItemMaster:
        """Load from `data/item_master_target.csv` (or `path`)."""
        return cls(load_item_master(path))

    @property
    def tcins(self) -> list[int]:
        """All TCINs, ascending."""
        return sorted(int(t) for t in self.frame.index)

    def row(self, tcin: int) -> pd.Series | None:
        """The item master row for a TCIN, or None."""
        try:
            row = self.frame.loc[int(tcin)]
        except KeyError:
            return None
        return row if isinstance(row, pd.Series) else None

    def sku_for(self, tcin: int, *, fallback_to_rdz: bool = True) -> str | None:
        """`biom_sku` for a TCIN; falls back to `rdz_item` (a Biom SKU spelling) when asked."""
        r = self.row(tcin)
        if r is None:
            return None
        sku = r["biom_sku"]
        if isinstance(sku, str) and sku.strip():
            return sku.strip()
        if fallback_to_rdz:
            rdz = r["rdz_item"]
            if isinstance(rdz, str) and rdz.strip():
                return rdz.strip()
        return None

    def tcins_for(
        self, sku: str, *, columns: Sequence[str] = ("biom_sku", "rdz_item")
    ) -> list[int]:
        """TCINs whose `biom_sku` or `rdz_item` (or the given `columns`, e.g. `vendor_style`)
        normalises to `sku` (PDQ suffix ignored)."""
        key = normalise_key(sku).key
        hits: list[int] = []
        for _, r in self.frame.iterrows():
            for col in columns:
                v = r[col]
                if isinstance(v, str) and normalise_key(v).key == key:
                    hits.append(int(r["tcin"]))
                    break
        return sorted(set(hits))

    def rdz_item_for(self, tcin: int) -> tuple[str | None, int]:
        """`(rdz_item, base_qty_multiplier)` for a TCIN."""
        r = self.row(tcin)
        if r is None:
            return None, 1
        v = r["rdz_item"]
        return (v.strip() if isinstance(v, str) and v.strip() else None), int(
            r["rdz_base_qty_multiplier"]
        )

    def casepack_of_record(self, tcin: int) -> CasepackOfRecord:
        """Casepack of record (mode of orders VENDOR_CASEPACK_Q when available) and its source."""
        r = self.row(tcin)
        if r is None:
            return CasepackOfRecord(int(tcin), None, None)
        cp = r["casepack"]
        src = r["casepack_source"]
        return CasepackOfRecord(
            int(tcin),
            None if pd.isna(cp) else float(cp),
            src if isinstance(src, str) and src.strip() else None,
        )

    def department_class(self, tcin: int) -> tuple[int | None, int | None]:
        """`(department_id, class_id)` from the DPCI (`DDD-CC-IIII`)."""
        from pipelines.target_shipment_forecast.channels.target.calendar import department_class_from_dpci

        r = self.row(tcin)
        return department_class_from_dpci(None if r is None else r["dpci"])

    def unmapped(self) -> pd.DataFrame:
        """Rows with `mapping_confidence == 'unmapped'` (4 today)."""
        return self.frame[self.frame["mapping_confidence"] == "unmapped"].reset_index(drop=True)
