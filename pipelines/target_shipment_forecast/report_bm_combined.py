#!/usr/bin/env python3
"""Build the combined Target SKU-level demand forecast: .xlsx (primary) + .html.

    python3 -m pipelines.target_shipment_forecast.report_bm_combined \
        --bm "/path/to/Brick & Mortar Master Forecast.xlsx" \
        --run runs/2026-09-08 --out runs/bm_combined

Reads the BPD-measured forecast from a PUBLISHED Shipcast run (its `csv/monthly.csv`)
and the B&M master forecast from a local .xlsx, combines them per model/bm_combine.py,
and writes both artifacts plus one CSV per frame.

WHY IT READS A PUBLISHED RUN RATHER THAN RE-RUNNING THE PIPELINE. The B&M sheet is not
in the warehouse -- it arrives by hand -- so nothing here can be scheduled yet, and
re-running `simulate` with a ramped POS path would change the production monthly grid on
the strength of a file with no ingestion path and no snapshot archive. This builder is
therefore additive and side-effect free: the production run is untouched and the combine
is reproducible from its committed CSVs. A live daily/weekly Drive pull (the RDZ pattern
in pipelines/rdz_inventory/) is the separate follow-on that would change that.
"""

from __future__ import annotations

import argparse
import html
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from pipelines.target_shipment_forecast.config import load_config
from pipelines.target_shipment_forecast.inputs.bm_master_forecast import parse_target_schedule
from pipelines.target_shipment_forecast.inputs.item_master import ItemMaster
from pipelines.target_shipment_forecast.model import bm_combine as bc
from pipelines.target_shipment_forecast.output.workbook import write_workbook

# Streams whose units are POs Target has cut or planned: facts, never scaled.
FACT_STREAMS = ("booked_forward", "planned_forward", "planned_launch", "created")

PALETTE = {
    # dataviz categorical slots 1/2/3, validated both modes 2026-09-09
    bc.AUTH_MEASURED: ("#2a78d6", "#3987e5"),
    bc.AUTH_SHAPED: ("#eb6834", "#d95926"),
    bc.AUTH_STATED: ("#1baf7a", "#199e70"),
}
AUTH_LABEL = {
    bc.AUTH_MEASURED: "Measured (BPD)",
    bc.AUTH_SHAPED: "Measured, shaped by the B&M store plan",
    bc.AUTH_STATED: "B&M stated only (no BPD signal)",
}


def _month(s: object) -> pd.Timestamp:
    """`Sep-26` (the run's own label) -> a month-start Timestamp."""
    return pd.Timestamp(datetime.strptime(str(s), "%b-%y").date().replace(day=1))


def load_run_monthly(run_dir: Path) -> pd.DataFrame:
    """The published monthly frame, one row per TCIN x month x stream."""
    p = run_dir / "csv" / "monthly.csv"
    if not p.exists():
        raise SystemExit(f"ABORT: {p} not found; pass --run <a published run directory>")
    df = pd.read_csv(p)
    for c in ("tcin", "month", "stream", "grade", "expected_ship_units"):
        if c not in df.columns:
            raise SystemExit(f"ABORT: {p} has no '{c}' column; columns are {list(df.columns)}")
    out = pd.DataFrame(
        {
            "tcin": pd.to_numeric(df["tcin"], errors="coerce").astype("Int64").astype("int64"),
            "month_start": df["month"].map(_month),
            "stream": df["stream"].astype(str),
            "units": pd.to_numeric(df["expected_ship_units"], errors="coerce").fillna(0.0),
            "grade": df["grade"].astype(str).str.strip().str[:1].replace({"": "E", "nan": "E"}),
            "pos_forecast_units": pd.to_numeric(
                df.get("pos_forecast_units"), errors="coerce"
            ).fillna(0.0),
        }
    )
    if out.empty:
        raise SystemExit(f"ABORT: {p} has no rows")
    return out


