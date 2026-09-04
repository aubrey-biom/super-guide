"""Rolling-origin backtest driver.

Origins are Saturdays (the day before each PO week). For each origin and
horizon h, a forecaster returns `(tcin, week, forecast, source_ts)`; the driver
joins actuals and stamps `origin, horizon, lead_days`. `panel_to_long` adapts
the committed signal panel (`tests/fixtures/signal_panel.csv`), whose feature
columns were built as-of the Saturday before each week, so the same driver
scores today's signals without a warehouse.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

Forecaster = Callable[[date, int], pd.DataFrame]
"""`forecaster(origin, horizon) -> DataFrame[tcin, week, forecast, source_ts]`."""

LONG_COLUMNS: tuple[str, ...] = (
    "origin",
    "horizon",
    "tcin",
    "week",
    "signal",
    "forecast",
    "actual",
    "source_ts",
    "lead_days",
)


def make_origins(start: date, end: date, *, step_days: int = 7) -> list[date]:
    """Dates from `start` to `end` inclusive, every `step_days`."""
    out: list[date] = []
    d = start
    while d <= end:
        out.append(d)
        d += timedelta(days=step_days)
    return out


def rolling_backtest(
    forecaster: Forecaster,
    actuals: pd.DataFrame,
    origins: Iterable[date],
    horizons: Sequence[int],
    *,
    signal: str,
    actual_col: str = "act_rep",
    week_col: str = "wk",
) -> pd.DataFrame:
    """Run `forecaster` at every origin x horizon and join `actuals` (`wk, tcin, <actual_col>`)."""
    act = actuals[[week_col, "tcin", actual_col]].rename(
        columns={week_col: "week", actual_col: "actual"}
    )
    act["week"] = pd.to_datetime(act["week"])
    frames: list[pd.DataFrame] = []
    for o in origins:
        for h in horizons:
            f = forecaster(o, h).copy()
            if f.empty:
                continue
            f["origin"] = pd.Timestamp(o)
            f["horizon"] = int(h)
            f["signal"] = signal
            f["week"] = pd.to_datetime(f["week"])
            frames.append(f)
    if not frames:
        return pd.DataFrame(columns=list(LONG_COLUMNS))
    out = pd.concat(frames, ignore_index=True).merge(act, on=["week", "tcin"], how="left")
    out["actual"] = out["actual"].fillna(0.0)
    if "source_ts" not in out:
        out["source_ts"] = pd.NaT
    out["source_ts"] = pd.to_datetime(out["source_ts"])
    out["lead_days"] = (out["week"] - out["source_ts"]).dt.days
    return out[list(LONG_COLUMNS)]


@dataclass(frozen=True)
class PanelSignal:
    """How one panel column maps onto the long form."""

    signal: str
    column: str
    horizon: int
    asof_column: str | None


DEFAULT_PANEL_SIGNALS: tuple[PanelSignal, ...] = (
    PanelSignal("plan_sat_ordered", "plan_sat_ordered_W", 1, "plan_sat_asof"),
    PanelSignal("plan_sat_ordered", "plan_sat_ordered_W1", 2, "plan_sat_asof"),
    PanelSignal("plan_sat_ordered", "plan_sat_ordered_W2", 3, "plan_sat_asof"),
    PanelSignal("plan_thu_ordered", "plan_thu_ordered_W", 1, "plan_thu_asof"),
    PanelSignal("plan_biweekly_ordered", "bw_ordered_W", 1, "bw_asof"),
    PanelSignal("dfe", "dfe_W", 1, "dfe_asof"),
    PanelSignal("lag1_rep", "lag1_rep", 1, None),
    PanelSignal("mean4_rep", "mean4_rep", 1, None),
)


def panel_to_long(
    panel: pd.DataFrame,
    signals: Sequence[PanelSignal] = DEFAULT_PANEL_SIGNALS,
    *,
    actual_col: str = "act_rep",
    week_col: str = "wk",
    only_complete: bool = True,
    only_active: bool = True,
) -> pd.DataFrame:
    """Melt the committed signal panel into long backtest rows.

    origin = the Saturday before the panel week (`wk - 1 day`); for horizon h the
    forecast targets week `wk + 7*(h-1)` days and is compared with that week's
    actual. `source_ts` is the panel's as-of column for the signal (NaT for
    naive lags, which are actuals of earlier weeks by construction).
    """
    df = panel.copy()
    df[week_col] = pd.to_datetime(df[week_col])
    if only_complete and "week_complete" in df:
        df = df[df["week_complete"].astype(str).str.lower() == "true"]
    if only_active and "active" in df:
        df = df[df["active"].astype(str).str.lower() == "true"]
    act = df[[week_col, "tcin", actual_col]].rename(
        columns={week_col: "week", actual_col: "actual"}
    )
    frames: list[pd.DataFrame] = []
    for s in signals:
        if s.column not in df:
            continue
        f = pd.DataFrame(
            {
                "origin": df[week_col] - pd.Timedelta(days=1),
                "horizon": s.horizon,
                "tcin": df["tcin"],
                "week": df[week_col] + pd.Timedelta(days=7 * (s.horizon - 1)),
                "signal": s.signal,
                "forecast": pd.to_numeric(df[s.column], errors="coerce"),
                "source_ts": pd.to_datetime(df[s.asof_column]) if s.asof_column else pd.NaT,
            }
        )
        frames.append(f)
    out = pd.concat(frames, ignore_index=True).merge(act, on=["week", "tcin"], how="inner")
    out["lead_days"] = (out["week"] - out["source_ts"]).dt.days
    return out[list(LONG_COLUMNS)]
