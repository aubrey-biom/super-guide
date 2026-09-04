"""`shipcast run` orchestration: load inputs, assemble the forecast, write the workbook.

Kept out of `cli.py` so it can be called from tests and from a Claude Code
session without Typer. Inputs come from `runs/<as_of>/*.parquet` written by
`shipcast pull`, plus optional parsed owner and RDZ frames.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from shipcast.channels.target.calendar import TargetCalendar
from shipcast.config import load_config, repo_root
from shipcast.inputs.item_master import ItemMaster
from shipcast.output.report import load_spec, render_workbook, write_csvs
from shipcast.pipeline import ForecastBundle, run_forecast

PULLED_TABLES: tuple[str, ...] = (
    "plan_snapshots",
    "orders_latest",
    "po_actuals_weekly",
    "sales_weekly",
    "inventory_weekly",
    "dfe_asof",
)


def load_pulled(run_dir: Path) -> dict[str, pd.DataFrame | None]:
    """Read every pulled parquet in `run_dir` (None where absent)."""
    out: dict[str, pd.DataFrame | None] = {}
    for name in PULLED_TABLES:
        p = run_dir / f"{name}.parquet"
        out[name] = pd.read_parquet(p) if p.exists() else None
    return out


def default_panel() -> pd.DataFrame | None:
    """The committed backtest panel, if present."""
    p = repo_root() / "tests" / "fixtures" / "signal_panel.csv"
    return pd.read_csv(p) if p.exists() else None


def execute_run(
    *,
    as_of: date,
    run_dir: Path,
    out_path: Path | None = None,
    owner_rows: pd.DataFrame | None = None,
    owner_meta: dict[str, Any] | None = None,
    on_hand: pd.DataFrame | None = None,
    inbound: pd.DataFrame | None = None,
    horizon_weeks: int = 16,
    months: int = 16,
    channel: str = "target",
    spec_path: Path | None = None,
    panel: pd.DataFrame | None = None,
) -> tuple[ForecastBundle, Path]:
    """Assemble and write the workbook; returns the bundle and the workbook path."""
    cfg = load_config(channel=channel)
    calendar = TargetCalendar.from_config(cfg)
    item_master = ItemMaster.load()
    inputs: dict[str, Any] = dict(load_pulled(run_dir))
    inputs["owner"] = owner_rows
    inputs["owner_meta"] = owner_meta or {}
    inputs["on_hand"] = on_hand
    inputs["inbound"] = inbound
    bundle = run_forecast(
        inputs,
        cfg=cfg,
        calendar=calendar,
        item_master=item_master,
        as_of=as_of,
        horizon_weeks=horizon_weeks,
        months=months,
        panel=panel if panel is not None else default_panel(),
    )
    spec = load_spec(spec_path)
    fname = str(
        spec.get("workbook", {}).get("filename", "target_shipment_forecast_{as_of}.xlsx")
    ).format(as_of=as_of.isoformat())
    out = out_path or run_dir / fname
    render_workbook(bundle.frames, bundle.readme, out_path=out, spec=spec)
    if spec.get("workbook", {}).get("also_write_csv", True):
        write_csvs(bundle.frames, out.parent / "csv")
    (out.parent / "readme.json").write_text(json.dumps(bundle.readme, indent=2, default=str))
    log = out.parent / "forecast_log.csv"
    wk = bundle.frames["weekly"][
        ["po_week", "tcin", "expected_po_units", "grade", "fallback_rung"]
    ].copy()
    wk.insert(0, "as_of", as_of.isoformat())
    wk.to_csv(log, mode="a", header=not log.exists(), index=False)
    return bundle, out


__all__ = ["PULLED_TABLES", "default_panel", "execute_run", "load_pulled"]
