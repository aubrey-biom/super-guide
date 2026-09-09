"""`shipcast` command line.

    shipcast check                               reachability + integrity probes
    shipcast pull    --as-of DATE [--out runs/]   pull Target signals to parquet
    shipcast run     --channel target --as-of DATE
    shipcast backtest [--panel FILE] [--out DIR]  melt the signal panel to long rows + leakage check
    shipcast score   [--rows FILE] [--out FILE]   metrics per signal x horizon with bootstrap CIs

`run` is wired end to end but the model stages are v1 stubs: it pulls inputs,
then reports exactly which stages raised
NotImplementedError and exits 2.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import pandas as pd
import typer

from pipelines.target_shipment_forecast import __version__, bq
from pipelines.target_shipment_forecast.config import load_config, repo_root

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)

FIXTURE_PANEL = repo_root() / "tests" / "fixtures" / "signal_panel.csv"


def _parse_as_of(value: str | None) -> date:
    return date.today() if not value else date.fromisoformat(value)


def _echo(msg: str) -> None:
    typer.echo(msg)


def _pass(name: str, detail: str) -> None:
    _echo(f"PASS  {name}: {detail}")


def _fail(name: str, detail: str) -> None:
    _echo(f"FAIL  {name}: {detail}")


@app.callback()
def _main() -> None:
    """shipcast — Target PO / shipment forecaster."""


@app.command()
def version() -> None:
    """Print the package version."""
    _echo(__version__)


# --------------------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------------------


@app.command()
def check(
    skip_bq: Annotated[bool, typer.Option(help="Skip the BigQuery probes")] = False,
) -> None:
    """BigQuery reachability as SESSION_USER, order de-dup ~7.8k lines, plan filtered to one BUSINESS_D."""
    from pipelines.target_shipment_forecast.channels.target import signals

    failures = 0
    if skip_bq:
        _echo("SKIP  bigquery probes (--skip-bq)")
    elif not bq.credentials_available():
        failures += 1
        _fail("bigquery", "no credential: set GOOGLE_APPLICATION_CREDENTIALS or GCP_SA_KEY_B64")
    else:
        try:
            c = bq.client()
            user = bq.session_user(c)
            _pass("bigquery.session_user", user)
        except Exception as e:  # any failure here is the finding
            failures += 1
            _fail("bigquery.session_user", f"{type(e).__name__}: {e}")
            c = None
        if c is not None:
            try:
                n = signals.orders_line_count(
                    run=lambda sql, **kw: bq.query(sql, bq_client=c, **kw)
                )
                lines = int(n["n_lines"].iloc[0])
                detail = f"{lines:,} latest-state lines (raw feed ~150k), max snapshot {n['max_snapshot_d'].iloc[0]}"
                if 5_000 <= lines <= 20_000:
                    _pass("orders.dedup", detail)
                else:
                    failures += 1
                    _fail(
                        "orders.dedup",
                        detail + " — outside the 5k-20k band; is the QUALIFY still applied?",
                    )
            except Exception as e:
                failures += 1
                _fail("orders.dedup", f"{type(e).__name__}: {e}")
            try:
                run = lambda sql, **kw: bq.query(sql, bq_client=c, **kw)  # noqa: E731
                dates = signals.plan_business_dates(date.today(), limit=1, run=run)
                if not dates:
                    raise RuntimeError("no plan BUSINESS_D on or before today")
                plan = signals.plan_snapshot(dates[0], run=run)
                nb = plan["business_d"].nunique()
                detail = f"BUSINESS_D {dates[0]}: {len(plan):,} rows, {nb} distinct BUSINESS_D, {plan['tcin'].nunique()} TCINs"
                if nb == 1:
                    _pass("plan.one_business_d", detail)
                else:
                    failures += 1
                    _fail("plan.one_business_d", detail)
            except Exception as e:
                failures += 1
                _fail("plan.one_business_d", f"{type(e).__name__}: {e}")

    if failures:
        _echo(f"{failures} check(s) failed")
        raise typer.Exit(code=1)
    _echo("all checks passed")


# --------------------------------------------------------------------------------------
# pull
# --------------------------------------------------------------------------------------


def _write_parquet(df: pd.DataFrame, path: Path) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return {
        "rows": len(df),
        "bytes_billed": int(df.attrs.get("bytes_billed", 0)),
        "path": str(path),
    }


@app.command()
def pull(
    as_of: Annotated[
        str | None, typer.Option("--as-of", help="Origin date YYYY-MM-DD (default today)")
    ] = None,
    out: Annotated[Path | None, typer.Option(help="Output root (default runs/)")] = None,
    weeks_back: Annotated[
        int,
        typer.Option(
            help="History window for sales/inventory (default 110 ~ 2.1 years: the "
            "seasonal index needs >= 2 calendar years per month or it stays at 1.000)"
        ),
    ] = 110,
    snapshots: Annotated[
        int, typer.Option(help="Plan BUSINESS_D snapshots to pull (newest first)")
    ] = 2,
) -> None:
    """Pull Target signals as of a date into runs/<as_of>/*.parquet with a manifest."""
    from pipelines.target_shipment_forecast.run import execute_pull

    origin = _parse_as_of(as_of)
    root = (out or repo_root() / "runs") / origin.isoformat()
    # The body lives in run.execute_pull so the Cloud Run entrypoint can pull and
    # assemble in one process. This command is deliberately a wrapper, not a copy.
    manifest = execute_pull(
        as_of=origin, run_dir=root, weeks_back=weeks_back, snapshots=snapshots
    )
    _echo(json.dumps(manifest, indent=2))


# --------------------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------------------


@app.command()
def run(
    channel: Annotated[str, typer.Option(help="Channel (v1: target)")] = "target",
    as_of: Annotated[
        str | None, typer.Option("--as-of", help="Origin date YYYY-MM-DD (default today)")
    ] = None,
    inputs_dir: Annotated[
        Path | None, typer.Option(help="Directory of pulled parquet (default runs/<as_of>)")
    ] = None,
    out: Annotated[
        Path | None, typer.Option(help="Workbook path (default runs/<as_of>/<report filename>)")
    ] = None,
    horizon_weeks: Annotated[
        int | None, typer.Option(help="PO weeks to forecast (default config horizon.po_weeks)")
    ] = None,
    months: Annotated[
        int | None, typer.Option(help="Months to forecast (default config horizon.months)")
    ] = None,
    spec: Annotated[
        Path | None, typer.Option(help="Report layout YAML (default config/report_target.yaml)")
    ] = None,
    bm: Annotated[
        Path | None,
        typer.Option(
            "--bm",
            help=(
                "Local copy of the B&M Master Forecast .xlsx: hand-run override of the "
                "BigQuery snapshot pulled as bm_schedule"
            ),
        ),
    ] = None,
) -> None:
    """Produce the ONE forecast workbook from the pulled BigQuery signals.

    No Drive fetch and no manually placed file in the scheduled path: the monthly POS
    forecast is `consumption.dist_velocity`, `planned_launch` comes from Target's own PO
    plan plus the live item-state feed, curated launch assumptions come from
    `biom_admin.seed_target_launch_velocity` (`launch_seed`), and the channel owner's
    Brick & Mortar store plan comes from `biom_admin.bm_target_schedule_snapshot`
    (`bm_schedule`, landed by ingest/bm_schedule_ingest.py). The store plan shapes the
    measured forecast's forward store count; it never sets its level. `--bm PATH` reads a
    local copy of that sheet instead, for a hand-run.
    """
    if channel != "target":
        raise typer.BadParameter("v1 supports --channel target only")
    from pipelines.target_shipment_forecast.run import execute_run

    origin = _parse_as_of(as_of)
    cfg = load_config(channel=channel)
    run_dir = inputs_dir or repo_root() / "runs" / origin.isoformat()
    if not (run_dir / "manifest.json").exists():
        _echo(f"no manifest in {run_dir}; run `shipcast pull --as-of {origin}` first")
        raise typer.Exit(code=2)
    hw = horizon_weeks or int(cfg.get("horizon", {}).get("po_weeks", 16))
    mo = months or int(cfg.get("horizon", {}).get("months", 16))

    bundle, path = execute_run(
        as_of=origin,
        run_dir=run_dir,
        out_path=out,
        horizon_weeks=hw,
        months=mo,
        channel=channel,
        spec_path=spec,
        bm_path=bm,
    )
    for w in bundle.warnings:
        _echo(f"warning: {w}")
    bm_meta = bundle.readme.get("bm_schedule") or {}
    _echo(
        f"bm schedule: {bm_meta.get('source')} {bm_meta.get('name') or ''} "
        f"snapshot {bm_meta.get('snapshot_date') or '-'}; "
        f"{(bm_meta.get('shaping') or {}).get('rows_shaped', 0)} POS rows shaped"
    )
    wk = bundle.frames["weekly"]
    mo_df = bundle.frames["monthly"]
    _echo(
        f"weekly rows {len(wk):,} ({wk['tcin'].nunique()} TCINs x {wk['po_week'].nunique()} weeks); monthly rows {len(mo_df):,}"
    )
    _echo(f"workbook -> {path}")


# --------------------------------------------------------------------------------------
# backtest / score
# --------------------------------------------------------------------------------------


@app.command()
def backtest(
    panel: Annotated[
        Path | None, typer.Option(help="Signal panel CSV (default tests/fixtures/signal_panel.csv)")
    ] = None,
    out: Annotated[
        Path | None, typer.Option(help="Output directory (default runs/backtest)")
    ] = None,
) -> None:
    """Melt the as-of signal panel into long backtest rows and assert no leakage."""
    from pipelines.target_shipment_forecast.backtest.leakage import LeakageError, assert_no_leakage
    from pipelines.target_shipment_forecast.backtest.rolling import panel_to_long

    src = panel or FIXTURE_PANEL
    df = pd.read_csv(src)
    rows = panel_to_long(df)
    try:
        assert_no_leakage(rows)
    except LeakageError as e:
        _fail("leakage", str(e))
        raise typer.Exit(code=1) from e
    dest = (out or repo_root() / "runs" / "backtest") / "backtest_rows.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    rows.to_csv(dest, index=False)
    _echo(
        f"{len(rows):,} backtest rows from {src.name} ({rows['origin'].nunique()} origins, signals {sorted(rows['signal'].unique())}) -> {dest}"
    )


@app.command()
def score(
    rows: Annotated[
        Path | None,
        typer.Option(help="Long backtest rows CSV (default runs/backtest/backtest_rows.csv)"),
    ] = None,
    out: Annotated[
        Path | None, typer.Option(help="Output CSV (default runs/backtest/scores.csv)")
    ] = None,
    n_boot: Annotated[
        int | None,
        typer.Option(help="Week-block bootstrap resamples (default config gate.bootstrap.n_boot)"),
    ] = None,
) -> None:
    """WAPE, bias, median APE, exact and within-10% shares per signal x horizon, with 80% bootstrap CIs on WAPE."""
    from pipelines.target_shipment_forecast.backtest.rolling import panel_to_long
    from pipelines.target_shipment_forecast.backtest.scoring import (
        score_table,
        wape,
        week_block_bootstrap_ci,
    )

    src = rows or repo_root() / "runs" / "backtest" / "backtest_rows.csv"
    long = (
        pd.read_csv(src, parse_dates=["origin", "week", "source_ts"])
        if src.exists()
        else panel_to_long(pd.read_csv(FIXTURE_PANEL))
    )
    table = score_table(
        long, actual_col="actual", forecast_col="forecast", by=["signal", "horizon"]
    )
    # ONE source of truth for the draw count (biom_sql fix (c)): this command used to
    # default to 500 while config/target.yaml said 1,000 and the Accuracy sheet used the
    # config value — so `score` and the workbook reported intervals from different
    # bootstraps and no document could be right about both.
    draws = int(n_boot if n_boot is not None else load_config()["gate"]["bootstrap"]["n_boot"])
    cis = []
    for (sig, h), g in long.groupby(["signal", "horizon"]):
        _point, lo, hi = week_block_bootstrap_ci(
            g,
            week_col="week",
            actual_col="actual",
            forecast_col="forecast",
            metric=wape,
            n_boot=draws,
        )
        cis.append({"signal": sig, "horizon": h, "wape_lo80": lo, "wape_hi80": hi})
    table = table.merge(pd.DataFrame(cis), on=["signal", "horizon"], how="left").sort_values(
        ["horizon", "wape"]
    )
    table["bootstrap_draws"] = draws
    dest = out or repo_root() / "runs" / "backtest" / "scores.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(dest, index=False)
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        _echo(table.round(3).to_string(index=False))
    _echo(f"-> {dest}")


def main() -> None:  # pragma: no cover - console entry
    """Console entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
