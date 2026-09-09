"""Config-driven workbook layout.

`config/report_target.yaml` says which sheets exist, in what order, and how each
is laid out. Changing the workbook's shape is a YAML edit, not a code change:

    sheets:
      - name: Monthly
        kind: wide                 # readme | long | wide | blocks
        source: monthly            # a frame key from the ForecastBundle
        index: [sku, tcin, description]
        column_from: month_start   # pivoted into columns
        column_format: "%b-%y"     # strftime for the pivoted headers
        value: expected_ship_units
        agg: sum                   # how to combine rows sharing index x column
        limit_columns: 16
        companion:                 # extra grids stacked below the value grid
          - {title: "Confidence grade", value: grade, agg: worst}
          - {title: "Low (P10 or band)", value: low, agg: sum}
      - name: Monthly detail
        kind: long
        source: monthly
        columns: [sku, tcin, month, stream, expected_ship_units, low, high, grade, confidence]
        labels: {expected_ship_units: "Expected shipments (units)"}
        sort: [tcin, month_start]

`kind: blocks` stacks several long tables on one sheet (used for Accuracy).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from pipelines.target_shipment_forecast.config import repo_root
from pipelines.target_shipment_forecast.model.grade import GRADE_ORDER, worse
from pipelines.target_shipment_forecast.output.workbook import _cell_value, _number_format, safe_sheet_name

DEFAULT_SPEC = repo_root() / "config" / "report_target.yaml"


def load_spec(path: Path | None = None) -> dict[str, Any]:
    """Read the report spec YAML."""
    with open(path or DEFAULT_SPEC, encoding="utf-8") as fh:
        spec = yaml.safe_load(fh)
    if not isinstance(spec, dict) or "sheets" not in spec:
        raise ValueError("report spec must be a mapping with a `sheets` list")
    return spec


# --------------------------------------------------------------------------------------
# frame shaping
# --------------------------------------------------------------------------------------


def _worst(series: pd.Series) -> str:
    vals = [str(v) for v in series.dropna() if str(v) in GRADE_ORDER]
    if not vals:
        return ""
    out = vals[0]
    for v in vals[1:]:
        out = worse(out, v)
    return out


def _agg_fn(name: str) -> Any:
    return {
        "sum": "sum",
        "mean": "mean",
        "max": "max",
        "min": "min",
        "first": "first",
        "worst": _worst,
    }.get(name, "sum")


def shape_long(df: pd.DataFrame, s: Mapping[str, Any]) -> pd.DataFrame:
    """Select, filter, sort and relabel columns per a `long` spec."""
    out = df.copy()
    flt = s.get("filter")
    if isinstance(flt, Mapping):
        for col, allowed in flt.items():
            if col in out:
                allowed_list = allowed if isinstance(allowed, list) else [allowed]
                out = out[out[col].isin(allowed_list)]
    sort = s.get("sort")
    if sort:
        cols = [c for c in sort if c in out]
        if cols:
            out = out.sort_values(cols)
    cols = [c for c in s.get("columns", list(out.columns)) if c in out]
    out = out[cols]
    labels = s.get("labels") or {}
    return out.rename(columns={k: v for k, v in labels.items() if k in out})


def shape_wide(
    df: pd.DataFrame, s: Mapping[str, Any], *, value: str | None = None, agg: str | None = None
) -> pd.DataFrame:
    """Pivot a long frame into index x (pivoted column) with formatted headers.

    Built with groupby + unstack (never `pivot_table(dropna=False)`, which
    expands the index levels into their cartesian product and produced a 58 MB
    workbook on the first live run).
    """
    idx = [c for c in s["index"] if c in df]
    col = s["column_from"]
    val = value or s["value"]
    fn = _agg_fn(agg or s.get("agg", "sum"))
    d = df.copy()
    flt = s.get("filter")
    if isinstance(flt, Mapping):
        for c, allowed in flt.items():
            if c in d:
                d = d[d[c].isin(allowed if isinstance(allowed, list) else [allowed])]
    if val not in d or d.empty:
        return pd.DataFrame(columns=idx)
    for c in idx:
        d[c] = d[c].where(d[c].notna(), "")
    g = d.groupby([*idx, col], dropna=False)[val].agg(fn)
    piv = g.unstack(col)
    piv = piv.reindex(sorted(piv.columns), axis=1)
    limit = s.get("limit_columns")
    if limit:
        piv = piv.iloc[:, : int(limit)]
    fmt = s.get("column_format")
    if fmt:
        piv.columns = [
            pd.Timestamp(c).strftime(fmt) if not isinstance(c, str) else c for c in piv.columns
        ]
    else:
        piv.columns = [str(c) for c in piv.columns]
    out = piv.reset_index()
    if s.get("total_row", False) and fn == "sum":
        total = {c: (out[c].sum() if c not in idx else "") for c in out.columns}
        total[idx[0]] = "TOTAL"
        out = pd.concat([out, pd.DataFrame([total])], ignore_index=True)
    return out


# --------------------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------------------


def _write_block(
    ws: Any,
    df: pd.DataFrame,
    *,
    title: str | None,
    start_row: int,
    number_format: str | None = None,
) -> int:
    """Write an optional title row then the frame; return the next free row."""
    r = start_row
    if title:
        ws.cell(row=r, column=1, value=title).font = Font(bold=True, size=12)
        r += 1
    ws.append([]) if False else None  # keep openpyxl happy about explicit rows
    for j, c in enumerate(df.columns, start=1):
        cell = ws.cell(row=r, column=j, value=str(c))
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="top", wrap_text=True)
    formats = {
        j: (number_format or _number_format(df[c])) for j, c in enumerate(df.columns, start=1)
    }
    r += 1
    for row in df.itertuples(index=False, name=None):
        for j, v in enumerate(row, start=1):
            cell = ws.cell(row=r, column=j, value=_cell_value(v))
            fmt = formats.get(j)
            if fmt and isinstance(cell.value, int | float):
                cell.number_format = fmt
        r += 1
    return r + 1


def _autowidth(ws: Any, frames: Sequence[pd.DataFrame]) -> None:
    widths: dict[int, int] = {}
    for df in frames:
        for j, c in enumerate(df.columns, start=1):
            sample = (
                df[c]
                .head(300)
                .map(
                    lambda v: (
                        0
                        if v is None or v is pd.NA or (isinstance(v, float) and v != v)
                        else len(str(v))
                    )
                )
            )
            widths[j] = max(widths.get(j, 0), len(str(c)), int(sample.max()) if len(sample) else 0)
    for j, w in widths.items():
        ws.column_dimensions[get_column_letter(j)].width = max(8, min(48, w + 2))


def _readme_frame(readme: Mapping[str, Any]) -> pd.DataFrame:
    rows = []
    for k, v in readme.items():
        if isinstance(v, Mapping):
            v = "; ".join(f"{a}={b}" for a, b in v.items())
        elif isinstance(v, list | tuple):
            v = ", ".join(map(str, v))
        rows.append({"key": str(k), "value": v})
    return pd.DataFrame(rows)


def render_workbook(
    frames: Mapping[str, pd.DataFrame],
    readme: Mapping[str, Any],
    *,
    out_path: str | Path,
    spec: Mapping[str, Any] | None = None,
    spec_path: Path | None = None,
) -> Path:
    """Write the workbook described by the spec. Missing frames are skipped with a note in README."""
    sp = spec or load_spec(spec_path)
    wb = Workbook()
    wb.remove(wb.active)
    taken: set[str] = set()
    notes: list[str] = []
    for s in sp["sheets"]:
        kind = s.get("kind", "long")
        name = safe_sheet_name(s["name"], taken)
        ws = wb.create_sheet(title=name)
        blocks: list[pd.DataFrame] = []
        r = 1
        if kind == "readme":
            rd = _readme_frame(readme)
            r = _write_block(ws, rd, title=s.get("title"), start_row=r)
            blocks.append(rd)
            for extra in s.get("append_frames", []) or []:
                if extra in frames and not frames[extra].empty:
                    r = _write_block(
                        ws,
                        frames[extra],
                        title=s.get("append_titles", {}).get(extra, extra),
                        start_row=r,
                    )
                    blocks.append(frames[extra])
            if notes:
                r = _write_block(ws, pd.DataFrame({"note": notes}), title="Notes", start_row=r)
        elif kind == "long":
            src = frames.get(s["source"])
            if src is None or src.empty:
                notes.append(f"{s['name']}: no data for frame {s['source']}")
                ws.cell(row=1, column=1, value=f"No data for {s['source']}")
                continue
            df = shape_long(src, s)
            r = _write_block(
                ws, df, title=s.get("title"), start_row=r, number_format=s.get("number_format")
            )
            blocks.append(df)
        elif kind == "wide":
            src = frames.get(s["source"])
            if src is None or src.empty:
                notes.append(f"{s['name']}: no data for frame {s['source']}")
                ws.cell(row=1, column=1, value=f"No data for {s['source']}")
                continue
            main = shape_wide(src, s)
            r = _write_block(
                ws, main, title=s.get("title"), start_row=r, number_format=s.get("number_format")
            )
            blocks.append(main)
            for comp in s.get("companion", []) or []:
                cdf = shape_wide(
                    src, {**s, "total_row": False}, value=comp["value"], agg=comp.get("agg", "sum")
                )
                r = _write_block(
                    ws,
                    cdf,
                    title=comp.get("title", comp["value"]),
                    start_row=r,
                    number_format=comp.get("number_format"),
                )
                blocks.append(cdf)
        elif kind == "blocks":
            for blk in s.get("blocks", []):
                src = frames.get(blk["source"])
                if src is None or src.empty:
                    continue
                df = shape_long(src, blk)
                r = _write_block(
                    ws,
                    df,
                    title=blk.get("title", blk["source"]),
                    start_row=r,
                    number_format=blk.get("number_format"),
                )
                blocks.append(df)
            if not blocks:
                ws.cell(row=1, column=1, value="No data")
                continue
        else:
            raise ValueError(f"unknown sheet kind {kind!r} for {s['name']}")
        if blocks:
            _autowidth(ws, blocks)
            if s.get("freeze", True):
                ws.freeze_panes = s.get(
                    "freeze_at",
                    "A2"
                    if kind != "wide"
                    else f"{get_column_letter(len([c for c in s.get('index', []) if True]) + 1)}2",
                )
    if "README" in taken and notes:
        # README was written before later notes accrued; append them.
        ws = wb["README"]
        _write_block(ws, pd.DataFrame({"note": notes}), title="Notes", start_row=ws.max_row + 2)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


def write_csvs(frames: Mapping[str, pd.DataFrame], out_dir: str | Path) -> list[Path]:
    """Write every frame as CSV next to the workbook."""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    paths = []
    for k, v in frames.items():
        p = d / f"{k}.csv"
        v.to_csv(p, index=False)
        paths.append(p)
    return paths


__all__ = ["DEFAULT_SPEC", "load_spec", "render_workbook", "shape_long", "shape_wide", "write_csvs"]
