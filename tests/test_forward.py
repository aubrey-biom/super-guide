from __future__ import annotations

from datetime import date

import pandas as pd

from shipcast.channels.target.calendar import sunday_week
from shipcast.channels.target.forward import (
    FORWARD,
    REPLENISHMENT,
    classify_lines,
    open_lines,
    weekly_by_stream,
)


def _orders() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "purchase_order_id": [1, 1, 2, 3],
            "tcin": [10, 11, 10, 12],
            "receiving_location_id": [553, 553, 551, 579],
            "purchase_order_create_d": pd.to_datetime(
                ["2026-08-30", "2026-08-30", "2026-08-31", "2026-08-31"]
            ),
            "revised_ship_begin_d": pd.to_datetime(
                ["2026-09-04", "2026-09-04", "2026-10-03", None]
            ),
            "revised_ship_end_d": pd.to_datetime(["2026-09-05", "2026-09-05", "2026-10-04", None]),
            "original_ship_begin_d": pd.to_datetime([None, None, None, "2026-09-07"]),
            "original_ship_end_d": pd.to_datetime([None, None, None, "2026-09-08"]),
            "revised_order_q": [48, 24, 600, 12],
            "original_order_q": [48, 30, 600, 12],
            "item_received_q": [48, 0, 0, 20],
            "cancel_remaining_order_q": [0, 6, 0, 0],
        }
    )


def test_classification_open_and_lapsed() -> None:
    lines = classify_lines(
        _orders(), as_of=date(2026, 9, 20), forward_threshold_days=14, lapsed_grace_days=7
    )
    assert list(lines["stream"]) == [REPLENISHMENT, REPLENISHMENT, FORWARD, REPLENISHMENT]
    assert list(lines["ship_lag_days"]) == [5, 5, 33, 7]
    assert list(lines["open_units"]) == [0, 18, 600, 0]
    assert bool(lines["negative_open"].iloc[3]) is True  # received 20 > revised 12
    # ship_end 09-05 + 7 d grace < 09-20 -> lapsed; the forward line (10-04) is not
    assert list(lines["lapsed"]) == [True, True, False, True]
    assert list(open_lines(lines)["purchase_order_id"]) == [2]


def test_weekly_by_stream() -> None:
    lines = classify_lines(_orders(), as_of=date(2026, 9, 1))
    wk = weekly_by_stream(lines, sunday_week)
    row = wk[wk["tcin"] == 10].iloc[0]
    assert row["wk"] == pd.Timestamp("2026-08-30")
    assert row["act_all"] == 648 and row["act_rep"] == 48 and row["act_fwd"] == 600
    assert row["n_po"] == 2 and row["n_dc"] == 2 and row["n_fwd_lines"] == 1
