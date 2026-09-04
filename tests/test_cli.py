from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from shipcast.cli import app

runner = CliRunner()


def test_check_without_bq_parses_rdz(fixtures_dir: Path) -> None:
    res = runner.invoke(
        app, ["check", "--skip-bq", "--rdz", str(fixtures_dir / "rdz_inventory_summary.txt")]
    )
    assert res.exit_code == 0, res.output
    assert "PASS  rdz.parse: 127 items" in res.output


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
