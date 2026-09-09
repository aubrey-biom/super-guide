"""Shared fixtures and the credential gate.

Two tiers, mirroring bullseye:
  (default)  pure python: parsers, aliases, scoring, SQL text, calendar. No network.
  -m bq_live real BigQuery on production tables (bills bytes). Skipped automatically
             when no credential is present, so a contributor without warehouse access
             still gets a meaningful green run.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

FIXTURES = ROOT / "tests" / "fixtures"


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    """Skip `bq_live` tests when no BigQuery credential is in the environment."""
    from shipcast.bq import credentials_available

    if credentials_available():
        return
    skip = pytest.mark.skip(
        reason="no BigQuery credential (GCP_SA_KEY_B64 / GOOGLE_APPLICATION_CREDENTIALS)"
    )
    for item in items:
        if "bq_live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """tests/fixtures."""
    return FIXTURES


@pytest.fixture(scope="session")
def rdz_text(fixtures_dir: Path) -> str:
    """The trimmed RDZ Inventory Summary export."""
    return (fixtures_dir / "rdz_inventory_summary.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def config() -> dict[str, Any]:
    """config/target.yaml as a mapping."""
    from shipcast.config import load_config

    return load_config()
