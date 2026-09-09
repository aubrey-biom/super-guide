"""Brick & Mortar Master Forecast, tab "Target Schedule" (Drive export)."""

from __future__ import annotations

import warnings
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from shipcast.inputs import drive_export as de
from shipcast.inputs.aliases import AliasMap
from shipcast.inputs.item_master import ItemMaster
from shipcast.inputs.owner_forecast import (
    BM_MASTER_FORECAST_FORMAT,
    ParsedOwnerForecast,
    bm_month_start,
    detect_format,
    load_owner_forecast,
    parse_bm_number,
)


@pytest.fixture(scope="module")
def export_text(fixtures_dir: Path) -> str:
    return (fixtures_dir / "bm_target_schedule_export.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def parsed(fixtures_dir: Path) -> ParsedOwnerForecast:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return load_owner_forecast(
            fixtures_dir / "bm_target_schedule_export.txt",
            aliases=AliasMap.load(),
            item_master=ItemMaster.load(),
            as_of=date(2026, 9, 4),
        )


def _total(rows: pd.DataFrame, start: date, end: date) -> float:
    t = rows[
        (rows["forecast_basis"] == "total")
        & (rows["period_start"] >= start)
        & (rows["period_start"] <= end)
    ]
    return float(t["forecast_units"].fillna(0).sum())


def test_tokenizer_and_rerow() -> None:
    toks = de.tokenize_csv('a,"b, c",d e,"f ""q"" g",h')
    assert toks == ["a", "b, c", "d e", 'f "q" g', "h"]
    # two rows of width 3, row break rendered as a space inside the boundary token
    rows = de.rows_from_tokens(de.tokenize_csv("Metric,x,1 Stores,y,2"), 3)
    assert rows == [["Metric", "x", "1"], ["Stores", "y", "2"]]
    # boundary with an empty last cell
    rows = de.rows_from_tokens(de.tokenize_csv("Metric,x, Stores,y,2"), 3)
    assert rows == [["Metric", "x", ""], ["Stores", "y", "2"]]
    with pytest.raises(de.DriveExportError):
        de.rows_from_tokens(de.tokenize_csv("a,b,c,d"), 3)
    assert (
        de.unescape_markdown(r"TARGET\_P-DIS-TER \# \! \< \> \[x\] \& \*")
        == "TARGET_P-DIS-TER # ! < > [x] & *"
    )


def test_number_and_month_parsing() -> None:
    assert parse_bm_number("-") == 0.0
    assert parse_bm_number("") is None
    assert parse_bm_number("7,056.00") == 7056.0
    assert parse_bm_number("$12.59") == 12.59
    assert parse_bm_number("-$602.29") == -602.29
    assert parse_bm_number("0.537") == 0.537
    assert parse_bm_number("15%") == 0.15
    assert parse_bm_number("KIT") is None
    assert bm_month_start("Sep-2026") == date(2026, 9, 1)
    assert bm_month_start("Metric") is None


def test_export_rerows_to_357_x_51(export_text: str) -> None:
    assert de.is_drive_export(export_text)
    frame = de.schedule_tab_frame(export_text, "Target Schedule")
    assert frame.shape == (356, 51)  # 357 rows including the header
    assert list(frame.columns[:3]) == ["Metric", "SKU / Description", "Unique Key"]
    assert frame.columns[3] == "Jan-2025" and frame.columns[-1] == "Dec-2028"
    assert frame.iloc[0, 0] == "Days in Month"
    assert detect_format(frame.columns).name == BM_MASTER_FORECAST_FORMAT


