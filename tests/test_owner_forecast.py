from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from shipcast.inputs.aliases import AliasMap
from shipcast.inputs.item_master import ItemMaster
from shipcast.inputs.owner_forecast import (
    BM_MASTER_FORECAST_FORMAT,
    TOOL_NATIVE_FORMAT,
    detect_format,
    load_owner_forecast,
    parse_bm_master_forecast,
    parse_period,
)


def test_parse_period() -> None:
    assert parse_period("2026-09") == date(2026, 9, 1)
    assert parse_period("2026-W36") == date(2026, 8, 30)  # Sunday before ISO Monday 2026-08-31
    assert parse_period("2026-09-06") == date(2026, 9, 6)
    with pytest.raises(ValueError):
        parse_period("Sep 26")


def test_tool_native_csv(tmp_path: Path) -> None:
    csv = tmp_path / "owner.csv"
    csv.write_text(
        "channel,item_key,item_key_type,period,units,unit_type,source,notes\n"
        "target,P-LDIS-EUC,sku,2026-09,1200,sell_in_units,owner sheet,\n"
        "target,P-LDIS-EUC,sku,2026-09,999,sell_in_units,owner sheet,duplicate row\n"
        "target,94799739,tcin,2026-10,300,sell_in_units,,\n"
        "target,30,sku,2026-11,50,sell_in_units,,row keyed 30\n"
        "target,P-60WIP-FLU-FRA,sku,2026-10,50,sell_in_units,,shared pool -> ambiguous\n"
        "target,NOT-A-SKU,sku,2026-10,50,sell_in_units,,\n"
        "target,P-DIS-WHI,sku,bad-period,50,sell_in_units,,\n"
    )
    with pytest.warns(UserWarning, match="duplicate owner rows collapsed"):
        parsed = load_owner_forecast(
            csv, aliases=AliasMap.load(), item_master=ItemMaster.load(), as_of=date(2026, 9, 3)
        )
    assert parsed.format == TOOL_NATIVE_FORMAT
    rows = parsed.rows
    assert len(rows) == 3
    euc = rows[rows["sku"] == "P-DIS-EUC"].iloc[0]
    assert euc["tcin"] == 89854821 and euc["forecast_units"] == 1200  # first kept, never summed
    assert set(rows["tcin"]) == {89854821, 94799739}
    assert (rows["channel"] == "TARGET").all()
    assert (rows["as_of"] == date(2026, 9, 3)).all()
    assert list(rows.columns) == [
        "channel",
        "sku",
        "tcin",
        "period_start",
        "forecast_units",
        "forecast_basis",
        "source_sheet",
        "as_of",
    ]
    unresolved = parsed.unresolved
    assert len(unresolved) == 3
    reasons = set(unresolved["reason"])
    assert any(r.startswith("sku_maps_to_multiple_tcins") for r in reasons)
    assert "sku_has_no_tcin" in reasons
    assert any("unrecognised period" in r for r in reasons)


def test_detect_format_registry() -> None:
    assert (
        detect_format(
            [
                "channel",
                "item_key",
                "item_key_type",
                "period",
                "units",
                "unit_type",
                "source",
                "notes",
            ]
        ).name
        == TOOL_NATIVE_FORMAT
    )
    bm = detect_format(
        ["Metric", "SKU / Description", "Unique Key", "Jan-2025", "Feb-2025", "Dec-2028"]
    )
    assert (
        bm is not None
        and bm.name == BM_MASTER_FORECAST_FORMAT
        and bm.parser is parse_bm_master_forecast
    )
    assert detect_format(["foo", "bar"]) is None
