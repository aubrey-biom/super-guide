"""Signal admission gate.

A signal is admitted at a horizon ONLY if, on the backtest, its WAPE beats the
naive benchmark (lag-1 replenishment actual) at that horizon AND its absolute
bias is below 15% (`config/target.yaml` gate.max_abs_bias, operational
choice). Uncertainty on the WAPE comparison comes from a week-block bootstrap
(`shipcast.backtest.scoring.week_block_bootstrap_ci`, 1000 resamples, 80%
interval): a signal whose interval overlaps the benchmark's point is admitted
but flagged `GATE_MARGINAL`.

Today's verdicts (2026-09-03/04): plan ORDERED_Q admitted at every horizon;
DFE (Pearson 0.13-0.17, +98% bias, feed dead since 2026-07-27) and trailing
POS (+35-41% bias) rejected as PO drivers; kept as context columns and as POS
forecast candidates for the monthly consumption view.
"""

from __future__ import annotations

import pandas as pd


def admit_signals(
    scores: pd.DataFrame,
    *,
    benchmark_signal: str = "lag1_rep",
    max_abs_bias: float = 0.15,
    n_boot: int = 1000,
    alpha: float = 0.2,
) -> pd.DataFrame:
    """Decide admission per (signal, horizon).

    Args:
        scores: long backtest rows `origin, horizon, tcin, week, signal, forecast, actual`
            (output of `shipcast.backtest.rolling`).
        benchmark_signal: the naive signal each candidate must beat.
        max_abs_bias: admission bound on |bias|.
        n_boot, alpha: week-block bootstrap settings for the WAPE interval.

    Returns:
        One row per (signal, horizon): `wape, wape_lo, wape_hi, bias, benchmark_wape,
        admitted, marginal, reason`.
    """
    raise NotImplementedError("v1: implemented in the model pass")
