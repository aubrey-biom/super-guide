# Backtest inputs

- `plan_hist_agg_2026-09-02.parquet` — Target's daily PO plan (`bpd_raw.dly_po_plan_tcin`), every
  snapshot 2026-05-07..2026-09-02 (119 BUSINESS_D values), aggregated to
  `business_d, tcin, order_d, plan_units, n_dc` (229,680 rows). Lets the plan be scored at every
  lead day and horizon. Regenerate with one GROUP BY over the raw table (~150 MB scanned).
- `rdz_target_shipments_2026-08-28.parquet` — RDZ Shipments Log rows with customer Target from the
  RDZ Inventory Tracking workbook (.xlsx export), 2026-01-29..2026-08-25: `ship_date, customer,
  reference (PO-DC), item, qty, po, dc`. Realised shipments by item and day; reconciles to Target PO
  lines at 98.5% shipped/ordered on 5,247 matched PO-DC-item lines (Target's own receipt field on the
  same lines shows 63.5%).

Results computed from these on 2026-09-04 are in `docs/backtest/2026-09-04/` with the script
(`backtest_full.py`) that produced them.