def build(bm_path: Path, run_dir: Path, out_dir: Path, *, as_of: date | None = None) -> dict[str, Path]:
    cfg = load_config()
    bands = {k: float(v["band_pct"]) for k, v in cfg["grades"]["labels"].items()}
    im = ItemMaster.load()

    bm = parse_target_schedule(bm_path, item_master=im)
    monthly = load_run_monthly(run_dir)
    run_as_of = as_of or _infer_as_of(run_dir)
    anchor = pd.Timestamp(run_as_of.replace(day=1))
    if anchor not in set(bm.frame["month_start"]):
        raise SystemExit(
            f"ABORT: the run's as-of month {anchor:%Y-%m} is not a column in the B&M sheet "
            f"({bm.months[0]:%b-%Y}..{bm.months[-1]:%b-%Y}). The store ramp has no denominator."
        )

    ramp = bc.store_ramp(bm.frame, anchor)
    combined = bc.apply_ramp(monthly, ramp)

    # B&M-only rows: TCIN-months the published run does not reach at all. Its horizon
    # ends Dec-27; the sheet reaches Dec-28, and the pre-launch items have no BPD row
    # in any month.
    covered = {(int(r.tcin), pd.Timestamp(r.month_start)) for r in monthly.itertuples(index=False)}
    fwd_months = [m for m in sorted(set(bm.frame["month_start"])) if m >= anchor]
    only = bc.bm_only_rows(bm.frame, covered, months=fwd_months)
    detail = (
        combined
        if only.empty
        else pd.concat([combined, only.reindex(columns=combined.columns)], ignore_index=True)
    )

    detail["sku"] = [im.sku_for(int(t)) or "" for t in detail["tcin"]]
    detail["description"] = [
        (im.row(int(t))["target_description"] if im.row(int(t)) is not None else "")
        for t in detail["tcin"]
    ]
    detail["band_pct"] = detail["grade"].map(bands).fillna(1.0)
    detail["low"] = (detail["units"] * (1 - detail["band_pct"])).clip(lower=0)
    detail["high"] = detail["units"] * (1 + detail["band_pct"])
    detail["month"] = detail["month_start"].dt.strftime("%b-%y")
    detail = detail.sort_values(["tcin", "month_start", "stream"]).reset_index(drop=True)

    # Load_Orders cross-check against what the run already carries as a fact.
    fact = (
        monthly.loc[monthly["stream"].isin(FACT_STREAMS)]
        .groupby(["tcin", "month_start"], as_index=False)["units"]
        .sum()
        .rename(columns={"units": "bpd_units"})
    )
    # The overlap window: months the published run actually covers. Without it the
    # sheet's whole pre-run load history reads as units BPD is missing.
    overlap = sorted(set(monthly["month_start"]) & set(bm.frame["month_start"]))
    verdicts = bc.load_order_verdicts(bm.frame, fact, months=overlap)

    frames = {
        "Monthly": _monthly_grid(detail),
        "Monthly detail": _detail_sheet(detail),
        "B&M vs BPD stores": _store_compare(bm.frame, ramp, monthly, anchor, im),
        "Load order check": verdicts,
        "Coverage": _coverage(bm, im, monthly),
        "Exceptions": _exceptions(bm, detail, verdicts),
    }
    readme = _readme(bm, run_dir, run_as_of, anchor, detail, monthly, verdicts, cfg, overlap)
    # A TCIN is an identifier, not a quantity: render it as text so neither Excel's
    # #,##0 nor the HTML formatter turns 89854821 into "89,854,821".
    frames = {k: _tcin_as_text(v) for k, v in frames.items()}

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = run_as_of.isoformat()
    xlsx = write_workbook(out_dir / f"target_demand_forecast_combined_{stamp}.xlsx", frames, readme)
    htmlp = out_dir / f"target_demand_forecast_combined_{stamp}.html"
    htmlp.write_text(_html(frames, readme, detail), encoding="utf-8")
    csv_dir = out_dir / "csv"
    csv_dir.mkdir(exist_ok=True)
    written = {"xlsx": xlsx, "html": htmlp}
    for name, df in frames.items():
        p = csv_dir / (name.lower().replace(" ", "_").replace("&", "and") + ".csv")
        df.to_csv(p, index=False)
        written[name] = p
    (out_dir / "readme.json").write_text(json.dumps(readme, indent=2, default=str), encoding="utf-8")
    return written


def _tcin_as_text(df: pd.DataFrame) -> pd.DataFrame:
    if "tcin" not in df.columns:
        return df
    out = df.copy()
    out["tcin"] = [
        "" if pd.isna(t) or str(t).strip() == "" else str(int(float(t))) for t in out["tcin"]
    ]
    return out


