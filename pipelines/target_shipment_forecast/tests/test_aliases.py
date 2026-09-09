from __future__ import annotations

import pytest

from pipelines.target_shipment_forecast.inputs.aliases import AliasMap, normalise_key, normalise_upc


@pytest.fixture(scope="module")
def aliases() -> AliasMap:
    return AliasMap.load()


def test_normalise_key_rules() -> None:
    nk = normalise_key("  p-40wip-lit_fra  ")
    assert nk.key == "P-40WIP-LIT-FRA" and nk.is_pdq is False
    nk = normalise_key("P-40WIP-6IN-SAN-STL   (pdq)")
    assert nk.key == "P-40WIP-6IN-SAN-STL" and nk.is_pdq is True
    assert normalise_key("K-60WIP-FLU-FRA-3Pk").key == "K-60WIP-FLU-FRA-3PK"
    assert normalise_key("a   b").key == "A B"
    assert normalise_upc("085005629809") == "85005629809"


@pytest.mark.parametrize(
    ("alias", "canonical", "is_pdq"),
    [
        ("P-LDIS-TER", "P-DIS-TER", False),
        ("P-LDIS-WHI", "P-DIS-WHI", False),
        ("P-LDIS-EUC", "P-DIS-EUC", False),
        ("P-SSDIS-BRU", "P-DIS-SS-BRU", False),
        ("P-SSDIS-BLU", "P-DIS-SS-BLU", False),
        ("K-60WIP-AP-COM-3PK", "K-60WIP-AP-COM", False),
        ("K-60WIP-DSN-COM", "K-60WIP-DSN-COM-3PK", False),
        ("30", "K-60WIP-BAB-FRA-4PK", False),
        ("P-40WIP-LIT_FRA", "P-40WIP-LIT-FRA", False),
        ("K-60WIP-FLU-FRA-3Pk", "K-60WIP-FLU-FRA-3PK", False),
        ("P-40WIP-6IN-SAN-STL-4", "P-40WIP-6IN-SAN-STL", True),
    ],
)
def test_target_aliases(aliases: AliasMap, alias: str, canonical: str, is_pdq: bool) -> None:
    res = aliases.resolve(alias)
    assert res.canonical_sku == canonical
    assert res.is_pdq is is_pdq
    assert res.base_qty == 1
    if res.source == "identity":
        # normalisation alone already produced the canonical spelling
        assert normalise_key(alias).key == canonical
    else:
        assert res.source == "sku_aliases_target"


def test_pdq_handling(aliases: AliasMap) -> None:
    res = aliases.resolve("P-40WIP-LIT-FRA (PDQ)")
    assert res.canonical_sku == "P-40WIP-LIT-FRA" and res.is_pdq is True
    assert res.rdz_item == "P-40WIP-LIT-FRA (PDQ)"
    res = aliases.resolve("P-40WIP-6IN-SAN-STL-4")
    assert res.rdz_item == "P-40WIP-6IN-SAN-STL (PDQ)"


def test_upstream_upc_and_multipack(aliases: AliasMap) -> None:
    res = aliases.resolve("850088546002")
    assert res.canonical_sku == "P-40WIP-6IN-SAN-STL" and res.base_qty == 3
    assert res.source == "item_aliases_upstream:upc"
    res = aliases.resolve("085005629809", kind="upc")  # case code: check digit dropped, zero-padded
    assert res.canonical_sku == "P-60WIP-AP-STL" and res.source == "item_aliases_upstream:upc11"
    assert (
        aliases.resolve("850088546111").matched is False
    )  # 12-digit placeholder must NOT truncate-match
    res = aliases.resolve("WIP-AP-STL")
    assert res.canonical_sku == "P-60WIP-AP-STL" and res.source == "item_aliases_upstream:code"


def test_unmapped_returns_identity_never_drops(aliases: AliasMap) -> None:
    res = aliases.resolve("not-a-real_sku")
    assert res.matched is False and res.source == "identity"
    assert res.canonical_sku == "NOT-A-REAL-SKU"
