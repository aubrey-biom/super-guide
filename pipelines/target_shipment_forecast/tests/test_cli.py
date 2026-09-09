from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from pipelines.target_shipment_forecast.cli import app

runner = CliRunner()


def test_check_without_bq_has_nothing_left_to_probe(fixtures_dir: Path) -> None:
    """`check --skip-bq` must exit clean and mention no local input.

    Every non-BigQuery probe is gone: the RDZ sheet parse was the last one, removed with
    the supply modules on 2026-09-08. This test is what fails if a file-based input is
    ever reintroduced to `check` without a deliberate decision.
    """
    res = runner.invoke(app, ["check", "--skip-bq"])
    assert res.exit_code == 0, res.output
    for gone in ("rdz", "RDZ", "drive", "Drive", "owner"):
        assert gone not in res.output, f"{gone!r} is back in `check` output"


def test_run_requires_a_pull_manifest(fixtures_dir: Path, tmp_path: Path) -> None:
    res = runner.invoke(
        app,
        ["run", "--channel", "target", "--as-of", "2026-09-03", "--inputs-dir", str(tmp_path)],
    )
    assert res.exit_code == 2, res.output
    assert "shipcast pull" in res.output


def test_backtest_and_score_on_fixture(fixtures_dir: Path, tmp_path: Path) -> None:
    res = runner.invoke(
        app, ["backtest", "--panel", str(fixtures_dir / "signal_panel.csv"), "--out", str(tmp_path)]
    )
    assert res.exit_code == 0, res.output
    assert (tmp_path / "backtest_rows.csv").exists()
    res = runner.invoke(
        app,
        [
            "score",
            "--rows",
            str(tmp_path / "backtest_rows.csv"),
            "--out",
            str(tmp_path / "scores.csv"),
            "--n-boot",
            "20",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "plan_sat_ordered" in res.output
