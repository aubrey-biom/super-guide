from __future__ import annotations

from datetime import date
from typing import Any

from pipelines.target_shipment_forecast.config import iter_numeric_nodes, threshold

MEASURED_OR_CHOICE = ("measured", "assumed", "operational choice, not measured")


def _basis_for(path: tuple[str, ...], cfg: dict[str, Any]) -> str | None:
    node: Any = cfg
    for k in path:
        node = node[int(k)] if isinstance(node, list) else node[k]
    if isinstance(node, dict) and "basis" in node:
        return str(node["basis"])
    parent: Any = cfg
    for k in path[:-1]:
        parent = parent[int(k)] if isinstance(parent, list) else parent[k]
    if isinstance(parent, dict) and "basis" in parent:
        return str(parent["basis"])
    grand: Any = cfg
    for k in path[:-2]:
        grand = grand[int(k)] if isinstance(grand, list) else grand[k]
    if isinstance(grand, dict) and "basis" in grand:
        return str(grand["basis"])
    return None


def test_every_threshold_has_a_basis(config: dict[str, Any]) -> None:
    missing = []
    for path, _node in iter_numeric_nodes(config):
        basis = _basis_for(path, config)
        if basis is None:
            missing.append("/".join(path))
        elif not any(basis.startswith(p) for p in MEASURED_OR_CHOICE):
            missing.append(
                "/".join(path)
                + f" (basis {basis!r} is neither measured nor the literal choice string)"
            )
    assert not missing, missing


def test_headline_values(config: dict[str, Any]) -> None:
    assert threshold(config, "streams", "forward_threshold_days") == 14
    assert threshold(config, "freshness", "stale_plan_days") == 4
    assert config["calendar"]["fiscal_year_start"]["value"] == date(2026, 2, 1)
    assert config["consumption"]["steady_state_wos_band"]["low"] == 5
    assert config["consumption"]["steady_state_wos_band"]["high"] == 10
    assert set(config["item_groups"]) == {"D3-C2", "D253-C4", "D253-C6", "D7"}
    assert config["item_groups"]["D7"]["ship_offset_days"] == 7
    # flushables (dept 253 class 6) launch 2026-10-11: Monday PO day, +5 d ship, measured
    assert config["item_groups"]["D253-C6"]["po_day"] == "monday"
    assert config["item_groups"]["D253-C6"]["ship_offset_days"] == 5
    assert config["bm_schedule"]["ramp_cap"]["value"] == 3.0
    assert config["bm_schedule"]["include_stated_only_months"]["value"] is True
    assert config["grades"]["by_lead_days"]["A"] == {"min": 0, "max": 1}