def _infer_as_of(run_dir: Path) -> date:
    try:
        return date.fromisoformat(run_dir.name)
    except ValueError as exc:
        raise SystemExit(
            f"ABORT: cannot read an as-of date from run directory name {run_dir.name!r}; "
            "pass --as-of YYYY-MM-DD"
        ) from exc


def _monthly_grid(detail: pd.DataFrame) -> pd.DataFrame:
    """Units by item x month, with a stacked grade grid underneath (worst of its streams)."""
    order = sorted(set(detail["month_start"]))
    labels = [m.strftime("%b-%y") for m in order]
    units = (
        detail.pivot_table(index=["tcin", "sku"], columns="month", values="units", aggfunc="sum")
        .reindex(columns=labels)
        .fillna(0.0)
        .round(0)
        .reset_index()
    )
    units.insert(2, "block", "UNITS")
    worst = (
        detail.assign(_g=detail["grade"].map(lambda g: bc.GRADE_ORDER.index(str(g)[:1])
                                             if str(g)[:1] in bc.GRADE_ORDER else 4))
        .pivot_table(index=["tcin", "sku"], columns="month", values="_g", aggfunc="max")
        .reindex(columns=labels)
        .reset_index()
    )
    for c in labels:
        worst[c] = worst[c].map(lambda i: bc.GRADE_ORDER[int(i)] if pd.notna(i) else "")
    worst.insert(2, "block", "GRADE (worst of streams)")
    return pd.concat([units, worst], ignore_index=True)


def _detail_sheet(detail: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "tcin", "sku", "description", "month", "stream", "units_bpd", "ramp", "units",
        "authority", "grade", "band_pct", "low", "high", "bm_stores", "bm_stores_anchor",
        "ramp_flag",
    ]
    out = detail.reindex(columns=cols).copy()
    for c in ("units_bpd", "units", "low", "high"):
        out[c] = pd.to_numeric(out[c], errors="coerce").round(1)
    out["ramp"] = pd.to_numeric(out["ramp"], errors="coerce").round(4)
    return out


def _store_compare(
    bm_frame: pd.DataFrame, ramp: pd.DataFrame, monthly: pd.DataFrame,
    anchor: pd.Timestamp, im: ItemMaster,
) -> pd.DataFrame:
    """What the sheet says about doors vs what BPD measured, at the anchor month."""
    a = ramp.loc[ramp["month_start"] == anchor, ["tcin", "bm_stores", "bm_upspw"]].copy()
    peak = ramp.groupby("tcin", as_index=False)["bm_stores"].max().rename(
        columns={"bm_stores": "bm_stores_plan_peak"}
    )
    peak_month = (
        ramp.sort_values(["tcin", "bm_stores", "month_start"])
        .groupby("tcin", as_index=False)
        .last()[["tcin", "month_start"]]
        .rename(columns={"month_start": "bm_peak_month"})
    )
    out = a.merge(peak, on="tcin").merge(peak_month, on="tcin")
    denom = out["bm_stores"].astype(float).where(out["bm_stores"].astype(float) > 0)
    out["bm_ramp_to_peak"] = (out["bm_stores_plan_peak"].astype(float) / denom).round(4)
    out["sku"] = [im.sku_for(int(t)) or "" for t in out["tcin"]]
    out["bm_peak_month"] = out["bm_peak_month"].dt.strftime("%b-%y")
    moves = ramp.groupby("tcin")["ramp"].nunique().rename("ramp_distinct_values")
    out = out.merge(moves, on="tcin", how="left")
    flags = (
        ramp.loc[ramp["ramp_flag"] != ""].groupby("tcin")["ramp_flag"]
        .agg(lambda s: "|".join(sorted(set(s)))).rename("ramp_flag")
    )
    out = out.merge(flags, on="tcin", how="left").fillna({"ramp_flag": ""})
    return out[
        ["tcin", "sku", "bm_stores", "bm_upspw", "bm_stores_plan_peak", "bm_peak_month",
         "bm_ramp_to_peak", "ramp_distinct_values", "ramp_flag"]
    ].sort_values("bm_stores_plan_peak", ascending=False).reset_index(drop=True)


