from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from shipcast.channels.target.calendar import (
    TargetCalendar,
    department_class_from_dpci,
    sunday_week,
    sunday_week_start,
)


def test_sunday_week_start_anchors_on_sunday() -> None:
    assert sunday_week_start(date(2026, 9, 3)) == date(2026, 8, 30)  # Thursday -> Sunday
    assert sunday_week_start(date(2026, 8, 30)) == date(2026, 8, 30)  # Sunday stays
    assert sunday_week_start(date(2026, 9, 5)) == date(2026, 8, 30)  # Saturday -> same week
    s = pd.Series(pd.to_datetime(["2026-09-03", "2026-08-30", "2026-09-06"]))
    assert list(sunday_week(s).dt.date) == [date(2026, 8, 30), date(2026, 8, 30), date(2026, 9, 6)]


def test_ship_offsets_and_transit(config: dict[str, Any]) -> None:
    cal = TargetCalendar.from_config(config)
    sun = date(2026, 8, 30)
    mon = date(2026, 8, 31)
    assert cal.ship_begin(sun, "D3-C2") == date(2026, 9, 4)  # +5 Friday
    assert cal.ship_begin(mon, "D253-C4") == date(2026, 9, 5)  # +5 Saturday
    assert cal.ship_begin(mon, "D7") == date(2026, 9, 7)  # +7 Monday
    assert cal.ship_end(date(2026, 9, 4)) == date(2026, 9, 5)
    assert cal.ship_week(mon, "D7") == date(2026, 9, 6)
    assert cal.transit_days(553) == 5 and cal.transit_days(3856) == 5
    assert cal.transit_days(551) == 16 and cal.transit_days(3802) == 16
    assert cal.transit_days(594) == 12
    assert cal.eta(date(2026, 9, 4), 579) == date(2026, 9, 20)


def test_item_groups(config: dict[str, Any]) -> None:
    cal = TargetCalendar.from_config(config)
    assert cal.item_group_for(3, 2) == "D3-C2"
    assert cal.item_group_for(253, 4) == "D253-C4"
    assert cal.item_group_for(253, 6) == "D253-C6"  # flushables, launch 2026-10-11
    assert cal.item_group_for(7, 7) == "D7"
    assert cal.item_group_for(7, 1) == "D7"
    assert cal.item_group_for(3, 9) is None
    assert department_class_from_dpci("003-02-1080") == (3, 2)
    assert department_class_from_dpci("253-04-0012") == (253, 4)
    assert department_class_from_dpci(None) == (None, None)


def test_fiscal_week(config: dict[str, Any]) -> None:
    cal = TargetCalendar.from_config(config)
    assert cal.fiscal_week(date(2026, 2, 1)) == (2026, 1)
    assert cal.fiscal_week(date(2026, 2, 7)) == (2026, 1)
    assert cal.fiscal_week(date(2026, 2, 8)) == (2026, 2)
    assert cal.fiscal_week(date(2026, 8, 30)) == (2026, 31)
    assert cal.fiscal_week(date(2026, 1, 31)) == (2025, 52)
