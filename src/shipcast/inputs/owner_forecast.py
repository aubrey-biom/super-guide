"""Owner (channel manager) forecast ingestion — header-detected parser registry.

Formats are detected by header text, never by file name. Every parser melts to
the same long form:

    channel, sku, tcin, period_start, forecast_units, forecast_basis, source_sheet, as_of

Rules that apply to every format:
* duplicate (channel, tcin, period_start) rows COLLAPSE to the first occurrence
  with a warning — they are never summed;
* keys that cannot be resolved to exactly one TCIN are returned separately in
  `unresolved` (with a reason), never dropped;
* `as_of` is the sheet's modification date, supplied by the caller.

Implemented:
* the tool-native CSV
  (`channel, item_key, item_key_type, period, units, unit_type, source, notes`);
* the "Brick & Mortar Master Forecast" Google Drive export, tab "Target
  Schedule" (`parse_bm_master_forecast`): one 7-row block per SKU with Metric
  values Stores, UPSPW, Velocity, Load_Orders, Quote, Total_Demand, Revenue over
  48 monthly columns Jan-2025..Dec-2028; Total_Demand == Velocity + Load_Orders.
  The Drive rendering is re-rowed by `shipcast.inputs.drive_export`; the tab
  "TARGET WORST CASE (DO NOT USE)" is never read. Department / category header
  rows, CATEGORY TOTAL and the arrow total rows (they omit late-added SKUs) and
  memo rows with a blank Metric are skipped. Placeholder blocks (Stores == 1),
  blocks whose 48-month Total_Demand is identical to another block's, and SKUs
  without a TCIN are flagged in `warnings`, never dropped.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from shipcast.inputs import drive_export
from shipcast.inputs.aliases import AliasMap, normalise_key
from shipcast.inputs.item_master import ItemMaster

TOOL_NATIVE_FORMAT = "tool_native_csv"
TOOL_NATIVE_COLUMNS: tuple[str, ...] = (
    "channel",
    "item_key",
    "item_key_type",
    "period",
    "units",
    "unit_type",
    "source",
    "notes",
)
BM_MASTER_FORECAST_FORMAT = "bm_master_forecast_target_schedule"
LONG_COLUMNS: tuple[str, ...] = (
    "channel",
    "sku",
    "tcin",
    "period_start",
    "forecast_units",
    "forecast_basis",
    "source_sheet",
    "as_of",
)
UNRESOLVED_COLUMNS: tuple[str, ...] = (
    "channel",
    "item_key",
    "item_key_type",
    "period",
    "units",
    "reason",
    "source_sheet",
)


@dataclass
class ParseContext:
    """What a parser needs besides the frame."""

    aliases: AliasMap
    item_master: ItemMaster
    as_of: date
    source_sheet: str
    default_channel: str = "target"


@dataclass
class ParsedOwnerForecast:
    """Long rows plus what could not be resolved and the warnings raised."""

    format: str
    rows: pd.DataFrame
    unresolved: pd.DataFrame
    warnings: list[str] = field(default_factory=list)


ParserFn = Callable[[pd.DataFrame, ParseContext], ParsedOwnerForecast]


@dataclass(frozen=True)
class FormatSpec:
    """A registered header signature and its parser."""

    name: str
    required_headers: frozenset[str]
    parser: ParserFn


PARSERS: dict[str, FormatSpec] = {}


def _norm(h: object) -> str:
    return re.sub(r"\s+", " ", str(h)).strip().lower()


def register_format(name: str, required_headers: Iterable[str], parser: ParserFn) -> None:
    """Register a parser for a header signature (normalised: lower, whitespace collapsed)."""
    PARSERS[name] = FormatSpec(name, frozenset(_norm(h) for h in required_headers), parser)


def detect_format(headers: Iterable[object]) -> FormatSpec | None:
    """The first registered format whose required headers are all present."""
    have = {_norm(h) for h in headers}
    for spec in PARSERS.values():
        if spec.required_headers.issubset(have):
            return spec
    return None


def parse_period(text: object) -> date:
    """`YYYY-MM` -> first of month; `YYYY-Www` -> the SUNDAY starting that ISO week
    (Target weeks are Sunday-anchored); `YYYY-MM-DD` -> as given."""
    s = str(text).strip()
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return date(int(s[:4]), int(s[5:7]), 1)
    m = re.fullmatch(r"(\d{4})-W(\d{2})", s, re.IGNORECASE)
    if m:
        monday = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
        return monday - timedelta(days=1)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return date.fromisoformat(s)
    raise ValueError(f"unrecognised period {text!r}; expected YYYY-MM, YYYY-Www or YYYY-MM-DD")


def _resolve_key(
    key: object, key_type: str, ctx: ParseContext
) -> tuple[str | None, int | None, str | None]:
    """-> (sku, tcin, reason_if_unresolved)."""
    kt = (key_type or "sku").strip().lower()
    im = ctx.item_master
    if kt == "tcin":
        try:
            tcin = int(str(key).strip())
        except ValueError:
            return None, None, "tcin_not_integer"
        if im.row(tcin) is None:
            return None, None, "tcin_not_in_item_master"
        return im.sku_for(tcin), tcin, None
    if kt in {"sku", "upc"}:
        res = ctx.aliases.resolve(key, kind="upc" if kt == "upc" else "sku")
        sku = res.rdz_item if res.is_pdq else res.canonical_sku
        tcins = im.tcins_for(res.canonical_sku)
        if not tcins:
            return sku, None, "sku_has_no_tcin" if res.matched or kt == "sku" else "upc_unmapped"
        if len(tcins) > 1:
            return sku, None, f"sku_maps_to_multiple_tcins:{tcins}"
        return sku, tcins[0], None
    return None, None, f"unknown_item_key_type:{key_type}"


def _unresolved(
    channel: str,
    key: object,
    key_type: str,
    period: object,
    units: object,
    reason: str,
    source: str,
) -> dict[str, Any]:
    return {
        "channel": channel,
        "item_key": key,
        "item_key_type": key_type,
        "period": period,
        "units": units,
        "reason": reason,
        "source_sheet": source,
    }


def _collapse_duplicates(
    rows: pd.DataFrame, warnings_out: list[str], *, key: list[str] | None = None
) -> pd.DataFrame:
    key = key or ["channel", "tcin", "period_start"]
    dup = rows.duplicated(subset=key, keep="first")
    if dup.any():
        offenders = rows.loc[dup, key].drop_duplicates()
        warnings_out.append(
            f"{int(dup.sum())} duplicate owner rows collapsed to the first occurrence (never summed): "
            + "; ".join("/".join(str(getattr(r, k)) for k in key) for r in offenders.itertuples())
        )
        rows = rows.loc[~dup]
    return rows.reset_index(drop=True)


def parse_tool_native(df: pd.DataFrame, ctx: ParseContext) -> ParsedOwnerForecast:
    """The tool-native CSV: one row per (channel, item, period)."""
    cols = {_norm(c): c for c in df.columns}

    def g(name: str) -> pd.Series:
        return df[cols[name]]

    out: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    warns: list[str] = []
    for i in range(len(df)):
        channel = (str(g("channel").iloc[i]).strip() or ctx.default_channel).upper()
        key = g("item_key").iloc[i]
        key_type = str(g("item_key_type").iloc[i])
        period_raw = g("period").iloc[i]
        units_raw = g("units").iloc[i]
        unit_type = str(g("unit_type").iloc[i]) if "unit_type" in cols else ""
        source = (
            str(g("source").iloc[i])
            if "source" in cols and not pd.isna(g("source").iloc[i])
            else ctx.source_sheet
        )
        try:
            period_start = parse_period(period_raw)
        except ValueError as e:
            unresolved.append(
                _unresolved(channel, key, key_type, period_raw, units_raw, str(e), source)
            )
            continue
        units = pd.to_numeric(pd.Series([units_raw]), errors="coerce").iloc[0]
        if pd.isna(units):
            unresolved.append(
                _unresolved(
                    channel, key, key_type, period_raw, units_raw, "units_not_numeric", source
                )
            )
            continue
        sku, tcin, reason = _resolve_key(key, key_type, ctx)
        if reason is not None:
            unresolved.append(
                _unresolved(channel, key, key_type, period_raw, units_raw, reason, source)
            )
            continue
        out.append(
            {
                "channel": channel,
                "sku": sku,
                "tcin": tcin,
                "period_start": period_start,
                "forecast_units": float(units),
                "forecast_basis": unit_type.strip().lower() or "units",
                "source_sheet": source,
                "as_of": ctx.as_of,
            }
        )
    rows = pd.DataFrame(out, columns=list(LONG_COLUMNS))
    rows["tcin"] = rows["tcin"].astype("Int64")
    rows = _collapse_duplicates(rows, warns)
    for w in warns:
        warnings.warn(w, stacklevel=2)
    return ParsedOwnerForecast(
        format=TOOL_NATIVE_FORMAT,
        rows=rows,
        unresolved=pd.DataFrame(unresolved, columns=list(UNRESOLVED_COLUMNS)),
        warnings=warns,
    )


register_format(TOOL_NATIVE_FORMAT, TOOL_NATIVE_COLUMNS, parse_tool_native)


# --------------------------------------------------------------------------------------
# Brick & Mortar Master Forecast, tab "Target Schedule"
# --------------------------------------------------------------------------------------

BM_TAB_NAME = "Target Schedule"
BM_IGNORED_TABS: frozenset[str] = frozenset({"TARGET WORST CASE (DO NOT USE)"})
BM_METRICS: tuple[str, ...] = (
    "Stores",
    "UPSPW",
    "Velocity",
    "Load_Orders",
    "Quote",
    "Total_Demand",
    "Revenue",
)
BM_REQUIRED_HEADERS: frozenset[str] = frozenset(
    {"Metric", "SKU / Description", "Unique Key", "Jan-2025", "Dec-2028"}
)
BM_BASES: tuple[tuple[str, str], ...] = (
    ("Velocity", "velocity"),
    ("Load_Orders", "load_orders"),
    ("Total_Demand", "total"),
)
BM_LONG_COLUMNS: tuple[str, ...] = (
    *LONG_COLUMNS,
    "description",
    "stores",
    "upspw",
    "quote_usd",
    "source_tab",
    "dept",
    "category",
    "unique_key",
)
_MONTH_HEADER_RE = re.compile(r"^([A-Z][a-z]{2})-(\d{4})$")
_DEPT_HEADER_RE = re.compile(r"^DEPT \d+ ")
_CATEGORY_HEADER_RE = re.compile(r"\((D\d+)\)\s*$")
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    )
}


def parse_bm_number(cell: object) -> float | None:
    """`-` -> 0; blank -> None (not modelled); `7,056.00`, `$12.59`, `-$602.29`, `12%` -> floats."""
    if cell is None:
        return None
    s = str(cell).strip()
    if s == "":
        return None
    if s in {"-", "–", "—"}:
        return 0.0
    neg = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.strip("()").lstrip("-").replace("$", "").replace(",", "").strip()
    pct = s.endswith("%")
    s = s.rstrip("%").strip()
    try:
        v = float(s)
    except ValueError:
        return None
    if pct:
        v /= 100.0
    return -v if neg else v


def bm_month_start(header: str) -> date | None:
    """`Sep-2026` -> date(2026, 9, 1); None for non-month headers."""
    m = _MONTH_HEADER_RE.match(header.strip())
    if not m or m.group(1) not in _MONTHS:
        return None
    return date(int(m.group(2)), _MONTHS[m.group(1)], 1)


def _bm_resolve_tcin(sku: str, ctx: ParseContext) -> tuple[int | None, str]:
    """-> (tcin, how). Rungs: alias map -> biom_sku -> vendor_style -> rdz_item -> K-/P- prefix swap."""
    res = ctx.aliases.resolve(sku, kind="sku")
    candidates = [res.canonical_sku]
    swapped = None
    if res.canonical_sku.startswith("K-"):
        swapped = "P-" + res.canonical_sku[2:]
    elif res.canonical_sku.startswith("P-"):
        swapped = "K-" + res.canonical_sku[2:]
    if swapped:
        candidates.append(swapped)
    for cand in candidates:
        for col in ("biom_sku", "vendor_style", "rdz_item"):
            hits = ctx.item_master.tcins_for(cand, columns=(col,))
            if len(hits) == 1:
                how = col if cand == res.canonical_sku else f"prefix_swap:{col}"
                if res.source != "identity":
                    how = f"{res.source}>{how}"
                return hits[0], how
            if len(hits) > 1:
                return None, f"ambiguous:{col}:{hits}"
    return None, "unresolved"


@dataclass
class BmBlock:
    """One SKU's 7-row block from the schedule tab."""

    row_index: int
    sku: str
    unique_key: str
    description: str
    dept: str | None
    category: str | None
    rows: dict[str, list[str]]


