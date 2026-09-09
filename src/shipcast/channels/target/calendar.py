"""Target calendar: Sunday-anchored fiscal weeks, PO->ship offsets, DC transit.

Facts this encodes (measured 2026-09-03 unless noted; see config/target.yaml
for the `basis` of every number):

* Weeks run Sunday..Saturday and are labelled by the Sunday. This matches the
  DFE feed's `fiscal_week_begin_d` (100% Sundays) and the backtest panel in
  `tests/fixtures/signal_panel.csv`. Weekly sales/inventory feeds carry the
  SATURDAY week-end; `week_start()` maps them onto the same label.
* Target raises three replenishment POs a week, one per item group:
  D3-C2 cleaning (Sunday), D253-C4 personal care (Monday), D7 baby (Monday).
  Replenishment lines ship `create + 5 d` for D3-C2 and D253-C4 and
  `create + 7 d` for D7; the ship window is always 1 day.
* ETA = ship_begin + a fixed per-DC transit: 5 d for 553/555/593/3806/3856/588,
  16 d for 551/579/3802, 12 d default (operational choice).
* Fiscal year start 2026-02-01 is ASSUMED (confirm with the buyer). Fiscal
  years are rolled in 364-day blocks from it, which is right until a 53-week
  year appears.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pandas as pd

_SUNDAY_OFFSET = 1  # Python weekday(): Monday=0 ... Sunday=6; (weekday+1) % 7 is days since Sunday


def sunday_week_start(d: date) -> date:
    """The Sunday on or before `d`."""
    return d - timedelta(days=(d.weekday() + _SUNDAY_OFFSET) % 7)


def sunday_week(s: pd.Series) -> pd.Series:
    """Vectorised `sunday_week_start` for a datetime Series (normalised to midnight)."""
    dt = pd.to_datetime(s).dt.normalize()
    return dt - pd.to_timedelta((dt.dt.weekday + _SUNDAY_OFFSET) % 7, unit="D")


@dataclass(frozen=True)
class ItemGroup:
    """One of Target's three replenishment PO groups."""

    code: str
    name: str
    department_id: int
    class_id: int | None
    po_day: str
    ship_offset_days: int

    def matches(self, department_id: int, class_id: int | None) -> bool:
        """Does a (department, class) pair belong to this group?"""
        if department_id != self.department_id:
            return False
        return self.class_id is None or class_id == self.class_id


@dataclass(frozen=True)
class TargetCalendar:
    """Sunday-anchored calendar with ship offsets and DC transit from config."""

    groups: Mapping[str, ItemGroup]
    transit_default_days: int
    transit_overrides: Mapping[int, int]
    fiscal_year_start: date
    ship_window_days: int = 1
    _fy_block_days: int = field(default=364, repr=False)

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> TargetCalendar:
        """Build from the mapping returned by `shipcast.config.load_config`."""
        groups = {
            code: ItemGroup(
                code=code,
                name=str(g["name"]),
                department_id=int(g["department_id"]),
                class_id=None if g.get("class_id") is None else int(g["class_id"]),
                po_day=str(g["po_day"]),
                ship_offset_days=int(g["ship_offset_days"]),
            )
            for code, g in cfg["item_groups"].items()
        }
        transit = cfg["dc_transit_days"]
        overrides: dict[int, int] = {}
        for o in transit.get("overrides", []):
            for dc in o["dcs"]:
                overrides[int(dc)] = int(o["days"])
        fy = cfg["calendar"]["fiscal_year_start"]["value"]
        fy_date = fy if isinstance(fy, date) else date.fromisoformat(str(fy))
        return cls(
            groups=groups,
            transit_default_days=int(transit["default"]["days"]),
            transit_overrides=overrides,
            fiscal_year_start=fy_date,
            ship_window_days=int(cfg["calendar"]["ship_window_days"]["value"]),
        )

    # -- weeks ---------------------------------------------------------------

    def week_start(self, d: date) -> date:
        """Sunday on or before `d` (the week label)."""
        return sunday_week_start(d)

    def week_end(self, d: date) -> date:
        """Saturday of the week containing `d`."""
        return sunday_week_start(d) + timedelta(days=6)

    def fiscal_week(self, d: date) -> tuple[int, int]:
        """`(fiscal_year, fiscal_week)` for `d`, weeks numbered from 1.

        Fiscal years are rolled in `_fy_block_days` (364-day) blocks from the
        configured start. Fiscal-year label = calendar year of the block start.
        """
        start = self.fiscal_year_start
        ws = self.week_start(d)
        blocks = (ws - start).days // self._fy_block_days
        fy_start = start + timedelta(days=blocks * self._fy_block_days)
        week_no = (ws - fy_start).days // 7 + 1
        return fy_start.year, week_no

    # -- item groups ---------------------------------------------------------

    def item_group_for(self, department_id: int | None, class_id: int | None) -> str | None:
        """Group code for a (department, class) pair, or None if not a known group."""
        if department_id is None:
            return None
        for code, g in self.groups.items():
            if g.matches(int(department_id), class_id):
                return code
        return None

    # -- ship calendar -------------------------------------------------------

    def ship_offset_days(self, item_group: str) -> int:
        """Days from PO creation to `ship_begin` for a group."""
        return self.groups[item_group].ship_offset_days

    def ship_begin(self, create_d: date, item_group: str) -> date:
        """First ship day for a replenishment PO created on `create_d`."""
        return create_d + timedelta(days=self.ship_offset_days(item_group))

    def ship_end(self, ship_begin: date) -> date:
        """Last ship day: `ship_begin + ship_window_days`."""
        return ship_begin + timedelta(days=self.ship_window_days)

    def ship_week(self, create_d: date, item_group: str) -> date:
        """Sunday label of the week the PO ships."""
        return self.week_start(self.ship_begin(create_d, item_group))

    def transit_days(self, dc: int) -> int:
        """Fixed transit days for a receiving DC (override or default)."""
        return self.transit_overrides.get(int(dc), self.transit_default_days)

    def eta(self, ship_begin: date, dc: int) -> date:
        """Expected arrival at `dc` for a shipment leaving on `ship_begin`."""
        return ship_begin + timedelta(days=self.transit_days(dc))


def department_class_from_dpci(dpci: str | None) -> tuple[int | None, int | None]:
    """Parse Target's DPCI (`DDD-CC-IIII`) into (department_id, class_id)."""
    if not dpci or not isinstance(dpci, str):
        return None, None
    parts = dpci.split("-")
    if len(parts) != 3:
        return None, None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None, None
