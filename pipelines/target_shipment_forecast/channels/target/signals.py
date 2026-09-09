"""Target signal pulls: BigQuery SQL against the logical-table registry.

Every function returns a DataFrame and takes an injectable `run` callable
(`run(sql, params=...) -> DataFrame`, default `shipcast.bq.query`) so tests can
assert the SQL without a warehouse. The `*_sql` builders are public for the
same reason.

Rule: SQL is written against logical names (`orders_daily`, `sales_weekly`,
`inventory_weekly`, `item_attr_extended`) and passed through
`bq.logical`, which injects those CTEs — including the `orders_daily` QUALIFY that
reduces ~150k accumulated snapshot rows to ~7.8k latest-state lines. The bodies live
in `bq.LOGICAL_TABLES`, copied verbatim from bullseye's registry when this package was
forked into biom_sql (2026-09-07); the comment on each statement below still names the
body it relies on, and a test enforces that. Two deliberate raw-table exceptions exist
because the logical tables are latest-state and would leak in a backtest:

1. `plan_snapshot(s)` read `bpd_raw.dly_po_plan_tcin` filtered to specific
   BUSINESS_D values (never unfiltered: 5.8M rows of accumulating snapshots).
2. `dfe_asof` reads `bpd_raw.dfe_wkly_item_loc_forecast` filtered to
   `LAST_UPDATE_D <= as_of` before applying the same newest-snapshot QUALIFY.

Each SQL string carries a comment naming the logical body it relies on.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import date
from typing import Any

import pandas as pd

from pipelines.target_shipment_forecast import bq

log = logging.getLogger(__name__)

QueryFn = Callable[..., pd.DataFrame]

_PROJECT = bq.PROJECT
RAW_PO_PLAN = f"`{_PROJECT}.bpd_raw.dly_po_plan_tcin`"
RAW_ORDERS = f"`{_PROJECT}.bpd_raw.daily_order_tcin_loc`"
RAW_DFE = f"`{_PROJECT}.bpd_raw.dfe_wkly_item_loc_forecast`"


def _runner(run: QueryFn | None) -> QueryFn:
    return run or bq.query


# --------------------------------------------------------------------------------------
# PO plan snapshots (raw as-of exception 1)
# --------------------------------------------------------------------------------------


def plan_snapshot_sql() -> str:
    """Daily PO plan rows for the BUSINESS_D values in `@business_dates` (ARRAY<DATE>)."""
    return f"""
-- shipcast.channels.target.signals.plan_snapshot
-- AS-OF EXCEPTION. Reads bpd_raw.dly_po_plan_tcin directly instead of the
-- logical table `po_plan_daily`. Column list and aliases mirror the `po_plan_daily`
-- body in bq.LOGICAL_TABLES (which is deliberately NOT de-duplicated: it accumulates one
-- snapshot per BUSINESS_D, ~5.8M rows over ~118 dates). The mandatory BUSINESS_D
-- filter is the whole point; never read this table unfiltered.
SELECT BUSINESS_D AS business_d, TCIN AS tcin, ORDER_D AS order_d,
       RECEIVING_LOCATION_ID AS receiving_location_id, DEPARTMENT_ID AS department_id,
       DPCI AS dpci, VENDOR_CASE_PACK_Q AS vendor_case_pack_q,
       ORDERED_Q AS ordered_q, RECEIVED_Q AS received_q, SCHEDULED_RECEIPT_Q AS scheduled_receipt_q,
       NET_STORE_MEAN_DEMAND_Q AS net_store_mean_demand_q,
       BEGINNING_SALESFLOOR_PRESENTATION_UNIT_Q AS beginning_salesfloor_presentation_unit_q,
       ENDING_SALESFLOOR_PRESENTATION_UNIT_Q AS ending_salesfloor_presentation_unit_q
FROM {RAW_PO_PLAN}
WHERE BUSINESS_D IN UNNEST(@business_dates)
"""


def plan_business_dates_sql() -> str:
    """Distinct plan BUSINESS_D values on or before `@as_of`, newest first, `@limit` rows."""
    return f"""
