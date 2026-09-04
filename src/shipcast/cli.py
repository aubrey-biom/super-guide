"""`shipcast` command line.

    shipcast check   [--rdz FILE]                 reachability + integrity probes
    shipcast pull    --as-of DATE [--out runs/]   pull Target signals to parquet
    shipcast run     --channel target --as-of DATE [--rdz FILE] [--owner FILE]
    shipcast backtest [--panel FILE] [--out DIR]  melt the signal panel to long rows + leakage check
    shipcast score   [--rows FILE] [--out FILE]   metrics per signal x horizon with bootstrap CIs

`run` is wired end to end but the model stages are v1 stubs: it pulls inputs,
parses the supply sheet, then reports exactly which stages raised
NotImplementedError and exits 2.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

import pandas as pd
import typer

from shipcast import __version__, bq
from shipcast.config import load_config, repo_root, threshold

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
    rdz: Annotated[Path | None, typer.Option(help="RDZ sheet export (plain text) to parse")] = None,
    skip_bq: Annotated[bool, typer.Option(help="Skip the BigQuery probes")] = False,
) -> None:
    """BigQuery reachability as SESSION_USER, order de-dup ~7.8k lines, plan filtered to one BUSINESS_D, RDZ parses."""
    from shipcast.channels.target import signals

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

    rdz_path = rdz or (repo_root() / "inputs" / "RDZ.txt")
    if rdz_path.exists():
        from shipcast.supply.rdz_sheet import RdzParseError, RdzSheetAdapter

        try:
            adapter = RdzSheetAdapter(rdz_path)
            snap = adapter.snapshot(date.today())
            inb = adapter.inbound(date.today())
            _pass(
                "rdz.parse",
                f"{snap.n_items} items, banner {snap.banner!r} -> {snap.as_of}, identity {snap.checks['identity_share']:.1%}, "
                f"inbound rows {len(inb)} ({inb['confidence'].value_counts().to_dict()})",
            )
        except RdzParseError as e:
            failures += 1
            _fail("rdz.parse", str(e))
    else:
        _echo(f"SKIP  rdz.parse: no file at {rdz_path} (pass --rdz)")

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
    weeks_back: Annotated[int, typer.Option(help="History window for sales/inventory")] = 30,
    snapshots: Annotated[
        int, typer.Option(help="Plan BUSINESS_D snapshots to pull (newest first)")
    ] = 2,
) -> None:
    """Pull Target signals as of a date into runs/<as_of>/*.parquet with a manifest."""
    from shipcast.channels.target import signals

    origin = _parse_as_of(as_of)
    root = (out or repo_root() / "runs") / origin.isoformat()
    cfg = load_config()
    cap = int(cfg["bq"]["max_bytes_billed"])
    c = bq.client()

    def run(sql: str, **kw: Any) -> pd.DataFrame:
        return bq.query(sql, bq_client=c, max_bytes_billed=cap, **kw)

    manifest: dict[str, Any] = {
        "as_of": origin.isoformat(),
        "pulled_at": datetime.now().isoformat(timespec="seconds"),
        "tables": {},
    }
    dates = signals.plan_business_dates(origin, limit=snapshots, run=run)
    if not dates:
        raise typer.BadParameter(f"no plan BUSINESS_D on or before {origin}")
    manifest["plan_business_dates"] = [d.isoformat() for d in dates]
    manifest["tables"]["plan_snapshots"] = _write_parquet(
        signals.plan_snapshots(dates, run=run), root / "plan_snapshots.parquet"
    )
    orders = signals.orders_latest(run=run)
    manifest["tables"]["orders_latest"] = _write_parquet(orders, root / "orders_latest.parquet")
    fwd = threshold(cfg, "streams", "forward_threshold_days")
    grace = threshold(cfg, "streams", "lapsed_grace_days")
    manifest["tables"]["po_actuals_weekly"] = _write_parquet(
        signals.po_actuals_weekly(
            orders, as_of=origin, forward_threshold_days=fwd, lapsed_grace_days=grace
        ),
        root / "po_actuals_weekly.parquet",
    )
    start = origin - pd.Timedelta(days=7 * weeks_back).to_pytimedelta()
    manifest["tables"]["sales_weekly"] = _write_parquet(
        signals.sales_weekly(start, origin, run=run), root / "sales_weekly.parquet"
    )
    manifest["tables"]["inventory_weekly"] = _write_parquet(
        signals.inventory_weekly(start, origin, dc_ids=signals.dc_ids_from_orders(orders), run=run),
        root / "inventory_weekly.parquet",
    )
    manifest["tables"]["dfe_asof"] = _write_parquet(
        signals.dfe_asof(origin, run=run), root / "dfe_asof.parquet"
    )
    manifest["bytes_billed_total"] = sum(t["bytes_billed"] for t in manifest["tables"].values())
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
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
    rdz: Annotated[
        Path | None,
        typer.Option(help="RDZ sheet export (plain text) to parse (default inputs/RDZ.txt)"),
    ] = None,
    owner: Annotated[
        Path | None,
        typer.Option(
            help="Owner forecast file (header-detected; default inputs/bm_master_forecast.txt)"
        ),
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
) -> None:
    """Produce the forecast workbook from pulled signals, the owner forecast and the RDZ sheet."""
    if channel != "target":
        raise typer.BadParameter("v1 supports --channel target only")
    from shipcast.inputs.aliases import AliasMap
    from shipcast.inputs.item_master import ItemMaster
    from shipcast.run import execute_run

    origin = _parse_as_of(as_of)
    cfg = load_config(channel=channel)
    run_dir = inputs_dir or repo_root() / "runs" / origin.isoformat()
    if not (run_dir / "manifest.json").exists():
        _echo(f"no manifest in {run_dir}; run `shipcast pull --as-of {origin}` first")
        raise typer.Exit(code=2)
    hw = horizon_weeks or int(cfg.get("horizon", {}).get("po_weeks", 16))
    mo = months or int(cfg.get("horizon", {}).get("months", 16))

    owner_rows = None
    owner_meta: dict[str, Any] = {}
    owner_path = owner or (repo_root() / "inputs" / "bm_master_forecast.txt")
    if owner_path.exists():
        from shipcast.inputs.owner_forecast import load_owner_forecast

        parsed = load_owner_forecast(
            owner_path, aliases=AliasMap.load(), item_master=ItemMaster.load(), as_of=origin
        )
        owner_rows = parsed.rows
        owner_meta = {
            "source": f"{owner_path.name} ({parsed.format})",
            "unresolved": parsed.unresolved.to_dict("records"),
            "warnings": list(parsed.warnings),
        }
        _echo(
            f"owner: {parsed.format} {len(parsed.rows)} rows, {len(parsed.unresolved)} unresolved, {len(parsed.warnings)} warnings"
        )
    else:
        _echo(f"owner: no file at {owner_path}; monthly view uses run-rate only")

    on_hand = inbound = None
    rdz_path = rdz or (repo_root() / "inputs" / "RDZ.txt")
    if rdz_path.exists():
        from shipcast.supply.rdz_sheet import RdzSheetAdapter

        adapter = RdzSheetAdapter(rdz_path)
        on_hand = adapter.on_hand(origin)
        inbound = adapter.inbound(origin)
        _echo(
            f"supply: RDZ {len(on_hand)} items, {len(inbound)} inbound rows (supply layer is v1.5; used for exceptions)"
        )

    bundle, path = execute_run(
        as_of=origin,
        run_dir=run_dir,
        out_path=out,
        owner_rows=owner_rows,
        owner_meta=owner_meta,
        on_hand=on_hand,
        inbound=inbound,
        horizon_weeks=hw,
        months=mo,
        channel=channel,
        spec_path=spec,
    )
    for w in bundle.warnings:
        _echo(f"warning: {w}")
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
    from shipcast.backtest.leakage import LeakageError, assert_no_leakage
    from shipcast.backtest.rolling import panel_to_long

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
    n_boot: Annotated[int, typer.Option(help="Week-block bootstrap resamples")] = 500,
) -> None:
    """WAPE, bias, median APE, exact and within-10% shares per signal x horizon, with 80% bootstrap CIs on WAPE."""
    from shipcast.backtest.rolling import panel_to_long
    from shipcast.backtest.scoring import score_table, wape, week_block_bootstrap_ci

    src = rows or repo_root() / "runs" / "backtest" / "backtest_rows.csv"
    long = (
        pd.read_csv(src, parse_dates=["origin", "week", "source_ts"])
        if src.exists()
        else panel_to_long(pd.read_csv(FIXTURE_PANEL))
    )
    table = score_table(
        long, actual_col="actual", forecast_col="forecast", by=["signal", "horizon"]
    )
    cis = []
    for (sig, h), g in long.groupby(["signal", "horizon"]):
        _point, lo, hi = week_block_bootstrap_ci(
            g,
            week_col="week",
            actual_col="actual",
            forecast_col="forecast",
            metric=wape,
            n_boot=n_boot,
        )
        cis.append({"signal": sig, "horizon": h, "wape_lo80": lo, "wape_hi80": hi})
    table = table.merge(pd.DataFrame(cis), on=["signal", "horizon"], how="left").sort_values(
        ["horizon", "wape"]
    )
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