def _coverage(bm, im: ItemMaster, monthly: pd.DataFrame) -> pd.DataFrame:
    """Scope overlap, stated rather than assumed."""
    bm_t = set(bm.tcins)
    all_t = set(im.tcins)
    run_t = {int(t) for t in monthly["tcin"]}
    rows = []
    for t in sorted(all_t | bm_t):
        r = im.row(int(t))
        rows.append(
            {
                "tcin": t,
                "sku": (im.sku_for(int(t)) or ""),
                "item_state": (r["item_state"] if r is not None else ""),
                "in_bm_sheet": t in bm_t,
                "in_item_master": t in all_t,
                "in_published_run": t in run_t,
            }
        )
    df = pd.DataFrame(rows)
    extra = pd.DataFrame(
        [
            {
                "tcin": None, "sku": u["bm_sku"], "item_state": u["reason"],
                "in_bm_sheet": True, "in_item_master": False, "in_published_run": False,
            }
            for u in bm.unresolved
        ]
    )
    out = pd.concat([df, extra], ignore_index=True) if not extra.empty else df
    # A B&M SKU with no TCIN has no TCIN: render it blank, not as the string "<NA>".
    out["tcin"] = [("" if pd.isna(t) else str(int(t))) for t in out["tcin"]]
    return out


def _exceptions(bm, detail: pd.DataFrame, verdicts: pd.DataFrame) -> pd.DataFrame:
    rows = [{"issue": "BM_SHEET_WARNING", "subject": bm.source_file, "detail": w} for w in bm.warnings]
    rows += [
        {"issue": u["reason"].split(":")[0], "subject": u["bm_sku"], "detail": u["reason"] + " | " + str(u["description"])}
        for u in bm.unresolved
    ]
    f = detail.loc[detail["ramp_flag"].astype(str) != ""]
    for (flag, tcin), g in f.groupby(["ramp_flag", "tcin"]):
        rows.append(
            {
                "issue": str(flag),
                "subject": f"{int(tcin)} {g['sku'].iloc[0]}",
                "detail": f"{len(g)} month(s), {g['month'].iloc[0]}..{g['month'].iloc[-1]}",
            }
        )
    for v in verdicts.loc[verdicts["verdict"] != "BM_LOAD_AGREES"].itertuples(index=False):
        rows.append(
            {"issue": v.verdict, "subject": f"{int(v.tcin)} {v.bm_sku}", "detail": v.note}
        )
    return pd.DataFrame(rows).sort_values(["issue", "subject"]).reset_index(drop=True)


def _readme(bm, run_dir, as_of, anchor, detail, monthly, verdicts, cfg, overlap) -> dict[str, object]:
    shaped = detail.loc[detail["authority"] == bc.AUTH_SHAPED, "units"].sum()
    stated = detail.loc[detail["authority"] == bc.AUTH_STATED, "units"].sum()
    measured = detail.loc[detail["authority"] == bc.AUTH_MEASURED, "units"].sum()
    shaped_rows = detail.loc[detail["authority"] == bc.AUTH_SHAPED]
    ramp_delta = float((shaped_rows["units"] - shaped_rows["units_bpd"]).sum())
    return {
        "report": "Target SKU-level demand forecast - BPD measured level x B&M stated store plan",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": as_of.isoformat(),
        "bpd_source": f"{run_dir}/csv/monthly.csv (published Shipcast run, unmodified)",
        "bpd_estimator": cfg["consumption"]["pos_estimator"]["value"],
        "bm_source_file": bm.source_file,
        "bm_snapshot_date": f"{bm.snapshot_date} (file mtime, never today())",
        "bm_tab": "Target Schedule",
        "bm_months": f"{bm.months[0]:%b-%Y}..{bm.months[-1]:%b-%Y} ({len(bm.months)} columns)",
        "bm_sku_blocks": int(bm.frame["bm_sku"].nunique()),
        "bm_tcins_resolved": len(bm.tcins),
        "bm_skus_unresolved": len(bm.unresolved),
        "store_ramp_anchor": f"{anchor:%b-%Y} (the run's as-of month; the ramp's denominator)",
        "store_ramp_rule": "units(replenishment) x bm_stores(M)/bm_stores(anchor); fact streams never scaled",
        "load_orders_rule": "CROSS-CHECK ONLY - never moves a unit BPD already carries",
        "load_orders_window": f"{overlap[0]:%b-%Y}..{overlap[-1]:%b-%Y} "
                              f"({len(overlap)} months both sources cover)",
        "load_order_verdicts": verdicts["verdict"].value_counts().to_dict() if len(verdicts) else {},
        "units_measured": round(float(measured)),
        "units_measured_shaped_by_plan": round(float(shaped)),
        "units_added_by_the_store_ramp": f"{ramp_delta:+,.0f} "
            f"({ramp_delta / (shaped - ramp_delta) * 100:+.1f}% on the BPD level of those rows)"
            if shaped - ramp_delta else f"{ramp_delta:+,.0f}",
        "units_stated_only_graded_E": round(float(stated)),
        "grade_penalty": f"one grade worse where the B&M ramp moved units by >{bc.RAMP_MATERIAL:.0%}; "
                         "B&M-only rows are E and never better",
        "drive_dependency": "NONE - the B&M file is read from a local path supplied on the "
                            "command line. A scheduled Drive pull is a separate follow-on task.",
        "judgment_call": "Monthly replenishment units are scaled LINEARLY in the store ramp. That "
                         "is exact in steady state (the controller converges orders -> sales) and "
                         "approximate through a ramp. The exact treatment re-runs simulate() with a "
                         "ramped POS path and needs the live BQ pull.",
        "sheet_warnings": len(bm.warnings),
    }