def _bm_blocks(df: pd.DataFrame) -> tuple[list[BmBlock], list[str]]:
    """Walk the tab rows and collect SKU blocks, tracking department / category context."""
    rows = df.to_numpy(dtype=object).tolist()
    warns: list[str] = []
    blocks: list[BmBlock] = []
    dept: str | None = None
    cat: str | None = None
    i = 0
    n = len(rows)
    while i < n:
        r = [str(c) if c is not None else "" for c in rows[i]]
        metric = r[0].strip()
        rest_blank = all(str(c).strip() == "" for c in r[1:])
        if metric == "Stores":
            names = [
                str(rows[i + j][0]).strip() if i + j < n else "" for j in range(len(BM_METRICS))
            ]
            if names != list(BM_METRICS):
                warns.append(
                    f"row {i}: Stores block does not have the 7 expected metrics ({names}); skipped"
                )
                i += 1
                continue
            block_rows = {
                BM_METRICS[j]: [str(c) if c is not None else "" for c in rows[i + j]]
                for j in range(len(BM_METRICS))
            }
            blocks.append(
                BmBlock(
                    row_index=i,
                    sku=normalise_key(r[1]).key,
                    unique_key=r[2].strip(),
                    description=block_rows["UPSPW"][1].strip(),
                    dept=dept,
                    category=cat,
                    rows=block_rows,
                )
            )
            i += len(BM_METRICS)
            continue
        if metric == "":
            pass  # memo row
        elif _DEPT_HEADER_RE.match(metric) and rest_blank:
            dept = metric
        elif _CATEGORY_HEADER_RE.search(metric) and rest_blank:
            cat = metric
        # CATEGORY TOTAL *, arrow totals, Days in Month, recap rows: skipped
        i += 1
    return blocks, warns


