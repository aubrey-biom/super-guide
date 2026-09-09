"""Configuration loading: `config/target.yaml` and repo-relative data paths.

Every threshold in the YAML carries a `basis:` field (a measured number with
its date, or the literal "operational choice, not measured"). `load_config`
returns the raw mapping; typed views live next to their consumers
(`channels.target.calendar.TargetCalendar.from_config`).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

_HERE = Path(__file__).resolve()


def repo_root() -> Path:
    """Repository root: `$SHIPCAST_ROOT` if set, else two levels above `src/shipcast`."""
    env = os.environ.get("SHIPCAST_ROOT")
    return Path(env).expanduser() if env else _HERE.parents[2]


def config_path(channel: str = "target") -> Path:
    """Path of the channel's YAML config."""
    return repo_root() / "config" / f"{channel}.yaml"


def data_dir() -> Path:
    """Directory holding the committed data files (item master, alias maps)."""
    return repo_root() / "data"


def load_config(path: Path | None = None, *, channel: str = "target") -> dict[str, Any]:
    """Load a channel config as a plain mapping.

    Dates in the YAML (e.g. `fiscal_year_start.value`) arrive as `datetime.date`
    because PyYAML's safe loader parses ISO dates natively.
    """
    p = path or config_path(channel)
    with open(p, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        raise ValueError(f"{p} did not parse to a mapping")
    return cfg


def threshold(cfg: dict[str, Any], *keys: str, field: str = "value") -> Any:
    """Read a threshold leaf, e.g. `threshold(cfg, "streams", "forward_threshold_days")`.

    Walks `keys` through nested mappings and returns `node[field]`.
    """
    node: Any = cfg
    for k in keys:
        node = node[k]
    return node[field]


def iter_numeric_nodes(
    node: Any, path: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    """Every mapping that directly holds a numeric (non-bool) leaf, with its path.

    Used by tests to enforce the `basis:` rule.
    """
    out: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    if isinstance(node, dict):
        if any(isinstance(v, int | float) and not isinstance(v, bool) for v in node.values()):
            out.append((path, node))
        for k, v in node.items():
            out.extend(iter_numeric_nodes(v, (*path, str(k))))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(iter_numeric_nodes(v, (*path, str(i))))
    return out
