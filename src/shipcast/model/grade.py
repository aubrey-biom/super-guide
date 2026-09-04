"""Row grades and plain-language confidence labels.

Weekly rows are graded by plan lead days (measured WAPE by lead, 2026-09-04):
A = lead 0-1 (0.12-0.18), B = lead 2-3 (0.25-0.31), C = lead >= 4 (0.42-0.62).
Rows produced by a fallback rung are graded by the rung, not by lead days:
`created` -> A (it is the order feed), `plan_zero_history_positive` and
`no_signal` -> C, `beyond_plan_horizon` and `no_plan_snapshot` -> D.

Monthly rows are graded by how much of the month is covered by plan rows and
how far out the month is (see `shipcast.model.consumption.grade_month`).

Every grade maps to a `confidence` label and an `expected error band` that the
workbook prints next to the number. The bands come from
`config/target.yaml` grades.labels, each with a `basis`; A/B/C bands are the
measured WAPE range rounded outward, D and E are assumed and say so.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

GRADE_ORDER: tuple[str, ...] = ("A", "B", "C", "D", "E")

RUNG_GRADES: Mapping[str, str] = {
    "created": "A",
    "plan_zero_history_positive": "C",
    "no_signal": "C",
    "beyond_plan_horizon": "D",
    "no_plan_snapshot": "D",
}

DEFAULT_LABELS: Mapping[str, Mapping[str, Any]] = {
    "A": {
        "label": "high",
        "band_pct": 0.15,
        "meaning": "Target's plan, 0-1 days old; measured error 12-18%",
    },
    "B": {
        "label": "good",
        "band_pct": 0.30,
        "meaning": "Target's plan, 2-3 days old; measured error 25-31%",
    },
    "C": {
        "label": "directional",
        "band_pct": 0.50,
        "meaning": "plan 4+ days old, or model-based; measured error 42-62%",
    },
    "D": {
        "label": "plan-based",
        "band_pct": 0.70,
        "meaning": "beyond Target's plan; owner forecast and run-rate; band assumed",
    },
    "E": {
        "label": "indicative",
        "band_pct": 1.00,
        "meaning": "beyond 12 months or no history; direction only",
    },
}


def worse(a: str, b: str) -> str:
    """The lower of two grades (A best)."""
    return a if GRADE_ORDER.index(a) >= GRADE_ORDER.index(b) else b


def grade_for_lead_days(
    lead_days: int | float | None, buckets: Mapping[str, Mapping[str, Any]]
) -> str:
    """Map lead days onto a letter using `cfg["grades"]["by_lead_days"]` (`max: null` = open)."""
    if lead_days is None or pd.isna(lead_days):
        return "D"
    ld = int(lead_days)
    for letter, b in buckets.items():
        lo = b.get("min")
        hi = b.get("max")
        if (lo is None or ld >= int(lo)) and (hi is None or ld <= int(hi)):
            return str(letter)
    return "C"


def grade_rows(forecast: pd.DataFrame, buckets: Mapping[str, Mapping[str, Any]]) -> pd.DataFrame:
    """Add `grade` to a plan-anchor frame using `lead_days` and `fallback_rung`."""
    df = forecast.copy()
    by_lead = df["lead_days"].map(lambda x: grade_for_lead_days(x, buckets))
    by_rung = df["fallback_rung"].map(RUNG_GRADES)
    df["grade"] = by_rung.where(by_rung.notna(), by_lead)
    return df


def labels_from_config(cfg: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Grade -> {label, band_pct, meaning, basis}; config overrides the defaults."""
    out: dict[str, dict[str, Any]] = {k: dict(v) for k, v in DEFAULT_LABELS.items()}
    node = (cfg or {}).get("grades", {}).get("labels") if cfg else None
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, dict):
                out.setdefault(str(k), {}).update(v)
    return out


def attach_confidence(
    df: pd.DataFrame, labels: Mapping[str, Mapping[str, Any]], *, value_col: str
) -> pd.DataFrame:
    """Add `confidence` ("A · high · ±15%"), `band_pct`, `low`, `high` from the grade.

    `low`/`high` are the symmetric band around `value_col`, floored at 0. Rows
    that already carry empirical `low`/`high` (from `model.intervals`) keep them.
    """
    out = df.copy()
    band = out["grade"].map(lambda g: float(labels.get(g, DEFAULT_LABELS["E"])["band_pct"]))
    out["band_pct"] = band
    out["confidence"] = [
        f"{g} · {labels.get(g, DEFAULT_LABELS['E'])['label']} · ±{round(b * 100)}%"
        for g, b in zip(out["grade"], band, strict=True)
    ]
    if "low" not in out or out["low"].isna().all():
        out["low"] = (out[value_col] * (1 - band)).clip(lower=0).round(0)
    else:
        out["low"] = out["low"].where(
            out["low"].notna(), (out[value_col] * (1 - band)).clip(lower=0).round(0)
        )
    if "high" not in out or out["high"].isna().all():
        out["high"] = (out[value_col] * (1 + band)).round(0)
    else:
        out["high"] = out["high"].where(out["high"].notna(), (out[value_col] * (1 + band)).round(0))
    return out


def legend(labels: Mapping[str, Mapping[str, Any]]) -> pd.DataFrame:
    """Grade legend as a frame for the README / Monthly sheet."""
    rows = []
    for g in GRADE_ORDER:
        v = labels.get(g)
        if not v:
            continue
        rows.append(
            {
                "grade": g,
                "confidence": v.get("label"),
                "expected_error_band": f"±{round(float(v.get('band_pct', 0)) * 100)}%",
                "meaning": v.get("meaning", ""),
                "basis": v.get(
                    "basis", "measured 2026-09-04 (A-C); assumed until backtested (D-E)"
                ),
            }
        )
    return pd.DataFrame(rows)


__all__ = [
    "DEFAULT_LABELS",
    "GRADE_ORDER",
    "RUNG_GRADES",
    "attach_confidence",
    "grade_for_lead_days",
    "grade_rows",
    "labels_from_config",
    "legend",
    "worse",
]
