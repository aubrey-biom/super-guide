-- Channel owner's Brick & Mortar Master Forecast, Target Schedule tab, as append-only
-- snapshots. Written by pipelines/target_shipment_forecast/ingest/bm_schedule_ingest.py,
-- read as-of by channels/target/signals.py::bm_schedule_asof. One row per SKU block x
-- month per snapshot; a snapshot is one edit of the sheet (keyed by Drive modifiedTime).
CREATE TABLE IF NOT EXISTS `biom-reporting-s26.biom_admin.bm_target_schedule_snapshot` (
  snapshot_date        DATE      NOT NULL OPTIONS(description="date of the sheet edit this snapshot captures (Drive modifiedTime), never the load date"),
  source_file_id       STRING             OPTIONS(description="Drive file id, or local:<name> for a hand-run load"),
  source_name          STRING,
  source_modified_time TIMESTAMP          OPTIONS(description="Drive modifiedTime; the idempotency key"),
  loaded_at            TIMESTAMP NOT NULL,
  source_row           INT64              OPTIONS(description="1-based row of the block's Stores line in the tab"),
  bm_sku               STRING    NOT NULL,
  unique_key           STRING    NOT NULL OPTIONS(description="the tab's Unique Key column; unique per block within a snapshot"),
  description          STRING,
  tcin                 INT64              OPTIONS(description="resolved at ingest via the item master; NULL when unresolved"),
  month_start          DATE      NOT NULL,
  bm_stores            FLOAT64,
  bm_upspw             FLOAT64,
  bm_velocity          FLOAT64            OPTIONS(description="Stores x UPSPW x days/7 as the sheet states it"),
  bm_load_orders       FLOAT64            OPTIONS(description="launch fills and pipeline loads; a cross-check, never a source"),
  bm_quote             FLOAT64,
  bm_total_demand      FLOAT64            OPTIONS(description="Velocity + Load_Orders; the parser aborts if this identity fails"),
  bm_revenue           FLOAT64,
  bm_placeholder       BOOL               OPTIONS(description="Stores == 1 wherever live: the sheet's placeholder shape; its ramp is refused")
)
PARTITION BY snapshot_date
CLUSTER BY unique_key, month_start
OPTIONS (
  description = "Brick & Mortar Master Forecast (Target Schedule) snapshots. Append-only. The forecast engine reads the newest snapshot on or before its as-of date and uses the store plan as a ratio to the anchor month, never as a level."
);