-- shipcast.channels.target.signals.plan_business_dates
-- Relies on the same source as logical `po_plan_daily` (bpd_raw.dly_po_plan_tcin) but
-- touches one column only, to find which snapshots exist before pulling any rows.
SELECT DISTINCT BUSINESS_D AS business_d
FROM {RAW_PO_PLAN}
WHERE BUSINESS_D <= @as_of
ORDER BY business_d DESC
LIMIT @limit
"""


def plan_business_dates(as_of: date, *, limit: int = 10, run: QueryFn | None = None) -> list[date]:
    """Snapshot dates available on or before `as_of`, newest first."""
    df = _runner(run)(plan_business_dates_sql(), params={"as_of": as_of, "limit": int(limit)})
    return [pd.Timestamp(x).date() for x in df["business_d"]]


def plan_snapshots(dates: Iterable[date], *, run: QueryFn | None = None) -> pd.DataFrame:
    """Plan rows (TCIN x order_d x receiving DC) for exactly the given snapshot dates."""
    ds = sorted(set(dates))
    if not ds:
        raise ValueError(
            "plan_snapshots needs at least one BUSINESS_D; unfiltered reads are forbidden"
        )
    return _runner(run)(plan_snapshot_sql(), params={"business_dates": ds})


def plan_snapshot(business_d: date, *, run: QueryFn | None = None) -> pd.DataFrame:
    """Plan rows for one snapshot date."""
    return plan_snapshots([business_d], run=run)


def plan_chain_by_order_week(
    plan: pd.DataFrame, week_start: Callable[[pd.Series], pd.Series]
) -> pd.DataFrame:
    """Sum plan ORDERED_Q over receiving DCs into (business_d, tcin, order_week).

    This is the plan-anchor signal: expected weekly PO units = ORDERED_Q for the
    order day, summed over DCs (docs/PLAN.md section 3.3).
    """
    df = plan.copy()
    df["order_week"] = week_start(df["order_d"])
    g = df.groupby(["business_d", "tcin", "order_week"], as_index=False).agg(
        ordered_q=("ordered_q", "sum"),
        scheduled_receipt_q=("scheduled_receipt_q", "sum"),
        net_store_mean_demand_q=("net_store_mean_demand_q", "sum"),
        n_dc=("receiving_location_id", "nunique"),
    )
    return g


# --------------------------------------------------------------------------------------
# Orders (latest state via logical orders_daily)
# --------------------------------------------------------------------------------------


def orders_latest_sql() -> str:
    """Latest-state PO lines with ship window and pack columns."""
    return f"""
-- shipcast.channels.target.signals.orders_latest
-- Relies on the logical table `orders_daily` (bq.LOGICAL_TABLES): its QUALIFY reduces
-- the accumulating daily_order_tcin_loc feed to the latest state per
-- (purchase_order_id, tcin, receiving_location_id) with the full tie-break order
-- (SNAPSHOT_D, ITEM_RECEIVED_Q, CANCEL_REMAINING_ORDER_Q, REVISED_ORDER_Q,
-- ORIGINAL_ORDER_Q, TO_JSON_STRING). ~150k raw rows -> ~7.8k lines.
-- The logical projection omits the ship window and pack columns shipcast needs
-- (replen/forward split, casepack of record), so they are joined back from the raw
-- table on the SAME snapshot_d and line key. The de-dup itself is never re-derived here.
SELECT o.*,
       x.original_ship_begin_d, x.original_ship_end_d,
       x.revised_ship_begin_d, x.revised_ship_end_d,
       x.vendor_casepack_q, x.store_shippack_q
FROM orders_daily AS o
LEFT JOIN (
  SELECT PURCHASE_ORDER_ID AS purchase_order_id, TCIN AS tcin,
         RECEIVING_LOCATION_ID AS receiving_location_id, SNAPSHOT_D AS snapshot_d,
         MAX(ORIGINAL_SHIP_BEGIN_D) AS original_ship_begin_d,
         MAX(ORIGINAL_SHIP_END_D) AS original_ship_end_d,
         MAX(REVISED_SHIP_BEGIN_D) AS revised_ship_begin_d,
         MAX(REVISED_SHIP_END_D) AS revised_ship_end_d,
         MAX(VENDOR_CASEPACK_Q) AS vendor_casepack_q,
         MAX(STORE_SHIPPACK_Q) AS store_shippack_q
  FROM {RAW_ORDERS}
  GROUP BY 1, 2, 3, 4
) AS x
USING (purchase_order_id, tcin, receiving_location_id, snapshot_d)
"""


def orders_latest(*, run: QueryFn | None = None) -> pd.DataFrame:
    """All latest-state PO lines (one row per PO x TCIN x receiving DC)."""
    return _runner(run)(bq.logical(orders_latest_sql()))


def orders_line_count_sql() -> str:
    """Row count of the de-duplicated order table — the `shipcast check` probe (~7.8k)."""
    return """
