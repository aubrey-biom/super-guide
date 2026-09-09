"""Shared fixtures and the credential gate.

Two tiers:
  (default)  pure python: aliases, item master, scoring, SQL text, calendar. No network.
  -m bq_live real BigQuery on production tables (bills bytes). Skipped automatically
             when no credential is present, so a contributor without warehouse access
             still gets a meaningful green run.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent          # pipelines/target_shipment_forecast
sys.path.insert(0, str(ROOT.parents[1]))               # biom_sql root, for `pipelines.` imports

FIXTURES = ROOT / "tests" / "fixtures"


def pytest_configure(config: Any) -> None:
    """Register the `bq_live` marker so `-m "not bq_live"` runs without a warning."""
    config.addinivalue_line("markers", "bq_live: hits production BigQuery (bills bytes); needs a credential")


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    """Skip `bq_live` tests when no BigQuery credential is in the environment."""
    from pipelines.target_shipment_forecast.bq import credentials_available

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
def config() -> dict[str, Any]:
    """config/target.yaml as a mapping."""
    from pipelines.target_shipment_forecast.config import load_config

    return load_config()
