from __future__ import annotations

import re
from datetime import date

import pandas as pd
import pytest

from shipcast.supply.base import INBOUND_COLUMNS, ON_HAND_COLUMNS, SupplyAdapter
from shipcast.supply.rdz_sheet import (
    RdzParseError,
    RdzSheetAdapter,
    inbound_frame,
    normalise_header,
    on_hand_frame,
    on_hand_long,
    parse_banner_date,
    parse_inventory_summary,
    parse_number,
    parse_us_date,
    unescape_cell,
)


def test_cell_helpers() -> None:
    assert unescape_cell(r"Item \#") == "Item #"
    assert unescape_cell(r"[merged] Low Stock? (\<= threshold)") == "Low Stock? (<= threshold)"
    assert normalise_header("Inbound Arrival Date  ") == "inbound arrival date"
    assert parse_number(r"\-6,252") == -6252.0
    assert parse_number(" $ 1,110,517.00 ") == 1110517.0
    assert parse_number("(265)") == -265.0
    assert parse_number("") is None and parse_number("-") is None and parse_number("N/A") is None
    assert parse_number("KIT BUILT IN DC") is None
    assert parse_us_date("9/18/2026") == date(2026, 9, 18)
    assert parse_us_date("8/21/26") == date(2026, 8, 21)
    assert parse_us_date("TBD") is None
    assert parse_banner_date("Last updated: August 31, 1:35 PM", 2026) == date(2026, 8, 31)


def test_fixture_parses_with_identity_and_row_count(rdz_text: str) -> None:
    snap = parse_inventory_summary(rdz_text, year=2026)
    assert snap.n_items == 127
    assert snap.as_of == date(2026, 8, 31)
    assert snap.checks["identity_share"] == 1.0
    assert snap.checks["identity_failures"] == []
    assert snap.checks["duplicate_items"] == 0
    assert snap.checks["parse_errors"] == 0
    items = snap.items.set_index("item")
    assert items.loc["P-20WIP-SAN-BER-TRV", "available"] == -102672  # negative ATP is legal
    assert items.loc["P-20WIP-SAN-BER-TRV", "physical"] == 0
    assert bool(items.loc["P-40WIP-6IN-SAN-STL (PDQ)", "is_pdq"]) is True
    assert items.loc["P-40WIP-6IN-SAN-STL (PDQ)", "base_sku"] == "P-40WIP-6IN-SAN-STL"
    assert "P-40WIP-6IN-SAN-STL" in items.index  # PDQ and plain lines are separate items
    assert (
        items.loc["P-60WIP-AP-LAV", "physical"] == 867
        and items.loc["P-60WIP-AP-LAV", "available"] == 531
    )


def test_on_hand_frames(rdz_text: str) -> None:
    snap = parse_inventory_summary(rdz_text, year=2026)
    wide = on_hand_frame(snap)
    assert list(wide.columns[: len(ON_HAND_COLUMNS)]) == list(ON_HAND_COLUMNS)
    assert len(wide) == 127
    long = on_hand_long(snap)
    assert len(long) == 254
    assert set(long["measure"]) == {"available", "on_hand_physical"}
    cit = long[(long["item_key"] == "P-60WIP-DSN-CIT")].set_index("measure")["qty_eaches"]
    assert cit["available"] == 171430 and cit["on_hand_physical"] == 171958


def test_inbound_rows(rdz_text: str) -> None:
    snap = parse_inventory_summary(rdz_text, year=2026)
    inb = inbound_frame(snap)
    assert list(inb.columns[: len(INBOUND_COLUMNS)]) == list(INBOUND_COLUMNS)
    counts = inb["confidence"].value_counts().to_dict()
    assert counts == {"dated": 4, "po_placed": 1, "tbd": 6}
    dated = inb[inb["confidence"] == "dated"].set_index("item")
    assert (
        dated.loc["P-60WIP-AP-LAV", "eta"] == date(2026, 10, 1)
        and dated.loc["P-60WIP-AP-LAV", "qty"] == 30000
    )
    assert dated.loc["P-20WIP-SAN-STL-TRV", "qty"] == 50000
    tbd = inb[inb["confidence"] == "tbd"].set_index("item")
    assert tbd.loc["K-60WIP-DSN-COM-3PK", "qty"] == 45000
    assert pd.isna(tbd.loc["P-60WIP-DSN-CIT", "qty"])
    assert (inb["status"] == "open").all()
    assert inb["shipment_id"].iloc[0].startswith("RDZ-SUMMARY:")
    reasons = snap.no_inbound["reason"].value_counts().to_dict()
    assert reasons["kit built in dc"] >= 50 and reasons["old stock"] == 16
    assert "healthy inv" in reasons and "blank" in reasons


def test_adapter_protocol(fixtures_dir) -> None:  # type: ignore[no-untyped-def]
    adapter = RdzSheetAdapter(fixtures_dir / "rdz_inventory_summary.txt")
    assert isinstance(adapter, SupplyAdapter)
    oh = adapter.on_hand(date(2026, 9, 3))
    assert len(oh) == 127 and int(oh["age_days"].iloc[0]) == 3
    assert len(adapter.inbound(date(2026, 9, 3))) == 11


def test_fail_loud_checks(rdz_text: str) -> None:
    # banner missing
    no_banner = re.sub(
        r"Last updated:[^|]*", "", rdz_text
    )  # the time carries a narrow no-break space
    with pytest.raises(RdzParseError, match="banner"):
        parse_inventory_summary(no_banner, year=2026)
    # too few rows
    lines = rdz_text.splitlines()
    short = "\n".join(lines[:60])
    with pytest.raises(RdzParseError, match="expected 100-200"):
        parse_inventory_summary(short, year=2026)
    # identity broken on many rows
    broken = rdz_text.replace("| 4 | 63,556 |", "| 4 | 63,557 |")
    snap = parse_inventory_summary(broken, year=2026)  # one row is within the 1% tolerance
    assert snap.checks["identity_failures"] == ["K-60WIP-AP-COM"]
    # header not found
    with pytest.raises(RdzParseError, match="not found"):
        parse_inventory_summary("| a | b |\n| 1 | 2 |\n", year=2026)


def test_locates_by_header_text_not_position(rdz_text: str) -> None:
    prefix = "| ABC | P_SKU | Next Inbound Date |\n| :-: | :-: | :-: |\n| C | P-DIS-BLK | May 8, 2026 |\n\nsome prose\n\n"
    snap = parse_inventory_summary(prefix + rdz_text, year=2026)
    assert snap.n_items == 127
