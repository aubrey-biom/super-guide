"""`shipcast run` orchestration: load inputs, assemble the forecast, write the workbook.

Kept out of `cli.py` so it can be called from tests and from a Claude Code
session without Typer. Inputs come from `runs/<as_of>/*.parquet` written by
`shipcast pull`, and nothing else - every input is a BigQuery read. The owner
spreadsheet and the RDZ supply sheet were both removed on 2026-09-08, so there is no
Drive call and no manually placed file in this path.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from pipelines.target_shipment_forecast.channels.target.calendar import TargetCalendar
from pipelines.target_shipment_forecast.config import load_config, repo_root
from pipelines.target_shipment_forecast.inputs.item_master import ItemMaster
from pipelines.target_shipment_forecast.output.report import load_spec, render_workbook, write_csvs
from pipelines.target_shipment_forecast.pipeline import ForecastBundle, run_forecast

PULLED_TABLES: tuple[str, ...] = (
    "plan_snapshots",
    "orders_latest",
    "po_actuals_weekly",
    "sales_weekly",
    "inventory_weekly",
    "dfe_asof",
    "item_state",
    "launch_seed",
)


def execute_pull(
    *,
    as_of: date,
    run_dir: Path,
    weeks_back: int = 110,
    snapshots: int = 2,
) -> dict[str, Any]:
    """Pull every signal for `as_of` into `run_dir/*.parquet` and return the manifest.

    Lives here rather than in `cli.py` so the Cloud Run entrypoint
    (`runners/run_target_shipment_forecast.py`) can pull AND assemble in ONE process.
    Before 2026-09-08 the pull existed only as a Typer command body, which made the
    runner un-deployable: it required a manifest a previous invocation had written, and a
    Cloud Run job starts with an empty filesystem every time. `cli.pull` is now a thin
    wrapper over this function, so the two paths cannot drift.

    `weeks_back` defaults to 110 (~2.1 years) because `consumption.seasonal_index` needs
    >= 2 calendar years per month before it will admit a factor; a shorter window keeps
    the gate shut no matter how deep `bpd_raw` gets.
    """
    from pipelines.target_shipment_forecast import bq
    from pipelines.target_shipment_forecast.channels.target import signals
    from pipelines.target_shipment_forecast.config import threshold

    cfg = load_config()
    cap = int(cfg["bq"]["max_bytes_billed"])
    client = bq.client()

    def q(sql: str, **kw: Any) -> pd.DataFrame:
        return bq.query(sql, bq_client=client, max_bytes_billed=cap, **kw)

    def write(df: pd.DataFrame, name: str) -> dict[str, Any]:
        path = run_dir / f"{name}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, index=False)
        return {
            "rows": len(df),
            "bytes_billed": int(df.attrs.get("bytes_billed", 0)),
            "path": str(path),
        }

    manifest: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "pulled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tables": {},
    }
    dates = signals.plan_business_dates(as_of, limit=snapshots, run=q)
    if not dates:
        raise RuntimeError(f"no plan BUSINESS_D on or before {as_of}")
    manifest["plan_business_dates"] = [d.isoformat() for d in dates]
    t = manifest["tables"]
    t["plan_snapshots"] = write(signals.plan_snapshots(dates, run=q), "plan_snapshots")
    orders = signals.orders_latest(run=q)
    t["orders_latest"] = write(orders, "orders_latest")
    t["po_actuals_weekly"] = write(
        signals.po_actuals_weekly(
            orders,
            as_of=as_of,
            forward_threshold_days=threshold(cfg, "streams", "forward_threshold_days"),
            lapsed_grace_days=threshold(cfg, "streams", "lapsed_grace_days"),
        ),
        "po_actuals_weekly",
    )
    start = as_of - timedelta(days=7 * int(weeks_back))
    t["sales_weekly"] = write(signals.sales_weekly(start, as_of, run=q), "sales_weekly")
    t["inventory_weekly"] = write(
        signals.inventory_weekly(
            start, as_of, dc_ids=signals.dc_ids_from_orders(orders), run=q
        ),
        "inventory_weekly",
    )
    t["dfe_asof"] = write(signals.dfe_asof(as_of, run=q), "dfe_asof")
    # item_state: live feed, not the committed CSV (biom_sql fix (d)).
    t["item_state"] = write(signals.item_state_live(as_of, run=q), "item_state")
    # curated launch assumptions: biom_admin.seed_target_launch_velocity, as-of filtered.
    # Empty (and harmless) until someone loads a row; an absent table degrades to empty.
    t["launch_seed"] = write(signals.launch_seed(as_of, run=q), "launch_seed")
    manifest["bytes_billed_total"] = sum(v["bytes_billed"] for v in t.values())
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


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


__all__ = ["PULLED_TABLES", "default_panel", "execute_pull", "execute_run", "load_pulled"]