-- shipcast.channels.target.signals.orders_line_count
-- Relies on logical `orders_daily` (QUALIFY de-dup). Raw is ~150k rows; this should be ~7.8k.
SELECT COUNT(*) AS n_lines, COUNT(DISTINCT purchase_order_id) AS n_pos, MAX(snapshot_d) AS max_snapshot_d
FROM orders_daily
"""


def orders_line_count(*, run: QueryFn | None = None) -> pd.DataFrame:
    """One-row frame: n_lines, n_pos, max_snapshot_d."""
    return _runner(run)(bq.logical(orders_line_count_sql()))


def po_actuals_weekly(
    orders: pd.DataFrame | None = None,
    *,
    as_of: date | None = None,
    forward_threshold_days: int = 14,
    lapsed_grace_days: int = 7,
    run: QueryFn | None = None,
) -> pd.DataFrame:
    """Weekly PO units by TCIN, split into replenishment and forward streams.

    Pure pandas over `orders_latest()`: the split needs `revised_ship_begin_d -
    purchase_order_create_d`, which is why it is not a GROUP BY in SQL. Columns:
    `wk, tcin, act_all, act_rep, act_fwd, act_orig_all, n_po, n_dc, n_fwd_lines`.
    """
    from pipelines.target_shipment_forecast.channels.target import forward
    from pipelines.target_shipment_forecast.channels.target.calendar import sunday_week

    lines = orders if orders is not None else orders_latest(run=run)
    classified = forward.classify_lines(
        lines,
        as_of=as_of or date.today(),
        forward_threshold_days=forward_threshold_days,
        lapsed_grace_days=lapsed_grace_days,
    )
    return forward.weekly_by_stream(classified, sunday_week)


# --------------------------------------------------------------------------------------
# Sales and inventory (logical weekly unions: canvas ∪ raw)
# --------------------------------------------------------------------------------------


def sales_weekly_sql() -> str:
    """Chain weekly POS by TCIN between `@start` and `@end` (Saturday week-end dates)."""
    return """
-- shipcast.channels.target.signals.sales_weekly
-- Relies on the logical table `sales_weekly`: canvas weekly/history grains
-- UNION ALL the raw weekly_sales_tcin_loc feed beyond the canvas horizon (self-healing
-- MAX() boundary, no double count). sales_date is the SATURDAY week-END; week_start_d
-- below is the Sunday label shipcast uses everywhere.
SELECT sales_date AS week_end_d, DATE_SUB(sales_date, INTERVAL 6 DAY) AS week_start_d, tcin,
       SUM(sale_quantity) AS units, SUM(sale_amount) AS dollars,
       COUNT(DISTINCT location_id) AS locations
FROM sales_weekly
WHERE sales_date BETWEEN @start AND @end
GROUP BY 1, 2, 3
"""


def sales_weekly(start: date, end: date, *, run: QueryFn | None = None) -> pd.DataFrame:
    """Weekly chain POS units and dollars by TCIN."""
    return _runner(run)(bq.logical(sales_weekly_sql()), params={"start": start, "end": end})


def inventory_weekly_sql() -> str:
    """Weekly on-hand / on-purchase / on-transfer by TCIN, split DC vs store by `@dc_ids`."""
    return """
