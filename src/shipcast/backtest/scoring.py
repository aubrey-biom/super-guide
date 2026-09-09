"""Forecast accuracy metrics.

WAPE is the primary metric; pooled MAPE is deliberately absent (small-actual
rows inflate it). Exact-match and within-10% shares are computed only on rows
where actual > 0 or forecast > 0 (a 0/0 row is not a hit). Median APE is over
rows with actual > 0. The week-block bootstrap resamples WEEKS, not rows,
because rows in the same week share the PO event.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np
import pandas as pd

Metric = Callable[[pd.Series, pd.Series], float]


def _pair(actual: pd.Series, forecast: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    a = pd.to_numeric(pd.Series(actual), errors="coerce").to_numpy(dtype=float)
    f = pd.to_numeric(pd.Series(forecast), errors="coerce").to_numpy(dtype=float)
    ok = ~(np.isnan(a) | np.isnan(f))
    return a[ok], f[ok]


def wape(actual: pd.Series, forecast: pd.Series) -> float:
    """sum|f - a| / sum|a|; NaN when sum|a| == 0."""
    a, f = _pair(actual, forecast)
    denom = np.abs(a).sum()
    return float(np.abs(f - a).sum() / denom) if denom > 0 else float("nan")


def bias(actual: pd.Series, forecast: pd.Series) -> float:
    """(sum f - sum a) / sum a; positive = over-forecast; NaN when sum a == 0."""
    a, f = _pair(actual, forecast)
    denom = a.sum()
    return float((f.sum() - a.sum()) / denom) if denom != 0 else float("nan")


def median_ape(actual: pd.Series, forecast: pd.Series) -> float:
    """median(|f - a| / a) over rows with a > 0."""
    a, f = _pair(actual, forecast)
    m = a > 0
    return float(np.median(np.abs(f[m] - a[m]) / a[m])) if m.any() else float("nan")


def scored_mask(actual: pd.Series, forecast: pd.Series) -> np.ndarray:
    """Rows that count for share metrics: actual > 0 or forecast > 0."""
    a, f = _pair(actual, forecast)
    return (a > 0) | (f > 0)


def exact_share(actual: pd.Series, forecast: pd.Series, *, tol_units: float = 0.5) -> float:
    """Share of scored rows with |f - a| <= tol_units."""
    a, f = _pair(actual, forecast)
    m = (a > 0) | (f > 0)
    return float((np.abs(f[m] - a[m]) <= tol_units).mean()) if m.any() else float("nan")


def within_share(actual: pd.Series, forecast: pd.Series, *, tol: float = 0.10) -> float:
    """Share of scored rows with |f - a| <= tol * a (a == 0 rows can only miss)."""
    a, f = _pair(actual, forecast)
    m = (a > 0) | (f > 0)
    return float((np.abs(f[m] - a[m]) <= tol * a[m]).mean()) if m.any() else float("nan")


def score(actual: pd.Series, forecast: pd.Series) -> dict[str, float]:
    """All metrics plus counts, as a flat dict."""
    a, f = _pair(actual, forecast)
    return {
        "n": float(len(a)),
        "n_scored": float(((a > 0) | (f > 0)).sum()),
        "sum_actual": float(a.sum()),
        "sum_forecast": float(f.sum()),
        "wape": wape(actual, forecast),
        "bias": bias(actual, forecast),
        "median_ape": median_ape(actual, forecast),
        "exact_share": exact_share(actual, forecast),
        "within_10_share": within_share(actual, forecast, tol=0.10),
    }


def score_table(
    df: pd.DataFrame,
    *,
    actual_col: str,
    forecast_col: str,
    by: Sequence[str] | None = None,
) -> pd.DataFrame:
    """`score()` per group (or once), as a DataFrame."""
    if not by:
        return pd.DataFrame([score(df[actual_col], df[forecast_col])])
    rows = []
    for keys, g in df.groupby(list(by), dropna=False):
        key_tuple = keys if isinstance(keys, tuple) else (keys,)
        row = dict(zip(by, key_tuple, strict=True))
        row.update(score(g[actual_col], g[forecast_col]))
        rows.append(row)
    return pd.DataFrame(rows)


def week_block_bootstrap_ci(
    df: pd.DataFrame,
    *,
    week_col: str,
    actual_col: str,
    forecast_col: str,
    metric: Metric = wape,
    n_boot: int = 1000,
    alpha: float = 0.2,
    seed: int = 0,
) -> tuple[float, float, float]:
    """`(point, lo, hi)`: resample whole weeks with replacement and recompute `metric`.

    `alpha = 0.2` gives an 80% interval. Weeks are the exchangeable unit because
    every TCIN row in a week comes from the same three PO events.
    """
    weeks = df[week_col].dropna().unique()
    point = metric(df[actual_col], df[forecast_col])
    if len(weeks) < 2:
        return point, float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    groups = dict(iter(df.groupby(week_col)))
    stats = np.empty(n_boot)
    for b in range(n_boot):
        pick = rng.choice(weeks, size=len(weeks), replace=True)
        sample = pd.concat([groups[w] for w in pick], ignore_index=True)
        stats[b] = metric(sample[actual_col], sample[forecast_col])
    lo, hi = np.nanquantile(stats, [alpha / 2, 1 - alpha / 2])
    return point, float(lo), float(hi)