def parse_bm_master_forecast(df: pd.DataFrame, ctx: ParseContext) -> ParsedOwnerForecast:
    """Brick & Mortar Master Forecast, tab "Target Schedule", already re-rowed (see `drive_export`).

    `df` columns are the header row (`Metric, SKU / Description, Unique Key,
    Jan-2025 .. Dec-2028`). Emits one long row per SKU x month x basis
    (`velocity`, `load_orders`, `total`) with `stores`, `upspw`, `quote_usd` on
    every row; `channel = 'TARGET'`, `source_tab = 'Target Schedule'`.
    """
    cols = list(df.columns)
    month_cols = [(j, bm_month_start(str(c))) for j, c in enumerate(cols)]
    month_cols = [(j, d) for j, d in month_cols if d is not None]
    if len(month_cols) != 48:
        raise ValueError(f"expected 48 monthly columns Jan-2025..Dec-2028, found {len(month_cols)}")
    blocks, warns = _bm_blocks(df)

    out: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    identity_violations: list[str] = []
    signatures: dict[tuple[float | None, ...], str] = {}
    for b in blocks:
        tcin, how = _bm_resolve_tcin(b.sku, ctx)
        if tcin is None:
            warns.append(f"SKU_NO_TCIN {b.sku} ({how}); rows kept with tcin null")
            unresolved.append(
                _unresolved(
                    "TARGET", b.sku, "sku", "Jan-2025..Dec-2028", None, how, ctx.source_sheet
                )
            )
        stores_vals = [parse_bm_number(b.rows["Stores"][j]) for j, _ in month_cols]
        non_null_stores = [v for v in stores_vals if v is not None]
        if non_null_stores and max(non_null_stores) == 1:
            warns.append(f"PLACEHOLDER_STORES_1 {b.sku}: Stores == 1 in every modelled month")
        totals = tuple(parse_bm_number(b.rows["Total_Demand"][j]) for j, _ in month_cols)
        if any(v for v in totals if v):
            if totals in signatures:
                warns.append(
                    f"DUPLICATE_TOTAL_DEMAND {b.sku} is cell-for-cell identical to {signatures[totals]}"
                )
            else:
                signatures[totals] = b.sku
        for k, (j, month) in enumerate(month_cols):
            vel = parse_bm_number(b.rows["Velocity"][j])
            load = parse_bm_number(b.rows["Load_Orders"][j])
            tot = totals[k]
            if tot is not None and abs(tot - ((vel or 0.0) + (load or 0.0))) > 0.02:
                identity_violations.append(
                    f"{b.sku} {cols[j]}: total {tot} != velocity {vel} + load {load}"
                )
            common = {
                "channel": "TARGET",
                "sku": b.sku,
                "tcin": tcin,
                "period_start": month,
                "source_sheet": ctx.source_sheet,
                "as_of": ctx.as_of,
                "description": b.description,
                "stores": stores_vals[k],
                "upspw": parse_bm_number(b.rows["UPSPW"][j]),
                "quote_usd": parse_bm_number(b.rows["Quote"][j]),
                "source_tab": BM_TAB_NAME,
                "dept": b.dept,
                "category": b.category,
                "unique_key": b.unique_key,
            }
            for metric, basis in BM_BASES:
                out.append(
                    {
                        **common,
                        "forecast_basis": basis,
                        "forecast_units": parse_bm_number(b.rows[metric][j]),
                    }
                )
    if identity_violations:
        warns.append(
            f"TOTAL_IDENTITY {len(identity_violations)} cells where Total_Demand != Velocity + Load_Orders: {identity_violations[:5]}"
        )
    rows = pd.DataFrame(out, columns=list(BM_LONG_COLUMNS))
    rows["tcin"] = rows["tcin"].astype("Int64")
    rows = _collapse_duplicates(
        rows, warns, key=["channel", "sku", "period_start", "forecast_basis"]
    )
    for w in warns:
        warnings.warn(w, stacklevel=2)
    return ParsedOwnerForecast(
        format=BM_MASTER_FORECAST_FORMAT,
        rows=rows,
        unresolved=pd.DataFrame(unresolved, columns=list(UNRESOLVED_COLUMNS)),
        warnings=warns,
    )