-- shipcast.channels.target.signals.inventory_weekly
-- Relies on the logical table `inventory_weekly`: canvas history_weekly grain
-- UNION ALL the raw weekly_inv_tcin_loc feed beyond the canvas horizon (same MAX()
-- boundary as sales_weekly). `business_d` is the logical alias of inventory_date and is
-- the SATURDAY week-end. is_dc uses the receiving-DC id list passed in @dc_ids.
SELECT business_d AS week_end_d, DATE_SUB(business_d, INTERVAL 6 DAY) AS week_start_d, tcin,
       location_id IN UNNEST(@dc_ids) AS is_dc,
       SUM(ending_on_hand_q) AS on_hand, SUM(ending_on_purchase_q) AS on_purchase,
       SUM(ending_on_transfer_q) AS on_transfer,
       COUNTIF(ending_on_hand_q > 0) AS locations_with_inventory,
       COUNT(DISTINCT location_id) AS locations,
       AVG(instock_percentage) AS instock_pct, AVG(out_of_stock_percentage) AS oos_pct
FROM inventory_weekly
WHERE business_d BETWEEN @start AND @end
GROUP BY 1, 2, 3, 4
"""


def inventory_weekly(
    start: date, end: date, *, dc_ids: Sequence[int] = (), run: QueryFn | None = None
) -> pd.DataFrame:
    """Weekly inventory position by TCIN, DC rows and store rows separated by `is_dc`."""
    return _runner(run)(
        bq.logical(inventory_weekly_sql()),
        params={"start": start, "end": end, "dc_ids": [int(x) for x in dc_ids]},
    )


def dc_ids_from_orders(orders: pd.DataFrame) -> list[int]:
    """Distinct receiving DC ids seen on PO lines — the `is_dc` universe for inventory."""
    return sorted(int(x) for x in orders["receiving_location_id"].dropna().unique())


# --------------------------------------------------------------------------------------
# DFE forecast as-of (raw as-of exception 2)
# --------------------------------------------------------------------------------------


def dfe_asof_sql() -> str:
    """Chain DFE forecast by TCIN x fiscal week as it stood on `@as_of`."""
    return f"""
-- shipcast.channels.target.signals.dfe_asof
-- AS-OF EXCEPTION. Reads bpd_raw.dfe_wkly_item_loc_forecast directly instead of the
-- logical table `forecast_weekly` (registered in bullseye, NOT ported here), whose QUALIFY keeps only the NEWEST
-- last_update_d per (tcin, location_id, fiscal_week_begin_d) and would therefore leak
-- post-origin snapshots into a backtest. Same QUALIFY, applied AFTER the as-of filter.
-- fiscal_week_begin_d is Sunday-anchored (Target schema quirk).
-- The feed has been stale since 2026-07-27; callers must surface last_update_d.
SELECT last_update_d, tcin, fiscal_week_begin_d,
       SUM(selected_forecast_q) AS forecast_units, COUNT(DISTINCT location_id) AS locations
FROM (
  SELECT LAST_UPDATE_D AS last_update_d, TCIN AS tcin, LOCATION_ID AS location_id,
         FISCAL_WEEK_BEGIN_D AS fiscal_week_begin_d, SELECTED_FORECAST_Q AS selected_forecast_q
  FROM {RAW_DFE}
  WHERE LAST_UPDATE_D <= @as_of
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY TCIN, LOCATION_ID, FISCAL_WEEK_BEGIN_D ORDER BY LAST_UPDATE_D DESC) = 1
)
GROUP BY 1, 2, 3
"""


def dfe_asof(as_of: date, *, run: QueryFn | None = None) -> pd.DataFrame:
    """DFE forecast (chain, by TCIN x Sunday week) using only snapshots up to `as_of`."""
    return _runner(run)(dfe_asof_sql(), params={"as_of": as_of})


# --------------------------------------------------------------------------------------
# Item attributes (item_state) — biom_sql fix (d), 2026-09-07
# --------------------------------------------------------------------------------------


def item_state_sql() -> str:
    """Target `ITEM STATE` per TCIN as it stood on `@as_of`, newest row per TCIN."""
    return """
