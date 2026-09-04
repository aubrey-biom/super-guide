"""Parser for the RDZ 3PL "Inventory Summary" sheet (Drive markdown/plain-text export).

Spec: scratchpad `sheet_specs.md` sections A and C (2026-09-03), reproduced in
the rules below. The export renders every tab as pipe tables separated by blank
lines; tab names are lost, so the table is located by HEADER TEXT, never by
position.

Rules implemented
-----------------
* Tables = runs of consecutive lines starting with `|`; a blank line ends one.
* Cells are markdown-unescaped (`\\#` -> `#`, `\\-` -> `-`, `\\<` -> `<`, ...),
  a leading `[merged] ` marker is stripped, whitespace trimmed.
* The Inventory Summary is the table whose first 6 rows contain a header with
  all of `Item #`, `Qty Received (unit)`, `Qty Allocated / Pending`,
  `Qty Remaining (each/unit)` (compared on normalised names: lower-cased,
  whitespace collapsed — so `Inbound Arrival Date ` with its trailing space and
  `Low Stock? (<= threshold)` resolve).
* The banner row immediately above the header must match `Last updated: ...`.
  Its month/day give `as_of`; the year comes from the caller (Drive
  modifiedTime or the run's as-of year), never defaulted silently to today.
* Numbers: strip `\\`, `,`, `$`, whitespace; `(123)` and `-123` are negative;
  `-`, blank, `N/A` -> None (not 0). `Qty Adjustments` / `Qty Allocated` blanks
  become 0 only when the identity then reconciles.
* Two on-hand measures per item: `available` = Qty Remaining, `physical` =
  Remaining + Allocated. Negative `available` is legal and never clipped.
* ` (PDQ)` rows are separate items; `base_sku` strips the suffix for joins.
* Inbound rows come from `Inbound Arrival Date` / `Inbound Shipment QTY`:
  a parseable M/D/YYYY date -> `dated`; token `PO Placed` -> `po_placed`;
  token `TBD` -> `tbd`; tokens KIT BUILT IN DC / Old Stock / Healthy INV /
  No Replen(i)sh(i)ment / Kitting / blank -> no inbound row (kept in
  `no_inbound` with the reason); anything else -> row kept, `unknown`,
  `parse_error` set.
* Fail-loud checks (any failure raises `RdzParseError`): 100 <= item rows <= 200;
  banner present; identity `Remaining = Received + Adj - Allocated - Shipped`
  holds (+-0.5) on >= 99% of rows.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from shipcast.supply.base import INBOUND_COLUMNS, ON_HAND_COLUMNS

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

REQUIRED_HEADERS: frozenset[str] = frozenset(
    {"item #", "qty received (unit)", "qty allocated / pending", "qty remaining (each/unit)"}
)

# normalised header -> field name
HEADER_FIELDS: dict[str, str] = {
    "item #": "item",
    "item description": "description",
    "units per case": "units_per_case",
    "qty received (unit)": "received",
    "cases received": "cases_received",
    "qty adjustments": "adjustments",
    "qty allocated / pending": "allocated",
    "qty shipped": "shipped",
    "qty remaining (each/unit)": "remaining",
    "cases remaining": "cases_remaining",
    "total pallets remaining": "pallets_remaining",
    "low stock? (<= threshold)": "low_stock",
    "notes": "sheet_notes",
    "wipe pack type": "wipe_pack_type",
    "vendor": "vendor",
    "inbound arrival date": "inbound_date_raw",
    "inbound shipment qty": "inbound_qty_raw",
    "target": "target_flag",
    "d2c": "d2c_flag",
}
NUMERIC_FIELDS: tuple[str, ...] = (
    "units_per_case",
    "received",
    "cases_received",
    "adjustments",
    "allocated",
    "shipped",
    "remaining",
    "cases_remaining",
    "pallets_remaining",
)
NO_INBOUND_TOKENS: frozenset[str] = frozenset(
    {
        "healthy inv",
        "no replenshiment",
        "no replenishment",
        "old stock",
        "kit built in dc",
        "kitting",
    }
)
TITLE_STOP_TOKENS: frozenset[str] = frozenset({"totals", "low stock view"})
MERGED_PREFIX = "[merged]"
BANNER_RE = re.compile(r"^\s*Last updated:\s*(.+?)\s*$", re.IGNORECASE)
PDQ_RE = re.compile(r"\s*\(PDQ\)\s*$", re.IGNORECASE)
_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|<>$~&:;,'\"/?])")
_PIPE_SPLIT_RE = re.compile(r"(?<!\\)\|")
_US_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")
_NUMBER_RE = re.compile(r"^\(?-?\d+(\.\d+)?\)?$")
IDENTITY_TOL = 0.5
MIN_ROWS, MAX_ROWS = 100, 200
MIN_IDENTITY_SHARE = 0.99


class RdzParseError(ValueError):
    """A fail-loud check failed; the previous snapshot must be kept."""


# --------------------------------------------------------------------------------------
# Cell-level helpers
# --------------------------------------------------------------------------------------


def unescape_cell(cell: str) -> str:
    """Strip a leading `[merged] ` marker, undo markdown escapes, normalise no-break spaces, trim."""
    s = cell.replace("\u202f", " ").replace("\xa0", " ").strip()
    while s.startswith(MERGED_PREFIX):
        s = s[len(MERGED_PREFIX) :].lstrip()
    return _ESCAPE_RE.sub(r"\1", s).strip()


def normalise_header(cell: str) -> str:
    """Lower-case, unescape, collapse internal whitespace."""
    return re.sub(r"\s+", " ", unescape_cell(cell)).strip().lower()


def parse_number(cell: str | None) -> float | None:
    """`'\\-6,252'` -> -6252.0; `'$ 1,110,517.00 '` -> 1110517.0; `''`/`'-'`/`'N/A'` -> None."""
    if cell is None:
        return None
    s = unescape_cell(cell).replace("$", "").replace(",", "").replace(" ", "")
    if s in {"", "-", "N/A", "n/a", "NA"}:
        return None
    if not _NUMBER_RE.match(s):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    v = float(s)
    return -v if neg else v


def parse_us_date(cell: str | None) -> date | None:
    """`M/D/YYYY` or `M/D/YY` -> date; anything else -> None."""
    if not cell:
        return None
    m = _US_DATE_RE.match(unescape_cell(cell))
    if not m:
        return None
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_banner_date(banner: str, year: int) -> date:
    """`'Last updated: August 31, 1:35 PM'` -> date(year, 8, 31)."""
    m = BANNER_RE.match(unescape_cell(banner))
    if not m:
        raise RdzParseError(f"banner does not match 'Last updated: ...': {banner!r}")
    body = m.group(1)
    md = re.match(r"^([A-Za-z]+)\s+(\d{1,2})(?:,?\s+(\d{4}))?", body)
    if not md:
        raise RdzParseError(f"cannot read month/day from banner {banner!r}")
    month_name, day = md.group(1), int(md.group(2))
    y = int(md.group(3)) if md.group(3) else year
    try:
        month = datetime.strptime(month_name[:3], "%b").month
    except ValueError as e:
        raise RdzParseError(f"unknown month in banner {banner!r}") from e
    return date(y, month, day)


def split_row(line: str) -> list[str]:
    """Split a `| a | b |` line on unescaped pipes; drop the outer empties."""
    parts = _PIPE_SPLIT_RE.split(line.strip())
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return parts


def _is_alignment_row(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r"\s*:?-{1,}:?\s*", c) for c in cells)


# --------------------------------------------------------------------------------------
# Table location
# --------------------------------------------------------------------------------------


@dataclass
class RawTable:
    """A run of pipe lines: `(1-based line number, raw cells)` pairs."""

    start_line: int
    rows: list[tuple[int, list[str]]] = field(default_factory=list)


def split_tables(text: str) -> list[RawTable]:
    """Group consecutive `|` lines into tables; a non-table line ends the run."""
    tables: list[RawTable] = []
    cur: RawTable | None = None
    for i, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("|"):
            if cur is None:
                cur = RawTable(start_line=i)
            cur.rows.append((i, split_row(line)))
        elif cur is not None:
            tables.append(cur)
            cur = None
    if cur is not None:
        tables.append(cur)
    return tables


@dataclass
class LocatedTable:
    """The Inventory Summary table with its header resolved."""

    table: RawTable
    header_index: int
    columns: dict[str, int]  # field name -> column index
    banner: str | None


def locate_inventory_summary(tables: list[RawTable]) -> LocatedTable:
    """Find the table whose first 6 rows carry the Inventory Summary header."""
    for t in tables:
        for idx, (_, cells) in enumerate(t.rows[:6]):
            names = [normalise_header(c) for c in cells]
            if REQUIRED_HEADERS.issubset(set(names)):
                columns: dict[str, int] = {}
                for j, n in enumerate(names):
                    f = HEADER_FIELDS.get(n)
                    if f and f not in columns:
                        columns[f] = j
                banner = None
                if idx > 0:
                    prev = t.rows[idx - 1][1]
                    for c in prev:
                        u = unescape_cell(c)
                        if BANNER_RE.match(u):
                            banner = u
                            break
                return LocatedTable(table=t, header_index=idx, columns=columns, banner=banner)
    raise RdzParseError(
        "Inventory Summary table not found: no table has a header row with "
        + ", ".join(sorted(REQUIRED_HEADERS))
    )


# --------------------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------------------


@dataclass
class RdzSnapshot:
    """Parsed Inventory Summary plus the fail-loud check results."""

    as_of: date
    banner: str
    source: str
    items: pd.DataFrame
    no_inbound: pd.DataFrame
    checks: dict[str, Any]

    @property
    def n_items(self) -> int:
        """Number of item rows parsed."""
        return len(self.items)


def _row_to_record(cells: list[str], loc: LocatedTable, line_no: int) -> dict[str, Any]:
    rec: dict[str, Any] = {"raw_line_no": line_no}
    for fname, j in loc.columns.items():
        raw = cells[j] if j < len(cells) else ""
        rec[fname] = unescape_cell(raw)
    errors: list[str] = []
    for fname in NUMERIC_FIELDS:
        raw = rec.get(fname, "")
        val = parse_number(raw)
        if val is None and raw not in {"", "-", "N/A"}:
            errors.append(f"{fname}={raw!r}")
        rec[fname] = val
    rec["parse_error"] = "; ".join(errors) if errors else None
    item = str(rec.get("item", "")).upper()
    rec["item"] = item
    rec["is_pdq"] = bool(PDQ_RE.search(item))
    rec["base_sku"] = PDQ_RE.sub("", item).strip()
    rec["low_stock"] = str(rec.get("low_stock", "")).strip().upper() == "LOW"
    return rec


def _apply_identity(items: pd.DataFrame) -> pd.DataFrame:
    """Compute `identity_ok`; fill blank adjustments/allocated with 0 when that reconciles."""
    adj = items["adjustments"].fillna(0.0)
    alloc = items["allocated"].fillna(0.0)
    expected = items["received"].fillna(0.0) + adj - alloc - items["shipped"].fillna(0.0)
    ok = (items["remaining"] - expected).abs() <= IDENTITY_TOL
    ok &= items["remaining"].notna() & items["received"].notna() & items["shipped"].notna()
    items["identity_ok"] = ok
    fill_adj = ok & items["adjustments"].isna()
    fill_alloc = ok & items["allocated"].isna()
    items.loc[fill_adj, "adjustments"] = 0.0
    items.loc[fill_alloc, "allocated"] = 0.0
    items["available"] = items["remaining"]
    items["physical"] = items["remaining"] + items["allocated"].fillna(0.0)
    return items


def parse_inventory_summary(text: str, *, year: int, source: str = "rdz_sheet") -> RdzSnapshot:
    """Parse the export text; raise `RdzParseError` on any fail-loud check."""
    loc = locate_inventory_summary(split_tables(text))
    if loc.banner is None:
        raise RdzParseError("banner 'Last updated:' not found immediately above the header row")
    as_of = parse_banner_date(loc.banner, year)

    records: list[dict[str, Any]] = []
    for line_no, cells in loc.table.rows[loc.header_index + 1 :]:
        if not cells or _is_alignment_row(cells):
            continue
        first = unescape_cell(cells[0])
        if first == "" or normalise_header(first) == "item #":
            continue
        if normalise_header(first) in TITLE_STOP_TOKENS:
            break
        records.append(_row_to_record(cells, loc, line_no))

    items = pd.DataFrame.from_records(records)
    if items.empty:
        raise RdzParseError("Inventory Summary header found but no item rows followed it")
    items = _apply_identity(items)
    items["as_of"] = as_of
    items["source"] = source

    n = len(items)
    identity_share = float(items["identity_ok"].mean()) if n else 0.0
    dup = items["item"].duplicated().sum()
    checks: dict[str, Any] = {
        "n_items": int(n),
        "row_count_ok": MIN_ROWS <= n <= MAX_ROWS,
        "banner_ok": True,
        "identity_share": identity_share,
        "identity_ok": identity_share >= MIN_IDENTITY_SHARE,
        "identity_failures": items.loc[~items["identity_ok"], "item"].tolist(),
        "duplicate_items": int(dup),
        "parse_errors": int(items["parse_error"].notna().sum()),
    }
    if not checks["row_count_ok"]:
        raise RdzParseError(f"Inventory Summary has {n} item rows; expected {MIN_ROWS}-{MAX_ROWS}")
    if not checks["identity_ok"]:
        raise RdzParseError(
            f"identity Remaining = Received + Adj - Allocated - Shipped holds on only "
            f"{identity_share:.1%} of rows (need >= {MIN_IDENTITY_SHARE:.0%}); "
            f"failures: {checks['identity_failures'][:10]}"
        )

    no_inbound = _inbound_frames(items, as_of)[1]
    return RdzSnapshot(
        as_of=as_of,
        banner=loc.banner,
        source=source,
        items=items,
        no_inbound=no_inbound,
        checks=checks,
    )


# --------------------------------------------------------------------------------------
# Normalised frames
# --------------------------------------------------------------------------------------


def on_hand_frame(snapshot: RdzSnapshot) -> pd.DataFrame:
    """Wide on-hand: one row per item with `available` and `physical` (SupplyAdapter contract).

    Extra columns kept for audit: `base_sku, is_pdq, units_per_case, low_stock,
    vendor, wipe_pack_type, sheet_notes, identity_ok, raw_line_no, source`.
    """
    it = snapshot.items
    out = pd.DataFrame(
        {
            "item": it["item"],
            "available": it["available"],
            "physical": it["physical"],
            "allocated": it["allocated"],
            "as_of": snapshot.as_of,
            "base_sku": it["base_sku"],
            "is_pdq": it["is_pdq"],
            "units_per_case": it["units_per_case"],
            "received": it["received"],
            "adjustments": it["adjustments"],
            "shipped": it["shipped"],
            "low_stock": it["low_stock"],
            "vendor": it["vendor"],
            "wipe_pack_type": it["wipe_pack_type"],
            "sheet_notes": it["sheet_notes"],
            "identity_ok": it["identity_ok"],
            "parse_error": it["parse_error"],
            "raw_line_no": it["raw_line_no"],
            "source": snapshot.source,
        }
    )
    ordered = list(ON_HAND_COLUMNS) + [c for c in out.columns if c not in ON_HAND_COLUMNS]
    return out[ordered].reset_index(drop=True)


def on_hand_long(snapshot: RdzSnapshot) -> pd.DataFrame:
    """Spec C.1 long form: two rows per item, `measure` in {available, on_hand_physical}.

    Columns: `as_of_date, item_key, item_key_type, measure, qty_eaches, location, source, notes`.
    """
    it = snapshot.items
    rows: list[dict[str, Any]] = []
    for _, r in it.iterrows():
        notes = {
            "base_sku": r["base_sku"],
            "units_per_case": r["units_per_case"],
            "low_stock_flag": bool(r["low_stock"]),
            "vendor": r["vendor"] or None,
            "wipe_pack_type": r["wipe_pack_type"] or None,
            "sheet_notes": r["sheet_notes"] or None,
            "inbound_hint": [r["inbound_date_raw"], r["inbound_qty_raw"]],
            "identity_ok": bool(r["identity_ok"]),
            "banner": snapshot.banner,
            "raw_line_no": int(r["raw_line_no"]),
        }
        if r["parse_error"]:
            notes["parse_error"] = r["parse_error"]
        for measure, qty in (("available", r["available"]), ("on_hand_physical", r["physical"])):
            rows.append(
                {
                    "as_of_date": snapshot.as_of,
                    "item_key": r["item"],
                    "item_key_type": "rdz_item",
                    "measure": measure,
                    "qty_eaches": None if pd.isna(qty) else float(qty),
                    "location": "RDZ",
                    "source": snapshot.source,
                    "notes": json.dumps(notes, default=str),
                }
            )
    return pd.DataFrame(rows)


def _inbound_record(
    r: pd.Series,
    as_of: date,
    base: dict[str, Any],
    qty: float | None,
    eta: date | None,
    confidence: str,
    parse_error: str | None,
) -> dict[str, Any]:
    return {
        "shipment_id": f"RDZ-SUMMARY:{r['item']}:{as_of.isoformat()}",
        "qty": qty,
        "eta": eta,
        "confidence": confidence,
        "status": "open",
        "destination": "RDZ",
        "parse_error": parse_error,
        **base,
    }


def _inbound_frames(items: pd.DataFrame, as_of: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    inbound: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for _, r in items.iterrows():
        d_raw = str(r.get("inbound_date_raw") or "").strip()
        q_raw = str(r.get("inbound_qty_raw") or "").strip()
        qty = parse_number(q_raw)
        eta = parse_us_date(d_raw)
        tokens = {d_raw.lower(), q_raw.lower()} - {""}
        base = {
            "item": r["item"],
            "raw_date": d_raw,
            "raw_qty": q_raw,
            "low_stock": bool(r["low_stock"]),
            "vendor": r["vendor"],
            "raw_line_no": int(r["raw_line_no"]),
        }
        if eta is not None:
            inbound.append(
                _inbound_record(
                    r, as_of, base, qty, eta, "dated", None if qty is not None else "DATED_NO_QTY"
                )
            )
        elif "po placed" in tokens:
            inbound.append(_inbound_record(r, as_of, base, qty, eta, "po_placed", None))
        elif "tbd" in tokens:
            inbound.append(_inbound_record(r, as_of, base, qty, eta, "tbd", None))
        elif not tokens:
            excluded.append({**base, "reason": "blank"})
        elif tokens & NO_INBOUND_TOKENS:
            excluded.append({**base, "reason": sorted(tokens & NO_INBOUND_TOKENS)[0]})
        elif qty is not None and qty <= 0 and not d_raw:
            excluded.append({**base, "reason": "zero_qty"})
        else:
            inbound.append(
                _inbound_record(
                    r,
                    as_of,
                    base,
                    qty,
                    eta,
                    "unknown",
                    f"unrecognised inbound tokens {sorted(tokens)}",
                )
            )

    cols = [
        *INBOUND_COLUMNS,
        "destination",
        "raw_date",
        "raw_qty",
        "low_stock",
        "vendor",
        "parse_error",
        "raw_line_no",
    ]
    inb = pd.DataFrame(inbound, columns=cols) if inbound else pd.DataFrame(columns=cols)
    exc_cols = [*base_cols(), "reason"]
    exc = pd.DataFrame(excluded, columns=exc_cols) if excluded else pd.DataFrame(columns=exc_cols)
    return inb, exc


def base_cols() -> list[str]:
    """Columns shared by inbound and no-inbound rows."""
    return ["item", "raw_date", "raw_qty", "low_stock", "vendor", "raw_line_no"]


def inbound_frame(snapshot: RdzSnapshot) -> pd.DataFrame:
    """Adapter I-1 inbound rows (SupplyAdapter contract plus audit columns)."""
    return _inbound_frames(snapshot.items, snapshot.as_of)[0]


# --------------------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------------------


class RdzSheetAdapter:
    """`SupplyAdapter` over one exported RDZ sheet file.

    `year` is the banner's year (Drive modifiedTime year when known). If it is
    omitted, `as_of.year` is used at call time, rolling back one year when the
    banner would otherwise land more than 7 days after `as_of`.
    """

    def __init__(
        self, path: str | Path, *, year: int | None = None, source: str | None = None
    ) -> None:
        self.path = Path(path)
        self.year = year
        self.source = source or f"file:{self.path.name}#Inventory Summary"
        self._snapshot: RdzSnapshot | None = None
        self._snapshot_year: int | None = None

    def snapshot(self, as_of: date) -> RdzSnapshot:
        """Parse (once per year value) and return the snapshot."""
        year = self.year or as_of.year
        if self._snapshot is None or self._snapshot_year != year:
            text = self.path.read_text(encoding="utf-8")
            snap = parse_inventory_summary(text, year=year, source=self.source)
            if self.year is None and (snap.as_of - as_of).days > 7:
                snap = parse_inventory_summary(text, year=year - 1, source=self.source)
            self._snapshot, self._snapshot_year = snap, year
        return self._snapshot

    def on_hand(self, as_of: date) -> pd.DataFrame:
        """Wide on-hand frame; `age_days` = as_of - banner date for the staleness flag."""
        snap = self.snapshot(as_of)
        df = on_hand_frame(snap)
        df["age_days"] = (as_of - snap.as_of).days
        return df

    def inbound(self, as_of: date) -> pd.DataFrame:
        """Inbound rows from the summary's inbound columns."""
        return inbound_frame(self.snapshot(as_of))
