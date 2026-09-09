# Test fixtures

All CSVs are the backtest panel built on 2026-09-03 from BigQuery
(`biom-reporting-s26`) via the scratchpad scripts `pull.py` / `build_panel.py` /
`eval_signals.py`. Weeks (`wk`) are Sunday labels; every feature column was
computed as-of the Saturday before `wk` (plan) or the last available date before
`wk` (DFE, inventory, sales), so the panel is leakage-free by construction and
`tests/test_leakage.py` asserts it.

## `signal_panel.csv` (578 rows = 17 weeks x 34 TCINs, 2026-05-10..2026-08-30)

| Columns | Meaning |
|---|---|
| `wk, tcin` | Sunday PO week, Target item id |
| `act_all, act_rep, act_fwd, act_orig_all` | revised PO units created in `wk`: all / replenishment (ship lag <= 14 d) / forward (> 14 d); `act_orig_all` = original units |
| `n_po, n_dc, n_fwd_lines, week_complete` | PO count, receiving DC count, forward lines; `week_complete` False for the partial week 2026-08-30 |
| `plan_sat_{ordered,sched,demand}_{W,W1,W2}, plan_sat_asof` | daily PO plan (`dly_po_plan_tcin`) from the freshest BUSINESS_D <= Saturday before `wk`: ORDERED_Q / SCHEDULED_RECEIPT_Q / NET_STORE_MEAN_DEMAND_Q for order weeks W, W+1, W+2; `plan_sat_asof` = that BUSINESS_D |
| `plan_thu_*` | same from the freshest BUSINESS_D <= Thursday before `wk` |
| `bw_ordered_W, bw_sched_W, bw_demand_W, bw_cost_W, bw_ordered_W1, bw_init_store_inv, bw_lead_time, bw_asof` | bi-weekly PO plan (`bi_weekly_po_planning_item_dc`) as of the last BUSINESS_D before `wk` |
| `dfe_W, dfe_W1, dfe_W2, dfe_Wm1, dfe_asof, dfe_age_days, dfe_L1..L3` | DFE forecast (`dfe_wkly_item_loc_forecast`) chain units for weeks W, W+1, W+2, W-1 from the last LAST_UPDATE_D before `wk`; `L1..L3` cumulative |
| `sales_1w, sales_4w, sales_8w` | trailing POS units ending the week before `wk` |
| `inv_on_hand, inv_on_purchase, inv_on_transfer, stores_with_inv, store_*, instock_pct, oos_pct, dc_on_hand, dc_on_purchase, dcs_with_inv, inv_asof` | chain / store / DC inventory position on the last inventory date before `wk` |
| `wos_4w, dc_wos_4w` | weeks of supply = on hand / (sales_4w / 4) |
| `lag1_all, lag1_rep, mean4_all, mean4_rep, hist8_po_units` | naive benchmarks from PO actuals of earlier weeks |
| `short_chain_L*, short_dc_L*, short_dc_L*_pos, dc_pos, chain_pos` | replenishment-identity features (DFE minus position) |
| `active, sku, product_title` | `active` = PO in trailing 8 weeks or POS in trailing 4; dim_product SKU and title |

## `signal_eval.csv` (132 rows)

One row per (sample, predictor, target): `n, n_weeks, pearson, spearman, wape,
wape_scaled_lowo, mape_actual_gt0, median_ape, bias_pct`. Samples are
`A_all_rows`, active-only and both-positive subsets; predictors are the panel
columns above.

## `signal_plan_horizon.csv` (18 rows)

Plan accuracy by horizon: `source` (daily / biweekly), `horizon_weeks`,
`n_target_weeks, n_rows`, `wape_rep, bias_rep, pearson_rep, spearman_rep`
(replenishment stream), `wape_all, bias_all`, `chain_wape_rep` (chain total).

## `po_weekly_by_tcin.csv` (960 rows)

PO units by Sunday `po_week` x `tcin` from the de-duplicated order table:
`po_units_revised, po_units_original, received, cancel_remaining, dcs, dc_list,
pos, po_ids, replen_units, forward_units, min_ship_begin, max_ship_end, min_eta,
sku, cat, sub, vendor_casepack, po_cases_revised, item_group`.

## `rdz_inventory_summary.txt`

The RDZ sheet export (2026-08-31 banner) trimmed to the Inventory Summary
table only: empty header row, alignment row, banner row, header row, 127 item
rows. Used by `tests/test_rdz_parser.py`.

## `bm_target_schedule_export.txt` and `bm_master_target_raw.md` — REMOVED 2026-09-08

The "Brick & Mortar Master Forecast" Google Drive export fixtures were deleted
with the owner-forecast parser they tested (`inputs/owner_forecast.py`,
`inputs/drive_export.py`, `tests/test_bm_master_forecast.py`,
`tests/test_owner_forecast.py`). The monthly POS forecast is now
`consumption.dist_velocity`, derived from `bpd_raw`/`biom_canvas`, and the
`planned_launch` stream comes from Target's own PO plan plus the live item-state
feed. Recover them from git history if the parser is ever needed again:
`git show 64e0e6c -- <path>`.