-- shipcast.channels.target.signals.item_state_live
-- AS-OF FILTERED, mirroring dfe_asof: the `last_update_date <= @as_of` predicate is
-- applied BEFORE the newest-per-TCIN QUALIFY, so a historical origin can never see a
-- state Target had not yet published. Without it (biom_sql, first-run validation
-- 2026-09-07) a replay at as_of 2026-09-02 read states stamped 2026-09-05 and the
-- README showed a NEGATIVE age_days — the staleness guard cannot catch that, because a
-- negative age trivially passes `age > max_age`.
--
-- Reads the logical table `item_attr_extended` (bpd_raw.wkly_tcin_item) rather than the
-- raw table, which is safe here and is the one departure from dfe_asof's shape: that
-- statement must go raw because its logical table `forecast_weekly` applies a QUALIFY of
-- its own that would leak post-origin snapshots. `item_attr_extended` applies NO
-- reduction — it is a plain projection whose only job is aliasing the SPACE-SEPARATED
-- source columns (`ITEM STATE`, `LAST UPDATE DATE`) — so there is nothing to leak and no
-- reason to re-derive the aliasing here.
--
-- WHY THIS TABLE AND NOT `item_attr`: the sibling feed bpd_raw.weekly_item_mta that
-- `item_attr` wraps is STALE (max PROCESSED_CT_DATE 2026-07-25, verified 2026-09-07),
-- so reading item_state from it would be no fresher than the committed CSV this
-- replaces. wkly_tcin_item is refreshed by biom-bpd-kiteworks-daily.
--
-- `last_update_date` is Target's own per-item attribute timestamp and the only populated
-- DATE/TIMESTAMP column here; `launch_date` is a STRING carrying Target's "" placeholder
-- and must never be CAST. NOTE the feed is FULL-REPLACE, not accumulating: a TCIN whose
-- only row is stamped after `@as_of` drops out entirely rather than resolving to an older
-- value, because no older value exists anywhere. The caller reports those TCINs and falls
-- back to the committed item master for them — see pipeline._resolve_item_state.
SELECT tcin, item_state, last_update_date
FROM item_attr_extended
WHERE tcin IS NOT NULL
  AND DATE(last_update_date) <= @as_of   -- date-grain, exactly as dfe_asof compares LAST_UPDATE_D
QUALIFY ROW_NUMBER() OVER (PARTITION BY tcin ORDER BY last_update_date DESC) = 1
"""


def item_state_live(as_of: date, *, run: QueryFn | None = None) -> pd.DataFrame:
    """`tcin, item_state, last_update_date` as published on or before `as_of`."""
    return _runner(run)(bq.logical(item_state_sql()), params={"as_of": as_of})


def launch_seed_sql() -> str:
    """Latest curated launch assumption per (tcin, month, basis) snapshotted on or before `@as_of`."""
    return """
-- shipcast.channels.target.signals.launch_seed
-- RAW read of biom_admin.seed_target_launch_velocity: this is a biom_sql-owned seed, not
-- a BPD feed, so it has no logical-table body and needs none.
--
-- AS-OF FILTERED on snapshot_date, for the same reason item_state_live is: the table is
-- APPEND-ONLY, so every past assumption is still in it, and a replay must see the
-- assumption that stood at the origin rather than the one somebody wrote afterwards.
-- That is the whole point of an append-only seed (ddl/admin/006) — without this
-- predicate the archive would exist and be unused.
--
-- Newest snapshot wins per (tcin, period_start, basis); source_row breaks a tie within a
-- snapshot so the reduction is a TOTAL order and cannot return a different row run to
-- run (the lesson from the orders_daily QUALIFY, bq.LOGICAL_TABLES).
SELECT tcin, period_start, LOWER(basis) AS basis, units, author, rationale, confidence,
       snapshot_date
FROM `biom-reporting-s26.biom_admin.seed_target_launch_velocity`
WHERE snapshot_date <= @as_of
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY tcin, period_start, LOWER(basis)
  ORDER BY snapshot_date DESC, source_row DESC
) = 1
"""


def launch_seed(as_of: date, *, run: QueryFn | None = None) -> pd.DataFrame:
    """Curated launch velocity / load-order assumptions as they stood at `as_of`.

    Returns an EMPTY frame with the right columns when the table does not exist yet, so
    a deploy that has not run `ddl/admin/006_seed_target_launch_velocity.sql` degrades to
    "no curated assumptions" rather than failing the whole run. Any other error raises.
    """
    cols = [
        "tcin",
        "period_start",
        "basis",
        "units",
        "author",
        "rationale",
        "confidence",
        "snapshot_date",
    ]
    try:
        return _runner(run)(launch_seed_sql(), params={"as_of": as_of})
    except Exception as e:  # noqa: BLE001 - only a missing table is tolerated
        if "Not found: Table" in str(e) or "404" in str(e):
            log.warning("launch seed table absent; continuing with no curated assumptions")
            return pd.DataFrame(columns=cols)
        raise


BM_SCHEDULE_TABLE = f"{_PROJECT}.biom_admin.bm_target_schedule_snapshot"


def bm_schedule_sql() -> str:
    """Every row of the newest Brick & Mortar Target Schedule snapshot on or before `@as_of`."""
    return f"""
