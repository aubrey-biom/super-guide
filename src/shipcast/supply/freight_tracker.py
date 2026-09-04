"""Inbound Freight Tracker (Google Sheet exported as .xlsx) -> inbound supplier PO lines.

Read the workbook, never the Drive text rendering: the text export truncates
long tabs (it showed 145 of 256 rows on 2026-09-04 and hid every PO placed
after March). `Freight Tracker` is the first tab; header row is located by the
cell text `Status`, not by position.

Header (verbatim, 2026-09-04): Status | PO # | Supplier | Item | # of Units |
UOM | Order Date | BOL/Tracking | Notes | Ship To | End of Production Date |
ETA | Per Unit Cost | Total Line Cost | Down Payment % | Down payment Paid
Date | Landed Cost.

Status mapping: Delivered -> closed; Canceled -> cancelled; Ordered /
Partial Received / In Transit / In Production -> open; Draft -> draft (shown,
never counted); blank status -> unknown (shown, never counted).

Confidence: `dated` when ETA is a real date on or after the as-of date;
`overdue` when ETA is before as-of on an open row (probably received and not
updated, or late: confirm against RDZ receipts before counting); `tbd` when
ETA is missing or the 1900-01-25 placeholder. Only `dated` rows are summed
into supply (config `supply.inbound_confidence_counted`).

Item keys are Biom SKUs for finished goods and free text for fragrances,
packaging and tooling; `is_finished_good` is True when the normalised key
resolves through the alias map / item master. Quantities are eaches for
finished goods (`UOM` blank or "Eaches"); other UOMs (lbs, Kg) are kept with
`unit_type` so nothing is silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from shipcast.inputs.aliases import normalise_key

MAIN_TAB_HINT = "Freight Tracker"
HEADER_KEY = "Status"
PLACEHOLDER_ETA = date(1900, 1, 25)

STATUS_MAP = {
    "delivered": "closed",
    "received": "closed",
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "ordered": "open",
    "partial received": "open",
    "in transit": "open",
    "in production": "open",
    "produced": "open",
    "draft": "draft",
}

COLUMNS: tuple[str, ...] = (
    "shipment_id",
    "item_key",
    "item",
    "sku",
    "is_pdq",
    "is_finished_good",
    "qty",
    "unit_type",
    "supplier",
    "order_date",
    "eta",
    "status_raw",
    "status",
    "confidence",
    "destination",
    "notes",
    "source",
    "row",
)


class FreightParseError(ValueError):
    """The workbook does not look like the Inbound Freight Tracker."""


def _as_date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        s = v.strip()
        for f in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, f).date()
            except ValueError:
                continue
    return None


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, int | float):
        return float(v)
    s = re.sub(r"[,$\s]", "", str(v))
    try:
        return float(s)
    except ValueError:
        return None


@dataclass(frozen=True)
class FreightSnapshot:
    """Parsed tracker plus the checks that were run."""

    rows: pd.DataFrame
    header: list[str]
    n_rows: int
    tab: str
    as_of: date


def parse_freight_xlsx(
    path: str | Path, *, as_of: date, resolver: Any | None = None, source: str | None = None
) -> FreightSnapshot:
    """Parse the exported workbook. `resolver(key) -> (sku, is_pdq, resolved: bool)` is optional."""
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True, read_only=True)
    ws = None
    for cand in wb.worksheets:
        if MAIN_TAB_HINT.lower() in cand.title.lower():
            ws = cand
            break
    ws = ws or wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    hdr_i = next(
        (
            i
            for i, r in enumerate(rows)
            if r and any(isinstance(c, str) and c.strip() == HEADER_KEY for c in r)
        ),
        None,
    )
    if hdr_i is None:
        raise FreightParseError(f"no header row containing {HEADER_KEY!r} in tab {ws.title!r}")
    header = [str(c).strip() if c is not None else "" for c in rows[hdr_i]]
    col = {h: i for i, h in enumerate(header) if h}
    required = ["Status", "PO #", "Item", "# of Units", "Order Date", "ETA"]
    missing = [c for c in required if c not in col]
    if missing:
        raise FreightParseError(f"tracker header is missing {missing}; got {header}")

    out: list[dict[str, Any]] = []
    for n, r in enumerate(rows[hdr_i + 1 :], start=hdr_i + 2):
        if not r or all(c in (None, "") for c in r):
            continue
        vals = {h: (r[i] if i < len(r) else None) for h, i in col.items()}

        def get(name: str, _v: dict[str, Any] = vals) -> Any:
            return _v.get(name)

        item = str(get("Item") or "").strip()
        if not item:
            continue
        status_raw = str(get("Status") or "").strip()
        status = STATUS_MAP.get(status_raw.lower(), "unknown" if not status_raw else "other")
        eta = _as_date(get("ETA"))
        if eta == PLACEHOLDER_ETA:
            eta = None
        qty = _num(get("# of Units"))
        uom = str(get("UOM") or "").strip() or "eaches"
        norm = normalise_key(item)
        sku, is_pdq, resolved = norm.key, norm.is_pdq, False
        if resolver is not None:
            try:
                sku, is_pdq, resolved = resolver(item)
            except Exception:
                resolved = False
        if status == "open":
            if eta is None:
                confidence = "tbd"
            elif eta < as_of:
                confidence = "overdue"
            else:
                confidence = "dated"
        elif status in ("draft", "unknown", "other"):
            confidence = "not_counted"
        else:
            confidence = "closed"
        out.append(
            {
                "shipment_id": str(get("PO #") or "").strip() or None,
                "item_key": item,
                "item": item,
                "sku": sku,
                "is_pdq": bool(is_pdq),
                "is_finished_good": bool(resolved)
                if resolver is not None
                else bool(re.match(r"^[KP]-", norm.key)),
                "qty": qty,
                "unit_type": uom.lower(),
                "supplier": str(get("Supplier") or "").strip() or None,
                "order_date": _as_date(get("Order Date")),
                "eta": eta,
                "status_raw": status_raw or None,
                "status": status,
                "confidence": confidence,
                "destination": str(get("Ship To") or "").strip() or None,
                "notes": str(get("Notes") or "").strip() or None,
                "source": source or str(path),
                "row": n,
            }
        )
    df = pd.DataFrame(out, columns=list(COLUMNS))
    if len(df) < 50:
        raise FreightParseError(
            f"only {len(df)} tracker rows parsed; expected a few hundred — wrong tab or truncated export?"
        )
    return FreightSnapshot(rows=df, header=header, n_rows=len(df), tab=ws.title, as_of=as_of)


def inbound_frame(
    snapshot: FreightSnapshot, *, counted: tuple[str, ...] = ("dated",)
) -> pd.DataFrame:
    """SupplyAdapter-shaped inbound rows: `shipment_id, item, qty, eta, confidence, status, destination, source`.

    Only finished goods with a quantity are returned; `counted` marks which
    confidence levels the ledger may sum (`counts_as_supply` column).
    """
    r = snapshot.rows
    r = r[r["is_finished_good"] & r["qty"].notna() & (r["status"] == "open")].copy()
    r["counts_as_supply"] = r["confidence"].isin(counted)
    r["item"] = r["sku"]
    return r[
        [
            "shipment_id",
            "item",
            "is_pdq",
            "qty",
            "unit_type",
            "eta",
            "confidence",
            "status",
            "destination",
            "supplier",
            "notes",
            "source",
            "counts_as_supply",
        ]
    ].reset_index(drop=True)


def reconcile_with_receipts(
    inbound: pd.DataFrame, receipts: pd.DataFrame, *, qty_tol: float = 0.05
) -> pd.DataFrame:
    """Mark overdue open lines that appear in the RDZ Receipts Log as `received_not_updated`.

    `receipts` needs `item, qty, received_date` (RDZ Receipts Log, eaches). A
    match is same item and a receipt within 5% of the line quantity dated after
    the order. Matched lines are never counted as supply; they are reported.
    """
    if inbound.empty or receipts is None or receipts.empty:
        return inbound
    r = receipts.copy()
    r["item_n"] = r["item"].map(lambda k: normalise_key(str(k)).key)
    out = inbound.copy()
    out["reconciled"] = ""
    for i, row in out.iterrows():
        if row["confidence"] != "overdue" or pd.isna(row["qty"]):
            continue
        cand = r[(r["item_n"] == normalise_key(str(row["item"])).key)]
        cand = cand[(cand["qty"] - row["qty"]).abs() <= qty_tol * row["qty"]]
        if not cand.empty:
            out.at[i, "reconciled"] = (
                f"received_not_updated ({pd.Timestamp(cand.iloc[0]['received_date']).date()})"
            )
            out.at[i, "counts_as_supply"] = False
    return out


__all__ = [
    "COLUMNS",
    "FreightParseError",
    "FreightSnapshot",
    "inbound_frame",
    "parse_freight_xlsx",
    "reconcile_with_receipts",
]
