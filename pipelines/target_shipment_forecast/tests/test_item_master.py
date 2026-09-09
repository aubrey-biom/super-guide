from __future__ import annotations

import pytest

from pipelines.target_shipment_forecast.inputs.item_master import ItemMaster, load_item_master


@pytest.fixture(scope="module")
def im() -> ItemMaster:
    return ItemMaster.load()


def test_loads_43_tcins(im: ItemMaster) -> None:
    df = load_item_master()
    assert len(df) == 43
    assert df["tcin"].dtype == "int64"
    assert len(im.tcins) == 43 and im.tcins == sorted(im.tcins)


def test_tcin_to_sku(im: ItemMaster) -> None:
    assert im.sku_for(89854821) == "P-DIS-EUC"
    assert im.sku_for(94799739) == "K-60WIP-BAB-FRA-4PK"
    # biom_sku missing, rdz_item present -> fallback
    assert im.sku_for(95285661) == "P-60WIP-FLU-FRA"
    assert im.sku_for(95285661, fallback_to_rdz=False) is None
    assert im.sku_for(1) is None


def test_sku_to_tcins(im: ItemMaster) -> None:
    assert im.tcins_for("P-DIS-EUC") == [89854821]
    assert im.tcins_for("p-dis_euc") == [89854821]
    assert im.tcins_for("P-60WIP-FLU-FRA") == [95285660, 95285661]  # shared RDZ pool
    assert im.tcins_for("NOPE") == []


def test_casepack_of_record(im: ItemMaster) -> None:
    cp = im.casepack_of_record(89854821)
    assert cp.casepack == 6.0 and cp.source == "orders.VENDOR_CASEPACK_Q"
    cp = im.casepack_of_record(93197978)
    assert cp.casepack is None


def test_rdz_item_and_multiplier(im: ItemMaster) -> None:
    assert im.rdz_item_for(95285661) == ("P-60WIP-FLU-FRA", 3)
    assert im.rdz_item_for(95285660) == ("P-60WIP-FLU-FRA", 2)
    assert im.rdz_item_for(89854821) == ("P-DIS-EUC", 1)


def test_unmapped_and_department_class(im: ItemMaster) -> None:
    assert len(im.unmapped()) == 4
    assert im.department_class(89854821) == (3, 2)