def _fmt(v: object) -> str:
    """Missing renders as an em dash, never as the string 'nan'. Checked FIRST: a NaN is
    a float, so a float branch placed above this one prints it verbatim."""
    if v is None or v is pd.NA or (isinstance(v, float) and v != v):
        return "&mdash;"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.4g}"
    if isinstance(v, int):
        return f"{v:,}"
    s = str(v)
    return "&mdash;" if s in ("nan", "<NA>", "NaT", "") else html.escape(s)


def _table(df: pd.DataFrame, *, limit: int | None = None) -> str:
    d = df if limit is None else df.head(limit)
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in d.columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{_fmt(v)}</td>" for v in row) + "</tr>"
        for row in d.itertuples(index=False, name=None)
    )
    more = "" if limit is None or len(df) <= limit else (
        f'<p class="muted">{len(df) - limit:,} further row(s) in the workbook and CSV.</p>'
    )
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{more}'


def _chart(detail: pd.DataFrame) -> str:
    """Stacked bars: units by month, split by provenance. One axis, legend + table view."""
    piv = (
        detail.pivot_table(index="month_start", columns="authority", values="units", aggfunc="sum")
        .reindex(columns=[bc.AUTH_MEASURED, bc.AUTH_SHAPED, bc.AUTH_STATED])
        .fillna(0.0)
        .sort_index()
    )
    if piv.empty:
        return ""
    W, H, PAD_L, PAD_B, PAD_T = 980, 340, 62, 46, 14
    n = len(piv)
    top = float(piv.sum(axis=1).max()) or 1.0
    step = (W - PAD_L - 8) / n
    bw = min(30.0, step * 0.62)
    plot_h = H - PAD_B - PAD_T
    ticks = [0, top / 2, top]
    grid = "".join(
        f'<line class="grid" x1="{PAD_L}" x2="{W - 8}" y1="{PAD_T + plot_h - t / top * plot_h:.1f}" '
        f'y2="{PAD_T + plot_h - t / top * plot_h:.1f}"/>'
        f'<text class="tick" x="{PAD_L - 8}" y="{PAD_T + plot_h - t / top * plot_h + 4:.1f}" '
        f'text-anchor="end">{t / 1000:,.0f}k</text>'
        for t in ticks
    )
    bars, labels = [], []
    for i, (m, row) in enumerate(piv.iterrows()):
        x = PAD_L + i * step + (step - bw) / 2
        y = PAD_T + plot_h
        total = float(row.sum())
        for auth in piv.columns:
            v = float(row[auth])
            if v <= 0:
                continue
            h = v / top * plot_h
            y -= h
            # 2px surface gap between stacked segments; 4px rounded data-end on the top one
            bars.append(
                f'<rect class="seg a-{auth}" x="{x:.1f}" y="{y + 1:.1f}" width="{bw:.1f}" '
                f'height="{max(h - 2, 0.5):.1f}" rx="2">'
                f'<title>{m:%b-%Y} · {AUTH_LABEL[auth]}: {v:,.0f} units '
                f'({v / total:.0%} of {total:,.0f})</title></rect>'
            )
        if i % 2 == 0 or n <= 18:
            labels.append(
                f'<text class="tick" x="{x + bw / 2:.1f}" y="{H - PAD_B + 16}" '
                f'text-anchor="middle">{m:%b-%y}</text>'
            )
    legend = "".join(
        f'<span class="key"><i class="a-{a}"></i>{html.escape(AUTH_LABEL[a])}</span>'
        for a in piv.columns if float(piv[a].sum()) > 0
    )
    table = piv.round(0).reset_index()
    table["month_start"] = table["month_start"].dt.strftime("%b-%y")
    table.columns = ["Month"] + [AUTH_LABEL[c] for c in piv.columns]
    return f"""
<section>
  <h2>Forecast units by month, split by provenance</h2>
  <p class="muted">Every unit is either measured from BPD, measured and reshaped by the
  B&amp;M store plan, or stated by B&amp;M alone. The third kind is graded E without exception.</p>
  <div class="legend">{legend}</div>
  <svg viewBox="0 0 {W} {H}" role="img" aria-label="Forecast units by month, stacked by provenance">
    {grid}{''.join(bars)}{''.join(labels)}
    <line class="axis" x1="{PAD_L}" x2="{W - 8}" y1="{PAD_T + plot_h}" y2="{PAD_T + plot_h}"/>
  </svg>
  <details><summary>Table view</summary>{_table(table)}</details>
</section>"""


