"""Re-rowing Google Drive's single-line export of a multi-tab Google Sheet.

The Drive connector renders a workbook as ONE string with no newlines: each
tab is Google-Sheets-style CSV with row breaks replaced by single spaces, and
tabs are concatenated as `<last cell of previous tab> <TabName> <first cell of
next tab>`. Recovering rows therefore needs three facts about the tab:

1. its name (to find the start),
2. its fixed column width (51 for the retailer schedule tabs), and
3. CSV quoting rules (a quote may start at the beginning of a token OR right
   after the space that replaced a row break).

`tokenize_csv` splits a tab's text on top-level commas; `rows_from_tokens`
rebuilds rows of a fixed width, splitting each merged boundary token
(`<last cell> <first cell>`) on its first space. Markdown escapes
(`\\_ \\# \\! \\< \\> \\[ \\] \\& \\*`) are undone by `unescape_markdown`.

Ported from the scratchpad prototype `parse_bm.py` (2026-09-04) with the same
behaviour; tab boundaries are located by text rather than by hard-coded offsets.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import pandas as pd

SCHEDULE_HEADER_PREFIX = "Metric,SKU / Description,Unique Key,Jan-2025"
SCHEDULE_WIDTH = 51  # Metric, SKU / Description, Unique Key + 48 months Jan-2025..Dec-2028

KNOWN_TABS: tuple[str, ...] = (
    "README",
    "Assumptions",
    "Summary",
    "Trade Expenses",
    "Revenue by SKU",
    "Demand by SKU",
    "Pricing by SKU",
    "Target Schedule",
    "TARGET WORST CASE (DO NOT USE)",
    "SKU Reference",
    "AAFES Schedule",
    "Costco Schedule",
    "Fresh Thyme Schedule",
    "Grove Schedule",
    "HEB Schedule",
    "Home Depot Schedule",
    "Kroger Schedule",
    "Lowes Schedule",
    "Lowes Foods Schedule",
    "Meijer Schedule",
    "Michaels Schedule",
    "Paradies Schedule",
    "Publix Schedule",
    "Sprouts Schedule",
    "Staples B2B Schedule",
    "Thrive Schedule",
    "UNFI WFM Schedule",
    "Walmart Schedule",
)
"""Tab names of the Brick & Mortar Master Forecast workbook (2026-09-04)."""

_ESCAPE_RE = re.compile(r"\\([_\[\]#*&!\\().+\-`~>|{}<])")


class DriveExportError(ValueError):
    """The export text does not have the shape the parser expects."""


def unescape_markdown(text: str) -> str:
    """`TARGET\\_P-DIS-TER` -> `TARGET_P-DIS-TER`; also `\\# \\! \\< \\> \\[ \\] \\& \\*`."""
    return _ESCAPE_RE.sub(r"\1", text)


def is_drive_export(text: str) -> bool:
    """One line (no row breaks) that contains a schedule header: the Drive rendering."""
    return "\n" not in text.strip() and SCHEDULE_HEADER_PREFIX in text


def tokenize_csv(segment: str) -> list[str]:
    """Split on top-level commas. Quotes may open at a token start or after a space.

    `""` inside a quoted token is a literal quote. Returns the raw tokens; the
    boundary tokens between rows still carry `<last cell> <first cell>`.
    """
    toks: list[str] = []
    cur: list[str] = []
    i, n = 0, len(segment)
    in_quotes = False
    prev = ","
    while i < n:
        ch = segment[i]
        if in_quotes:
            if ch == '"':
                if i + 1 < n and segment[i + 1] == '"':
                    cur.append('"')
                    i += 2
                    continue
                in_quotes = False
                i += 1
                continue
            cur.append(ch)
            i += 1
            continue
        if ch == '"' and prev in {",", " "}:
            in_quotes = True
            prev = ch
            i += 1
            continue
        if ch == ",":
            toks.append("".join(cur))
            cur = []
            prev = ","
            i += 1
            continue
        cur.append(ch)
        prev = ch
        i += 1
    toks.append("".join(cur))
    return toks


def rows_from_tokens(tokens: Sequence[str], ncol: int) -> list[list[str]]:
    """Rebuild fixed-width rows; boundary tokens split on their first space.

    Raises `DriveExportError` when the token count is not consistent with `ncol`.
    """
    per = ncol - 1
    if per <= 0:
        raise ValueError("ncol must be >= 2")
    if (len(tokens) - 1) % per != 0:
        raise DriveExportError(
            f"{len(tokens)} tokens do not form rows of width {ncol} "
            f"((len-1) % {per} = {(len(tokens) - 1) % per}); wrong tab or wrong width"
        )
    nrows = (len(tokens) - 1) // per
    rows: list[list[str]] = []
    cur: list[str] = [tokens[0]]
    for r in range(nrows):
        cur.extend(tokens[1 + r * per : 1 + (r + 1) * per - 1])
        boundary = tokens[1 + (r + 1) * per - 1]
        if r == nrows - 1:
            cur.append(boundary)
            rows.append(cur)
            break
        last, first = boundary.split(" ", 1) if " " in boundary else (boundary, "")
        cur.append(last)
        rows.append(cur)
        cur = [first]
    return rows


def find_tab(text: str, name: str, *, known_tabs: Sequence[str] = KNOWN_TABS) -> str | None:
    """The CSV text of tab `name`, or None if the tab is absent.

    Start: `<name> ` at the beginning of the string or after a space. End: the
    earliest ` <other known tab> ` after the start (followed by a non-space), or
    the end of the string.
    """
    m = re.search(rf"(?:^| ){re.escape(name)} (?=\S)", text)
    if not m:
        return None
    start = m.end()
    end = len(text)
    for other in known_tabs:
        if other == name:
            continue
        mm = re.search(rf" {re.escape(other)} (?=\S)", text[start:])
        if mm:
            end = min(end, start + mm.start())
    return text[start:end]


def schedule_tab_text(text: str, tab_name: str) -> str:
    """The schedule tab's CSV text: the whole string if it already starts with the header."""
    if text.lstrip().startswith(SCHEDULE_HEADER_PREFIX):
        return text.strip()
    seg = find_tab(text, tab_name)
    if seg is None:
        raise DriveExportError(f"tab {tab_name!r} not found in the export")
    if not seg.startswith(SCHEDULE_HEADER_PREFIX):
        raise DriveExportError(f"tab {tab_name!r} does not start with the schedule header")
    return seg


def schedule_tab_frame(text: str, tab_name: str, *, width: int = SCHEDULE_WIDTH) -> pd.DataFrame:
    """Re-row a schedule tab into a DataFrame whose columns are the (unescaped) header row."""
    seg = schedule_tab_text(text, tab_name)
    rows = rows_from_tokens(tokenize_csv(seg), width)
    header = [unescape_markdown(c).strip() for c in rows[0]]
    body = [[unescape_markdown(c) for c in r] for r in rows[1:]]
    return pd.DataFrame(body, columns=header)
