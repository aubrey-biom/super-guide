"""Empirical prediction bands from the backtest.

For each lead bucket (A: 0-1 d, B: 2-3 d, C: >= 4 d) the backtest rows give
`r = log(actual + c) - log(forecast + c)`. P10/P90 of `r` scale a point
forecast into `low`/`high`. Buckets with fewer than `min_pool_rows` rows are
pooled with all rows. Nothing is asserted: `lowo_coverage` reports how often the
actual landed inside the band when the band was fitted on the OTHER weeks.

Rows with forecast == 0 and actual == 0 carry no information and are dropped;
rows with forecast == 0 and actual > 0 are kept (they widen the upper band,
which is the honest effect of a missed order).
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

BUCKETS: tuple[str, ...] = ("A", "B", "C")
POOLED = "pooled"


def lead_bucket(lead_days: pd.Series) -> pd.Series:
    """Lead days -> A (0-1), B (2-3), C (>= 4 or unknown)."""
    ld = pd.to_numeric(lead_days, errors="coerce")
    return pd.Series(np.select([ld <= 1, ld <= 3], ["A", "B"], default="C"), index=lead_days.index)


def _log_ratio(actual: pd.Series, forecast: pd.Series, c: float) -> pd.Series:
    a = pd.to_numeric(actual, errors="coerce").astype(float)
    f = pd.to_numeric(forecast, errors="coerce").astype(float)
    return np.log(a + c) - np.log(f + c)


def _informative(rows: pd.DataFrame) -> pd.DataFrame:
    a = pd.to_numeric(rows["actual"], errors="coerce").fillna(0)
    f = pd.to_numeric(rows["forecast"], errors="coerce").fillna(0)
    return rows[(a > 0) | (f > 0)]


def fit_ratio_quantiles(
    rows: pd.DataFrame,
    *,
    c: float = 1.0,
    q: tuple[float, float] = (0.10, 0.90),
    min_pool_rows: int = 100,
) -> pd.DataFrame:
    """Per bucket: `q_lo`, `q_hi` of the log ratio, `n`, and whether it was pooled.

    `rows` needs `actual`, `forecast`, `lead_days` (long backtest rows for the
    plan signal). Returns one row per bucket in `BUCKETS` plus `pooled`.
    """
    r = _informative(rows).copy()
    r["bucket"] = lead_bucket(r["lead_days"])
    r["lr"] = _log_ratio(r["actual"], r["forecast"], c)
    pooled_lo, pooled_hi = np.nanquantile(r["lr"], q) if len(r) else (np.nan, np.nan)
    out = [{"bucket": POOLED, "q_lo": pooled_lo, "q_hi": pooled_hi, "n": len(r), "pooled": True}]
    for b in BUCKETS:
        g = r[r["bucket"] == b]
        if len(g) >= min_pool_rows:
            lo, hi = np.nanquantile(g["lr"], q)
            out.append({"bucket": b, "q_lo": lo, "q_hi": hi, "n": len(g), "pooled": False})
        else:
            out.append(
                {
                    "bucket": b,
                    "q_lo": pooled_lo,
                    "q_hi": pooled_hi,
                    "n": len(g),
                    "pooled": True,
                }
            )
    return pd.DataFrame(out)


def apply_bands(
    forecast: pd.DataFrame,
    fitted: pd.DataFrame,
    *,
    value_col: str = "expected_po_units",
    c: float = 1.0,
) -> pd.DataFrame:
    """Add `low`/`high` = (value + c) * exp(q) - c per row's lead bucket, floored at 0."""
    df = forecast.copy()
    lut: Mapping[str, tuple[float, float]] = {
        r.bucket: (float(r.q_lo), float(r.q_hi)) for r in fitted.itertuples()
    }
    b = lead_bucket(df["lead_days"]) if "lead_days" in df else pd.Series("C", index=df.index)
    lo = b.map(lambda k: lut.get(k, lut.get(POOLED, (np.nan, np.nan)))[0])
    hi = b.map(lambda k: lut.get(k, lut.get(POOLED, (np.nan, np.nan)))[1])
    v = pd.to_numeric(df[value_col], errors="coerce").astype(float)
    df["low"] = ((v + c) * np.exp(lo) - c).clip(lower=0).round(0)
    df["high"] = ((v + c) * np.exp(hi) - c).clip(lower=0).round(0)
    df["band_source"] = "empirical_lowo" if not fitted.empty else "assumed"
    return df


def lowo_coverage(
    rows: pd.DataFrame,
    *,
    c: float = 1.0,
    q: tuple[float, float] = (0.10, 0.90),
    min_pool_rows: int = 100,
    week_col: str = "week",
) -> pd.DataFrame:
    """Leave-one-week-out realised coverage of the P10-P90 band per bucket.

    For each week, quantiles are fitted on the other weeks and applied to that
    week's rows. Returns `bucket, n, coverage` (share of rows inside the band).
    """
    r = _informative(rows).copy()
    r[week_col] = pd.to_datetime(r[week_col])
    r["bucket"] = lead_bucket(r["lead_days"])
    hits: dict[str, list[bool]] = {b: [] for b in (*BUCKETS, POOLED)}
    for w in sorted(r[week_col].unique()):
        train = r[r[week_col] != w]
        test = r[r[week_col] == w]
        if train.empty or test.empty:
            continue
        fitted = fit_ratio_quantiles(train, c=c, q=q, min_pool_rows=min_pool_rows)
        lut = {x.bucket: (float(x.q_lo), float(x.q_hi)) for x in fitted.itertuples()}
        a = pd.to_numeric(test["actual"], errors="coerce").fillna(0).astype(float)
        f = pd.to_numeric(test["forecast"], errors="coerce").fillna(0).astype(float)
        for bkt in (*BUCKETS, POOLED):
            sel = test if bkt == POOLED else test[test["bucket"] == bkt]
            if sel.empty:
                continue
            lo, hi = lut.get(bkt, lut[POOLED])
            fa = f.loc[sel.index]
            aa = a.loc[sel.index]
            inside = ((fa + c) * np.exp(lo) - c <= aa) & (aa <= (fa + c) * np.exp(hi) - c)
            hits[bkt].extend(inside.tolist())
    return pd.DataFrame(
        [
            {"bucket": b, "n": len(v), "coverage": (float(np.mean(v)) if v else np.nan)}
            for b, v in hits.items()
        ]
    )


__all__ = [
    "BUCKETS",
    "POOLED",
    "apply_bands",
    "fit_ratio_quantiles",
    "lead_bucket",
    "lowo_coverage",
]
