"""Item alias resolution over two files.

* `data/item_aliases_upstream.tsv` — copied from fastidious-lion's
  crstl-po-alert skill (`id_type, identifier, rdz_item, base_qty, note`):
  UPC / case-UPC / partner code / description -> RDZ Item #, with the number of
  base units one ordered unit consumes.
* `data/sku_aliases_target.tsv` — Target-channel SKU spellings
  (`alias, canonical_sku, note`).

Normalisation, applied to BOTH sides before lookup, in this order:
strip -> uppercase -> `_` to `-` -> collapse whitespace runs to one space ->
split off a trailing ` (PDQ)` into `is_pdq`. UPC-kind identifiers are reduced
to digits with leading zeros stripped.

`resolve()` returns the canonical SKU, `is_pdq`, `base_qty` (1 unless an
upstream multi-pack row says otherwise), and `source` naming which file/row
kind matched (`identity` when nothing did — the normalised key is returned so
callers can report it as unmapped, never dropped).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pipelines.target_shipment_forecast.config import data_dir

PDQ_RE = re.compile(r"\s*\(PDQ\)\s*$", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")

UPSTREAM_FILENAME = "item_aliases_upstream.tsv"
TARGET_FILENAME = "sku_aliases_target.tsv"


@dataclass(frozen=True)
class NormalisedKey:
    """Result of `normalise_key`."""

    key: str
    is_pdq: bool


def normalise_key(raw: object) -> NormalisedKey:
    """strip, uppercase, `_`->`-`, collapse whitespace, split trailing ` (PDQ)`."""
    s = "" if raw is None else str(raw)
    s = s.strip().upper().replace("_", "-")
    s = _WS_RE.sub(" ", s).strip()
    is_pdq = bool(PDQ_RE.search(s))
    if is_pdq:
        s = PDQ_RE.sub("", s).strip()
    return NormalisedKey(key=s, is_pdq=is_pdq)


def normalise_upc(raw: object) -> str:
    """Digits only, leading zeros stripped (`085005629809` == `85005629809`)."""
    digits = re.sub(r"\D", "", "" if raw is None else str(raw))
    return digits.lstrip("0")


@dataclass(frozen=True)
class UpstreamAlias:
    """One row of the upstream alias file."""

    id_type: str
    identifier: str
    rdz_item: str
    base_qty: int
    note: str


@dataclass(frozen=True)
class Resolution:
    """Outcome of `AliasMap.resolve`."""

    input: str
    key: str
    canonical_sku: str
    is_pdq: bool
    base_qty: int
    source: str
    matched: bool

    @property
    def rdz_item(self) -> str:
        """The RDZ stock line: canonical SKU plus ` (PDQ)` when the PDQ flag is set."""
        return f"{self.canonical_sku} (PDQ)" if self.is_pdq else self.canonical_sku


def _read_tsv(path: Path) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        rows.append(line.rstrip("\n").split("\t"))
    return rows


class AliasMap:
    """Lookup over the Target SKU aliases and the upstream identifier map."""

    def __init__(self, target: dict[str, tuple[str, str]], upstream: list[UpstreamAlias]) -> None:
        self._target = target
        self._upstream_by_kind: dict[str, dict[str, UpstreamAlias]] = {}
        # GTIN 11-digit index (crstl-po-alert SKILL.md section 4d): some partners drop the
        # check digit (UNFI) or send it zero-padded as a case code (Merchants Distributors).
        # Applied ONLY when the incoming code is exactly 11 digits after normalisation.
        self._upc11: dict[str, UpstreamAlias] = {}
        for u in upstream:
            if u.id_type in {"upc", "upc_case"}:
                digits = normalise_upc(u.identifier)
                self._upstream_by_kind.setdefault(u.id_type, {})[digits] = u
                self._upc11.setdefault(digits[:11], u)
            else:
                self._upstream_by_kind.setdefault(u.id_type, {})[
                    normalise_key(u.identifier).key
                ] = u

    @classmethod
    def load(cls, target_path: Path | None = None, upstream_path: Path | None = None) -> AliasMap:
        """Load both TSVs from `data/` (or the given paths)."""
        tpath = target_path or data_dir() / TARGET_FILENAME
        upath = upstream_path or data_dir() / UPSTREAM_FILENAME
        target: dict[str, tuple[str, str]] = {}
        for cells in _read_tsv(tpath):
            if cells[0].strip().lower() == "alias":
                continue  # header
            alias, canonical = cells[0], cells[1]
            note = cells[2] if len(cells) > 2 else ""
            target[normalise_key(alias).key] = (canonical.strip(), note)
        upstream: list[UpstreamAlias] = []
        for cells in _read_tsv(upath):
            if len(cells) < 3:
                continue
            id_type, identifier, rdz_item = cells[0].strip(), cells[1].strip(), cells[2].strip()
            base_qty = int(cells[3]) if len(cells) > 3 and cells[3].strip().isdigit() else 1
            note = cells[4] if len(cells) > 4 else ""
            upstream.append(UpstreamAlias(id_type, identifier, rdz_item, base_qty, note))
        return cls(target, upstream)

    @property
    def target_aliases(self) -> dict[str, tuple[str, str]]:
        """Normalised alias -> (canonical_sku, note)."""
        return dict(self._target)

    def resolve(self, raw: object, *, kind: str = "auto") -> Resolution:
        """Resolve any identifier.

        `kind`: `sku` (Target file, then upstream `code`), `upc` (upstream `upc`
        then `upc_case`), `desc` (upstream `desc`), or `auto` (all-digit input
        of 8+ digits is tried as UPC first, then as SKU).
        """
        text = "" if raw is None else str(raw)
        nk = normalise_key(text)
        digits = normalise_upc(text)
        looks_upc = text.strip().isdigit() and len(text.strip()) >= 8

        order: list[str]
        if kind == "sku":
            order = ["sku"]
        elif kind == "upc":
            order = ["upc"]
        elif kind == "desc":
            order = ["desc"]
        else:
            order = ["upc", "sku", "desc"] if looks_upc else ["sku", "desc"]

        for step in order:
            if step == "sku":
                hit = self._target.get(nk.key)
                if hit is not None:
                    canon = normalise_key(hit[0])
                    return Resolution(
                        text,
                        nk.key,
                        canon.key,
                        nk.is_pdq or canon.is_pdq,
                        1,
                        "sku_aliases_target",
                        True,
                    )
                code = self._upstream_by_kind.get("code", {}).get(nk.key)
                if code is not None:
                    canon = normalise_key(code.rdz_item)
                    return Resolution(
                        text,
                        nk.key,
                        canon.key,
                        nk.is_pdq or canon.is_pdq,
                        code.base_qty,
                        "item_aliases_upstream:code",
                        True,
                    )
            elif step == "upc":
                for id_type in ("upc", "upc_case"):
                    u = self._upstream_by_kind.get(id_type, {}).get(digits)
                    if u is not None:
                        canon = normalise_key(u.rdz_item)
                        return Resolution(
                            text,
                            digits,
                            canon.key,
                            canon.is_pdq,
                            u.base_qty,
                            f"item_aliases_upstream:{id_type}",
                            True,
                        )
                if len(digits) == 11 and digits in self._upc11:
                    u = self._upc11[digits]
                    canon = normalise_key(u.rdz_item)
                    return Resolution(
                        text,
                        digits,
                        canon.key,
                        canon.is_pdq,
                        u.base_qty,
                        "item_aliases_upstream:upc11",
                        True,
                    )
            elif step == "desc":
                u = self._upstream_by_kind.get("desc", {}).get(nk.key)
                if u is not None:
                    canon = normalise_key(u.rdz_item)
                    return Resolution(
                        text,
                        nk.key,
                        canon.key,
                        canon.is_pdq,
                        u.base_qty,
                        "item_aliases_upstream:desc",
                        True,
                    )
        return Resolution(text, nk.key, nk.key, nk.is_pdq, 1, "identity", False)