def _html(frames, readme, detail) -> str:
    lt = {k: v[0] for k, v in PALETTE.items()}
    dk = {k: v[1] for k, v in PALETTE.items()}
    css_vars = lambda d: "".join(f"--a-{k}:{v};" for k, v in d.items())
    hero = [
        ("Measured (BPD)", readme["units_measured"]),
        ("Shaped by the B&M plan", readme["units_measured_shaped_by_plan"]),
        ("B&M stated only (grade E)", readme["units_stated_only_graded_E"]),
    ]
    hero_html = "".join(
        f'<div class="tile"><div class="tile-n">{v:,}</div><div class="tile-l">{html.escape(l)}</div></div>'
        for l, v in hero
    )
    meta = "".join(
        f"<tr><th>{html.escape(str(k))}</th><td>{_fmt(v)}</td></tr>" for k, v in readme.items()
    )
    sections = "".join(
        f"<section><h2>{html.escape(name)}</h2>{_table(df, limit=400)}</section>"
        for name, df in frames.items()
        if name != "Monthly detail"
    )
    detail_sheet = frames["Monthly detail"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Target Demand Forecast — BPD × B&amp;M</title>
<style>
:root{{color-scheme:light dark;--bg:#fcfcfb;--card:#fff;--ink:#0b0b0b;--ink2:#52514e;
--muted:#78766f;--line:#e6e4dd;--head:#f4f3ef;{css_vars(lt)}}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141413;--card:#1a1a19;--ink:#fff;
--ink2:#c3c2b7;--muted:#8f8d84;--line:#2e2e2b;--head:#232322;{css_vars(dk)}}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:1180px;margin:0 auto;padding:28px 20px 72px}}
h1{{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}}
h2{{font-size:15px;text-transform:uppercase;letter-spacing:.06em;color:var(--ink2);
margin:36px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}}
.sub{{color:var(--muted);margin:0 0 22px}}
.muted{{color:var(--muted);font-size:13px}}
.tiles{{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0 4px}}
.tile{{flex:1 1 190px;background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:14px 16px}}
.tile-n{{font-size:25px;font-weight:600;letter-spacing:-.02em;font-variant-numeric:tabular-nums}}
.tile-l{{color:var(--muted);font-size:12px;margin-top:2px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:4px 18px 18px;margin-top:22px}}
section h2{{margin-top:16px}}
.scroll{{overflow-x:auto;max-height:520px;overflow-y:auto;border:1px solid var(--line);
border-radius:8px}}
table{{border-collapse:collapse;width:100%;font-size:12.5px}}
th,td{{padding:6px 10px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}}
thead th{{position:sticky;top:0;background:var(--head);z-index:1;font-weight:600;
color:var(--ink2)}}
td{{font-variant-numeric:tabular-nums}}
svg{{width:100%;height:auto;display:block;margin:6px 0 2px}}
.grid{{stroke:var(--line);stroke-width:1}}
.axis{{stroke:var(--line);stroke-width:1}}
.tick{{fill:var(--muted);font-size:11px}}
.seg{{stroke:var(--card);stroke-width:0}}
.seg:hover{{stroke:var(--ink);stroke-width:1.5}}
.a-{bc.AUTH_MEASURED}{{fill:var(--a-{bc.AUTH_MEASURED})}}
.a-{bc.AUTH_SHAPED}{{fill:var(--a-{bc.AUTH_SHAPED})}}
.a-{bc.AUTH_STATED}{{fill:var(--a-{bc.AUTH_STATED})}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;margin:8px 0 2px;font-size:12.5px;
color:var(--ink2)}}
.key{{display:flex;align-items:center;gap:6px}}
.key i{{width:11px;height:11px;border-radius:3px;display:inline-block}}
i.a-{bc.AUTH_MEASURED}{{background:var(--a-{bc.AUTH_MEASURED})}}
i.a-{bc.AUTH_SHAPED}{{background:var(--a-{bc.AUTH_SHAPED})}}
i.a-{bc.AUTH_STATED}{{background:var(--a-{bc.AUTH_STATED})}}
details{{margin-top:10px}}
summary{{cursor:pointer;color:var(--ink2);font-size:13px}}
.bar{{display:flex;gap:8px;align-items:center;margin:10px 0 2px;flex-wrap:wrap}}
input,select{{background:var(--card);color:var(--ink);border:1px solid var(--line);
border-radius:7px;padding:6px 9px;font:inherit;font-size:13px}}
</style></head><body>
<div class="wrap">
<h1>Target SKU-level demand forecast</h1>
<p class="sub">BPD-measured level ({html.escape(str(readme['bpd_estimator']))}) reshaped by the
Brick &amp; Mortar master forecast's stated store plan · as of {html.escape(str(readme['as_of']))}</p>
<div class="tiles">{hero_html}</div>
{_chart(detail)}
<section><h2>Monthly detail</h2>
<div class="bar">
  <input id="q" type="search" placeholder="Filter by TCIN, SKU, stream, flag…" aria-label="Filter rows">
  <select id="auth" aria-label="Filter by provenance"><option value="">All provenance</option>
    {''.join(f'<option value="{a}">{html.escape(AUTH_LABEL[a])}</option>' for a in PALETTE)}
  </select>
  <select id="grade" aria-label="Filter by grade"><option value="">All grades</option>
    {''.join(f'<option value="{g}">Grade {g}</option>' for g in bc.GRADE_ORDER)}
  </select>
  <span class="muted" id="count"></span>
</div>
{_table(detail_sheet, limit=4000)}
</section>
{sections}
<section><h2>Run metadata</h2><div class="scroll"><table><tbody>{meta}</tbody></table></div></section>
</div>
<script>
(function(){{
  var sec=document.querySelectorAll('section');
  var host=null; sec.forEach(function(s){{ if(s.querySelector('h2') &&
    s.querySelector('h2').textContent.trim()==='Monthly detail') host=s; }});
  if(!host) return;
  var q=host.querySelector('#q'), a=host.querySelector('#auth'),
      g=host.querySelector('#grade'), c=host.querySelector('#count'),
      rows=Array.prototype.slice.call(host.querySelectorAll('tbody tr'));
  var cells=rows.map(function(r){{ return r.textContent.toLowerCase(); }});
  function apply(){{
    var t=q.value.trim().toLowerCase(), av=a.value.toLowerCase(), gv=g.value.toLowerCase(), n=0;
    for(var i=0;i<rows.length;i++){{
      var s=cells[i];
      var ok=(!t||s.indexOf(t)>-1)&&(!av||s.indexOf(av)>-1)&&
             (!gv||rows[i].children[9].textContent.trim().toLowerCase()===gv);
      rows[i].hidden=!ok; if(ok) n++;
    }}
    c.textContent=n.toLocaleString()+' of '+rows.length.toLocaleString()+' rows';
  }}
  q.addEventListener('input',apply); a.addEventListener('change',apply);
  g.addEventListener('change',apply); apply();
}})();
</script>
</body></html>"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bm", required=True, help="path to the Brick & Mortar Master Forecast .xlsx")
    ap.add_argument("--run", required=True, help="a published Shipcast run directory (holds csv/monthly.csv)")
    ap.add_argument("--out", default="runs/bm_combined", help="output directory")
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD; defaults to the run directory name")
    a = ap.parse_args(argv)
    written = build(
        Path(a.bm).expanduser(),
        Path(a.run),
        Path(a.out),
        as_of=date.fromisoformat(a.as_of) if a.as_of else None,
    )
    for k, v in written.items():
        print(f"{k:24s} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