register_format(BM_MASTER_FORECAST_FORMAT, BM_REQUIRED_HEADERS, parse_bm_master_forecast)


def _read_owner_xlsx(p: Path) -> pd.DataFrame:
    """Read an owner workbook: the "Target Schedule" tab when present, else the first sheet.

    Month headers arrive as datetimes from a Drive .xlsx export; they are
    rendered back to `Mon-YYYY` so `detect_format` and `bm_month_start` see the
    same header text as the Drive text rendering.
    """
    xl = pd.ExcelFile(p)
    sheet = BM_TAB_NAME if BM_TAB_NAME in xl.sheet_names else xl.sheet_names[0]
    raw = pd.read_excel(xl, sheet_name=sheet, header=None, dtype=object)
    raw = raw.dropna(how="all").reset_index(drop=True)
    header = raw.iloc[0].tolist()
    cols: list[str] = []
    for c in header:
        if isinstance(c, datetime | date):
            cols.append(pd.Timestamp(c).strftime("%b-%Y"))
        elif c is None or (isinstance(c, float) and pd.isna(c)):
            cols.append("")
        else:
            cols.append(str(c).strip())
    body = raw.iloc[1:].reset_index(drop=True)
    body.columns = cols
    return body.astype(object).where(body.notna(), "")


def load_owner_forecast(
    path: str | Path,
    *,
    aliases: AliasMap,
    item_master: ItemMaster,
    as_of: date,
    default_channel: str = "target",
) -> ParsedOwnerForecast:
    """Read a CSV/XLSX, detect its format by header, and parse to long form."""
    p = Path(path)
    if p.suffix.lower() in {".xlsx", ".xlsm"}:
        df = _read_owner_xlsx(p)
    else:
        text = p.read_text(encoding="utf-8")
        if drive_export.is_drive_export(text):
            # Brick & Mortar Master Forecast rendering: take ONLY the Target Schedule tab.
            df = drive_export.schedule_tab_frame(text, BM_TAB_NAME)
        else:
            df = pd.read_csv(p, dtype=str, keep_default_na=False)
    spec = detect_format(df.columns)
    if spec is None:
        raise ValueError(
            f"{p.name}: no registered owner-forecast format matches headers {list(df.columns)}. "
            f"Registered: {sorted(PARSERS)}"
        )
    ctx = ParseContext(
        aliases=aliases,
        item_master=item_master,
        as_of=as_of,
        source_sheet=p.name,
        default_channel=default_channel,
    )
    return spec.parser(df, ctx)