-- shipcast.channels.target.signals.bm_schedule
-- RAW read of biom_admin.bm_target_schedule_snapshot: written by
-- ingest/bm_schedule_ingest.py from the channel owner's Drive sheet, not a BPD feed, so it
-- has no logical-table body and needs none.
--
-- AS-OF FILTERED on snapshot_date, like launch_seed: the table is APPEND-ONLY, one
-- snapshot per edit of the sheet, and a replay must see the store plan that stood at its
-- origin, not the one written afterwards. Unlike launch_seed the reduction is WHOLE
-- SNAPSHOT, not per key: a plan is one coherent edit of the sheet, and mixing months from
-- two edits would splice two plans. source_modified_time breaks a same-day tie.
SELECT snapshot_date, source_file_id, source_name, source_modified_time, loaded_at,
       source_row, bm_sku, unique_key, description, tcin, month_start,
       bm_stores, bm_upspw, bm_velocity, bm_load_orders, bm_quote, bm_total_demand,
       bm_revenue, bm_placeholder
FROM `{BM_SCHEDULE_TABLE}`
WHERE snapshot_date = (
  SELECT MAX(snapshot_date) FROM `{BM_SCHEDULE_TABLE}` WHERE snapshot_date <= @as_of
)
QUALIFY source_modified_time = MAX(source_modified_time) OVER ()
"""


def bm_schedule_asof(as_of: date, *, run: QueryFn | None = None) -> pd.DataFrame:
    """The B&M Target Schedule snapshot that stood at `as_of`, one row per SKU block x month.

    Returns an EMPTY frame with the snapshot columns when the table does not exist yet
    (`ddl/bm_target_schedule_snapshot.sql` not run) or holds no snapshot on or before
    `as_of`. The caller turns "empty" into the BM_SCHEDULE_NOT_AVAILABLE exception and
    runs on BPD alone; any other error raises.
    """
    cols = [
        "snapshot_date",
        "source_file_id",
        "source_name",
        "source_modified_time",
        "loaded_at",
        "source_row",
        "bm_sku",
        "unique_key",
        "description",
        "tcin",
        "month_start",
        "bm_stores",
        "bm_upspw",
        "bm_velocity",
        "bm_load_orders",
        "bm_quote",
        "bm_total_demand",
        "bm_revenue",
        "bm_placeholder",
    ]
    try:
        df = _runner(run)(bm_schedule_sql(), params={"as_of": as_of})
    except Exception as e:  # noqa: BLE001 - only a missing table is tolerated
        if "Not found: Table" in str(e) or "404" in str(e):
            log.warning("bm schedule snapshot table absent; continuing on BPD alone")
            return pd.DataFrame(columns=cols)
        raise
    return df if not df.empty else pd.DataFrame(columns=cols)


ALL_SQL: dict[str, Callable[[], str]] = {
    "plan_snapshot": plan_snapshot_sql,
    "plan_business_dates": plan_business_dates_sql,
    "orders_latest": orders_latest_sql,
    "orders_line_count": orders_line_count_sql,
    "sales_weekly": sales_weekly_sql,
    "inventory_weekly": inventory_weekly_sql,
    "dfe_asof": dfe_asof_sql,
    "item_state_live": item_state_sql,
    "launch_seed": launch_seed_sql,
    "bm_schedule": bm_schedule_sql,
}
"""Every SQL builder, for the test that checks each names its logical body."""


def describe() -> dict[str, Any]:
    """Which statements are logical (CTE-injected) and which are raw as-of exceptions."""
    return {
        "logical": [
            "orders_latest",
            "orders_line_count",
            "sales_weekly",
            "inventory_weekly",
            "item_state_live",
        ],
        "raw_asof_exceptions": ["plan_snapshot", "plan_business_dates", "dfe_asof"],
        "admin_snapshots": ["launch_seed", "bm_schedule"],
    }