def test_blocks_totals_and_identity(parsed: ParsedOwnerForecast) -> None:
    rows = parsed.rows
    assert parsed.format == BM_MASTER_FORECAST_FORMAT
    assert rows["sku"].nunique() == 39
    assert len(rows) == 39 * 48 * 3
    assert set(rows["forecast_basis"]) == {"velocity", "load_orders", "total"}
    assert (rows["channel"] == "TARGET").all() and (rows["source_tab"] == "Target Schedule").all()
    assert _total(rows, date(2026, 9, 1), date(2026, 12, 1)) == pytest.approx(1_076_212, abs=5)
    assert _total(rows, date(2026, 6, 1), date(2026, 8, 1)) == pytest.approx(522_609, abs=5)
    piv = rows.pivot_table(
        index=["sku", "period_start"],
        columns="forecast_basis",
        values="forecast_units",
        aggfunc="first",
    )
    gap = piv["total"].fillna(0) - piv["velocity"].fillna(0) - piv["load_orders"].fillna(0)
    assert (gap.abs() <= 0.02).all()
    assert not any(w.startswith("TOTAL_IDENTITY") for w in parsed.warnings)
    # first block: P-DIS-TER resolves via vendor_style/biom_sku; description from the UPSPW row
    ter = rows[
        (rows["sku"] == "P-DIS-TER")
        & (rows["period_start"] == date(2026, 9, 1))
        & (rows["forecast_basis"] == "total")
    ].iloc[0]
    assert ter["tcin"] == 94723688 and ter["description"] == "Home Dispenser - Terracotta"
    assert ter["quote_usd"] == 12.59 and ter["unique_key"] == "TARGET_P-DIS-TER"
    assert ter["dept"] == "DEPT 3 — CLEANING" and ter["category"] == "HOME DISPENSERS (D3)"
    assert rows["tcin"].dtype == "Int64"


def test_resolution_rungs(parsed: ParsedOwnerForecast) -> None:
    by_sku = parsed.rows.drop_duplicates("sku").set_index("sku")["tcin"]
    assert by_sku["P-DIS-WHI"] == 89854823  # biom_sku
    assert by_sku["K-60WIP-FLU-FRA-2PK"] == 95285660  # K-/P- prefix swap onto vendor_style
    assert by_sku["K-60WIP-FLU-FRA-3PK"] == 95285661
    assert pd.isna(by_sku["P-DIS-SS-BLU"])  # no Target TCIN
    assert pd.isna(by_sku["P-60WIP-FLU-FRA"])  # ambiguous shared RDZ pool -> null, flagged
    assert set(parsed.unresolved["item_key"]) == {
        "P-DIS-SS-BLU",
        "P-10WIP-BOD-NAT-TRV",
        "P-60WIP-FLU-FRA",
    }


def test_warnings_flag_not_drop(parsed: ParsedOwnerForecast) -> None:
    placeholders = {
        w.split()[1].rstrip(":") for w in parsed.warnings if w.startswith("PLACEHOLDER_STORES_1")
    }
    assert placeholders == {
        "P-DIS-BLK",
        "P-DIS-DGR",
        "P-DIS-LGR",
        "K-DIS-2BAB-LGR",
        "K-DIS-2BAB-PUR",
    }
    dup = [w for w in parsed.warnings if w.startswith("DUPLICATE_TOTAL_DEMAND P-30WIP-LIT-FRA")]
    assert dup and "P-30WIP-BAB-FRA" in dup[0]
    assert any(w.startswith("SKU_NO_TCIN P-DIS-SS-BLU") for w in parsed.warnings)
    # flagged SKUs are still present in the rows
    assert {"P-DIS-BLK", "P-30WIP-LIT-FRA", "P-DIS-SS-BLU"} <= set(parsed.rows["sku"])


def test_worst_case_tab_is_ignored(export_text: str, tmp_path: Path) -> None:
    mini = (
        "README a,b,c Target Schedule "
        + export_text
        + " TARGET WORST CASE (DO NOT USE) "
        + export_text
        + " SKU Reference SKU Reference,,,,, Retailer,Category"
    )
    p = tmp_path / "bm_export.txt"
    p.write_text(mini, encoding="utf-8")
    assert de.find_tab(mini, "SKU Reference", known_tabs=de.KNOWN_TABS).startswith(
        "SKU Reference,,,,,"
    )
    assert de.find_tab(mini, "Nope") is None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        parsed = load_owner_forecast(
            p, aliases=AliasMap.load(), item_master=ItemMaster.load(), as_of=date(2026, 9, 4)
        )
    assert parsed.rows["sku"].nunique() == 39
    assert len(parsed.rows) == 39 * 48 * 3  # the WORST CASE copy contributed nothing
