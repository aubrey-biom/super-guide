# shipcast design record

> **Revision note (2026-09-04).** The v1 horizon design below was revised after
> this text was written:
>
> * a **monthly consumption layer** was added for S&OP (expected shipments =
>   POS forecast for the month + the on-hand drawdown toward the steady-state
>   WOS band; see `shipcast.model.consumption`);
> * **supplier POs are out of scope** for v1 (the freight tracker is parsed for
>   the record only; `inputs/inbound_manual.csv` is not a v1 input);
> * the **owner input is the "Brick & Mortar Master Forecast" workbook, Target
>   schedule tab** (structure TBD; registration point documented in
>   `shipcast.inputs.owner_forecast`), replacing the Retail Inventory Forecast
>   / S&OP house formats named in section 4.
>
> Numbers quoted below were measured on 2026-09-03/04 against the backtest
> panel now committed under `tests/fixtures/`. The text is otherwise the
> synthesis as written; where it disagrees with `config/target.yaml` or a
> module docstring, the code wins and this note should be extended.

---

# Target PO / Shipment Forecast (`shipcast`) — unified v0 build plan

**For:** Aubrey (Biom) · **Date:** 2026-09-03 · **Scope today:** Target. Amazon 1P, DTC, other retail follow through the same adapter seam.

## 1. How I would attack this

Target already tells us what it will order. Its daily PO plan (`bpd_raw.dly_po_plan_tcin.ORDERED_Q`, Saturday snapshot, summed over the 29 receiving DCs) predicts next week's replenishment PO units per TCIN with WAPE 0.168, bias -1.0%, Spearman 0.93, median APE 6.8%, and exact matches on 31.5% of TCIN-weeks (69.9% within 10%). Nothing else is close: the DFE forecast is Pearson 0.13-0.17 with +98% bias and the feed has been dead since 2026-07-27; trailing POS is Pearson 0.20-0.33 with +35-41% bias; store/DC on-hand and WOS are |r| < 0.22; the "cover the forecast" replenishment identity has LOWO R² 0.015-0.026 with a wrong-signed on-purchase coefficient. Fitting anything on top of the plan makes it worse (plan-only OLS LOWO WAPE 0.368 vs 0.168 as-is) because an intercept blurs the exact matches. So v0 is not a demand model. It is a **plan-anchored PO forecaster** with measured fallbacks, a separate booked forward-PO stream (34% of units, unpredictable at create time), a single-count Biom supply ledger, empirical intervals, and an accuracy sheet that re-derives its own weights every run and leads with how wrong last week's published forecast was. The channel owner's monthly sell-in plan is ingested verbatim, scored against Jun-Aug actuals, used as reconciliation inside the plan horizon and as the only signal beyond it. Every threshold lives in config with a `basis` field that is either a measured number or the words "operational choice, not measured".

## 2. Repo decision and reuse

**New repo `aubrey-biom/shipcast`, depending on bullseye as a library.** Bullseye is a read-only FastMCP server with a tight cost/safety contract and no pandas/openpyxl; a batch forecaster with sheet parsers and a backtest loop would double its dependency surface and couple two release cadences. fastidious-lion is prompt-only by design; a tested model cannot live in a SKILL.md. vigilant-engine is the Shopify connector and becomes the DTC `actuals` source later, not the home.

Reuse, not re-derivation. Verified in `/home/user/bullseye/src/bpd_mcp/bq.py`: `build(sql, registry)` at line 1052 is a public, pure function that injects the registry CTEs, with no server import. **No bullseye PR is needed.** shipcast writes `SELECT ... FROM orders_daily / po_plan_daily / po_plan_biweekly / sales_daily / inventory_daily / item_attr` and inherits the full orders QUALIFY (SNAPSHOT_D DESC, ITEM_RECEIVED_Q DESC, CANCEL_REMAINING_ORDER_Q DESC, REVISED_ORDER_Q DESC, ORIGINAL_ORDER_Q DESC, TO_JSON_STRING), week anchors and `latest_state_note` semantics from one place (150,175 raw rows → 7,846 latest-state lines). Two deliberate exceptions, because bullseye's tables are latest-state and would leak in a backtest: (a) DFE is read from `bpd_raw.dfe_wkly_item_loc_forecast` with `LAST_UPDATE_D < w`, not from `forecast_weekly` (whose QUALIFY keeps only the newest snapshot per tcin/location/week); (b) any as-of open-PO position used at a backtest origin is computed from `daily_order_tcin_loc` snapshot history with MAX per line within snapshot, not from `orders_daily`. Later, shipcast's as-of SQL can be registered back into bullseye as `LogicalTable` entries via the documented `depends_on` pattern so Target de-dup fixes flow in one direction. From fastidious-lion we reuse `item-aliases.tsv` (extended, not forked) and the Slack posting pattern of `crstl-po-alert` for the digest.

## 3. The model

### 3.1 Target variable and streams
`y[t,w]` = revised PO units for TCIN `t` created in Sunday-anchored week `w`, summed over DCs, from the QUALIFY-reduced order table. Target raises three replenishment POs a week today (Sun D3-C2 cleaning, Mon D253-C4 personal care, Mon D7 baby), with a fourth (Mon D253-C6 flushables) from the 2026-10-11 launch; a TCIN never appears in two replen POs in a week (0 of 7,348 tcin-DC-weeks), so TCIN × week at chain level is the grain Target actually orders at.

Two streams, never pooled:
- **Replenishment** (`ship_begin − create ≤ 14 d`; 67 POs; steady state since 2026-05-17 mean 22,541 units/wk, CV 0.277): forecast.
- **Forward/launch** (`> 14 d`; 17 POs, 272,682 units = 34%; 231,948 units open for Sept-Nov ship): **never forecast**. Open lines are taken from the PO feed, placed in their ship week, stream `BOOKED`. Pooled metrics including them collapse (plan-only LOWO R² 0.114 vs 0.379), so the headline accuracy is replenishment-only with forward reported separately.

Pipeline-fill weeks 2026-04-26..05-10 (36-64k/wk) are excluded from every baseline and shown separately.

### 3.2 Signals and measured worth (16 complete weeks, 432 active TCIN-weeks)

| Signal | h=1 WAPE | Pearson | Bias | v0 role |
|---|---|---|---|---|
| Plan `ORDERED_Q`, Sat snapshot | 0.168 | 0.83 | -1% | Primary, weight 1.0, no rescaling |
| Plan, Thu snapshot | 0.303 | 0.76 | -2.5% | Fallback if Sat file not landed |
| Bi-weekly plan | 0.389 | 0.69 | +2.2% | Fallback, h=1 only (unmeasured at h≥2) |
| Naive 0.5·lag1_rep + 0.5·mean4_rep | ~0.80 | 0.31-0.32 | — | Fallback when plan=0, scaled by p0 |
| Trailing POS 1w/4w/8w | ~1.0 | 0.20-0.33 | +35-41% | Weight 0 by gate; context + flag |
| DFE forecast | 1.39 | 0.13-0.17 | +98% | Weight 0 by gate; context only |
| Inventory / WOS / DC position | ~1 | |r|<0.22 | — | Never a predictor; risk flag |
| Owner monthly sell-in | not yet scored | — | — | Reconciliation ≤ plan horizon; level beyond it |

**Admission gate (replaces "Pearson ≥ 0.25", which sales_1w at 0.327 would have passed):** a signal is admitted at horizon `h` only if its backtest WAPE is below the naive benchmark at that horizon **and** |bias| < 15%. Today that admits the plan variants and nothing else; POS fails on bias, DFE on both. The gate result per signal per horizon is printed in the Accuracy sheet every run, so when DFE resumes or POS bias shrinks, a signal earns its way in without a code change. Weights among admitted signals are `w_s ∝ 1/WAPE_s²`, renormalised over signals actually present for the row; a missing signal is never imputed as zero into a blend.

### 3.3 Formulas by horizon
Origin `o` = the Saturday before PO week `W0`; `h` = weeks ahead.

**h=1.**
```
if plan_W[t] > 0:            y_hat = plan_W[t]                         grade A (core) / B (intermittent)
elif PO in trailing 4 wks:   y_hat = p0 * (0.5*lag1_rep + 0.5*mean4_rep) grade C, flag PLAN_ZERO_HISTORY_POSITIVE
else:                        y_hat = 0                                   grade C, flag NO_SIGNAL
```
`p0` = backtested probability that Target orders when the plan says zero and the TCIN had a PO in the trailing 4 weeks, estimated from the panel (the 7 plan=0 misses cost 24,160 units; the 6 plan>0 no-orders cost 13,784). `p0` and its n are printed; nothing is asserted. Grade A is defined as **all** plan>0 rows including the plan>0/actual=0 rows, and its WAPE is computed on that set; the 0.079 both-positive figure is reported as a secondary line, never as the grade's error. Core = ordered ≥ 80% of eligible weeks (e.g. 93197979 22/22, 94979717 20/22); intermittent = the rest (baby kits ~50%).

If the week's PO already exists in the latest order snapshot at run time (a Monday run sees Sunday's D3-C2 PO), the row becomes stream `CREATED` with the actual units; a report, not a forecast, and labelled so.

**h=2..8** (plan `ORDER_D` runs to 2026-10-31). The plan's chain total holds far ahead (chain WAPE 0.12-0.20, bias within ±13% at every h) while TCIN timing decays (TCIN WAPE 0.52 at h=1 → 0.67 at h=8). So forecast the item-group total and allocate:
```
FWD_CANDIDATE[t,h] = plan[t,W0+h] > 3 * trailing-8wk replen mean[t]  AND not explained by a booked forward PO in that ship week
T_g,h      = sum over non-candidate TCINs in group g of plan[t, W0+h]
share[t]   = a_h*share_plan[t] + (1-a_h)*share_hist4[t]
y_hat[t,h] = T_g,h * share[t]                                            grade B (h=2-3) / C (h=4-8)
```
`a_h` is **not** transplanted from level-forecast WAPEs; it is chosen per horizon by leave-one-week-out grid search (0, 0.1, …, 1.0) minimising TCIN-level WAPE of `T·share` against `act_rep`, and printed. FWD_CANDIDATE rows (today: plan spikes of 71k/98k/97k/77k for order weeks 09-27..10-18 vs ~20-25k normal, only partly explained by the 231,948 booked forward units) are excluded from `T` and listed on Forward_POs with a confirm/edit column. The 3× multiplier is config, basis "operational choice".

**h>8 to end of owner horizon (Dec-2026).**
```
y_hat[t,h] = rho[t] * owner_month_units[sku(t), month] * 7/days_in_month     grade D
rho[t]     = sum(actual replen PO units Jun-Aug) / sum(owner sell-in Jun-Aug), shrunk toward the item-group ratio
```
`rho` is computed on Jun-Aug replenishment actuals only (no pipeline fill, no forward POs), per TCIN, shrunk to the group ratio with weight `n_months/(n_months+k)`; `k` is config with basis "operational choice" and no clip is applied unless the observed Jun-Aug ratio distribution justifies one. Owner numbers are flat run-rates (21,000/mo P-60WIP-BAB-FRA, 4,300/mo K-DIS-2BAB-WHI); we do not invent seasonality we cannot observe (sales from 2025-01-11, POs from Apr-2026). No owner row → `mean4_rep`, grade D, flag NO_OWNER_PLAN.

**Inside the plan horizon the owner forecast is reconciliation only** (Owner_Reconciliation sheet, OWNER_GAP flag at |gap|/owner > 25%, basis "operational choice"): there is no evidence it beats a -1%-bias plan. Every ingested owner sheet is archived with its Drive modifiedTime so it accrues its own WAPE; it enters the weight rule only after ~8 scored weeks. An explicit `overrides` tab (`tcin, week_start, override_units, override_reason` — reason mandatory) is honoured verbatim and stamped `OWNER_OVERRIDE`.

**No revision haircut.** Original→revised on replen lines is -3.6% (552,611 → 532,980), but the plan was scored against revised units at -1% bias; multiplying would add ~3.5% downward bias. Documented in README, no multiplier, no "final" column.

### 3.4 Case packs and DC lines
100% of ordered/received quantities are `VENDOR_CASEPACK_Q` multiples (1/3/4/6/12/24; median replen line 48 units = 6 cases). Casepack of record = mode of orders `VENDOR_CASEPACK_Q` (31 TCINs), fallback plan `VENDOR_CASE_PACK_Q` (2), fallback RDZ Units per Case (8); 2 go-packs have none → flag. Point rounds to nearest case, P10/P90 outward. Conflicts P-DIS-BLK (Target 6 vs RDZ 12) and K-DIS-2BAB-PUR (6|4 vs 4) carry CASEPACK_CONFLICT until confirmed. v0 is TCIN-level; v1 allocates to 29 DCs by trailing-8wk DC share within item group (Lugoff 594 4.9%, FCs 1.2-1.9%), largest-remainder rounding to casepack, minimum one case where share > 0.

### 3.5 Ship calendar
Replen PO created Sun/Mon → `ship_begin = create + 5` (Fri/Sat) for D3-C2 and D253-C4, `+7` (Mon) for D7; window always 1 day; ETA = ship_begin + fixed per-DC transit (5 d CA DCs 553/555/593/3806/3856/588; 16 d 551/579/3802; 9-15 d others). Output carries `po_week` and `ship_week`.

### 3.6 Supply ledger (single-count)
Per RDZ item `s`, in ship-week order:
```
supply_s(w)    = physical_onhand_s + sum inbound_s(eta <= ship_date(w) - 3 d, confidence in {dated, po_placed})
                 - non_target_alloc_s - sum_{v<w} expected_ship_s(v)
demand_s(w)    = open Target PO lines shipping in w (replen created-unshipped + booked forward; qty = revised - received - cancel)
                 + forecast PO units for w
expected_ship[t,w] = min(demand[t,w], supply_s(w) allocated pro rata across TCINs sharing s)
```
`physical_onhand = Qty Remaining + Qty Allocated/Pending` (identity Remaining = Received + Adj − Allocated − Shipped holds on 127/127 rows, so Remaining is ATP not physical). Target allocations (75,374 units) are the open Target POs, which appear once as demand in their ship week — nothing is subtracted from supply for them. Non-Target allocations (Amazon 1P 41,787, Grove, Thrive) are subtracted. Kit multipliers: stocked kits 1; K-60WIP-FLU-FRA-3Pk ×3 and P-60WIP-FLU-FRA-2PK ×2 against the shared 12,833-unit P-60WIP-FLU-FRA pool, allocated by demand share. Negative RDZ rows (P-20WIP-SAN-BER-TRV -102,672, P-30WIP-LIT-FRA -30,600, P-30WIP-BAB-FRA -27,000) are Target POs allocated against zero receipts → SUPPLY_UNKNOWN unless an inbound row exists. TBD inbound is shown as upside, never summed. The freight tracker (last PO 3/4/2026, max ETA 5/15, ≥13 of 27 open rows already received, ~250k double-count) is parsed for the record and never summed. Historic fill 64.5% is **not** applied: we cannot separate short-shipping from Target auto-closing lines ~6 d after ETA; it appears as a scenario column `expected_ship_at_hist_fill` and a per-DC fill table (31% at 3857 to 90% at 3804). Coverage flags OK/TIGHT/SHORT at 1.5×/1.0× cumulative need are config, basis "operational choice".

### 3.7 Confidence per row
Intervals are empirical: for each (horizon bucket × core/intermittent class) the backtest stores quantiles of `log(actual + c) − log(forecast + c)`; P10/P90 scale the point, then round to casepack. Nothing is asserted; realised PI80 coverage is reported per bucket. Business wording in the file: "8 weeks in 10, actual PO units land between L and U", with the README stating the bands rest on 16 weeks and are recalibrated every run. Each row carries `grade, stream, primary_signal, fallback_rung, p10, p90, flags`.

### 3.8 Graceful degradation (published in README)

| Missing | Behaviour |
|---|---|
| Saturday plan file | Thursday snapshot; run labelled; A→B |
| Plan snapshot > 4 d old (operational choice) | STALE_PLAN; all A→B |
| Plan = 0 for TCIN | p0·naive, grade C; zero + NO_SIGNAL if no PO in 4 wks |
| Plan horizon exhausted | rho·owner, grade D; NO_OWNER_PLAN → mean4 |
| RDZ banner > 7 d old / unreadable | supply columns blank, SUPPLY_STALE; PO forecast still produced |
| TCIN unmapped (4 today, e.g. P-60WIP-DSN-PIN with 43,968 planned units) | forecast by TCIN, supply blank, Exceptions row |

Every one of the 43 TCINs in the universe appears in the output with a status; no row is silently dropped.

## 4. Inputs

**Owner forecast.** Three formats detected by header, no retyping: the S&OP house format (`Level | SKU | UniqueKey | ChannelRaw | CanonChannel | Category | 2026-01..2026-12 | Notes`), the Retail Inventory Forecast header (`ACCOUNT | SKU | DESCRIPTION | Mon-YY... | TARGET QUOTE`), and a tool-native minimal CSV (`channel, item_key, item_key_type sku|tcin|upc, period YYYY-MM or YYYY-Www, units, unit_type, source, notes`). All melt to `channel, sku, tcin, period_start, forecast_units, unit_cost, source_sheet, as_of`. Aliases resolve via `item-aliases.tsv` plus `data/sku_aliases_target.tsv` (P-LDIS-*, P-SSDIS-*, K-60WIP-AP-COM-3PK → K-60WIP-AP-COM, K-60WIP-DSN-COM → -3PK, row keyed `30` → K-60WIP-BAB-FRA-4PK, P-40WIP-LIT_FRA, K-60WIP-FLU-FRA-3Pk, P-60WIP-FLU-FRA-2PK, tracker P-40WIP-6IN-SAN-STL-4 → PDQ row). Duplicate SKU rows (the two extra Jan-only P-DIS-WHI/P-DIS-EUC rows) are **collapsed with a warning, never summed**. Unresolvable keys go to Exceptions, not dropped. Optional `overrides` tab as in §3.3.

**RDZ Inventory Summary.** Read via the Drive connector into `inputs/`, located by header text (tab names are not exported), unescaped, `[merged]` stripped. Fail-loud checks: row count 100-200 (127 today), banner `Last updated:` present (year from Drive modifiedTime), identity holds ≥ 99%. Emits `on_hand(item, available, physical, allocated, as_of)` and `inbound(shipment_id, item, qty, eta, confidence dated|po_placed|tbd, status)` from the Inbound Arrival Date/Qty columns (4 dated, 1 PO Placed, 6 TBD today); tokens KIT BUILT IN DC / Old Stock / No Replenshiment excluded. The red "Target SKU" cell colour is lost in export → `dim_product.is_in_target` plus the item master. PDQ rows stay separate items pending decision 3. Allocations log is split by Customer to feed `non_target_alloc`.

**Inbound after March 2026.** `inputs/inbound_manual.csv` (`sku, qty, eta, po_ref, confidence`) maintained by Aubrey until DOSS; the freight tracker only seeds candidate rows for confirmation.

**Item master.** `data/item_master_target.csv` (43 TCINs × 36 cols; 37 exact / 2 inferred / 4 unmapped) committed and regenerated by script. TCIN (INT64) is the only join key; `dim_product.tcin` joined with SAFE_CAST; never MANUFACTURER_STYLE / VENDOR_STYLE_ID (`""` placeholder on ~95% of PO lines, 10-char truncations, LIT_FRA underscore, FLU-FRA renamed -2PK on 2026-06-09).

**DOSS seam.** `SupplyAdapter` returns the same two frames; `supply/doss.py` is a stub with one requirement: DOSS SKU ids equal RDZ Item # including PDQ lines.

## 5. Output workbook `target_po_forecast_<as_of>.xlsx` (+ CSVs)

1. **README** — as-of of every source (plan BUSINESS_D and Sat/Thu, orders SNAPSHOT_D, RDZ banner + modifiedTime year, owner sheet + modifiedTime), grade legend, interval wording, derived `a_h`, `p0`, `rho`, gate results, casepack conflicts, degradation table, the 3.6% revision note, and a closing "what this will not do" paragraph (no anticipation of forward POs before creation; TCIN timing beyond one week ~0.5-0.7 WAPE; DFE/WOS not drivers until they pass the gate).
2. **Shipments** — the primary deliverable: TCIN × ship-week pivot in cases and units, streams stacked (forecast replen, created, booked forward), chain total row with P10/P90.
3. **Forecast** (audit tab, TCIN × week, 16 weeks): `po_week, ship_week, item_group, tcin, biom_sku, rdz_item, description, class, casepack, stream, expected_po_units, expected_cases, p10, p90, grade, horizon, primary_signal, fallback_rung, expected_ship_units, expected_ship_at_hist_fill, coverage_flag, flags`.
4. **Signal_Attribution** (long) — per row: plan_sat, plan_thu, plan_biweekly, plan_chain_total, share_plan, share_hist4, lag1_rep, mean4_rep, p0, owner_month_units, rho, dfe_W (context), pos_4w (context), store_wos, dc_on_hand, weight_used, contribution_units, source timestamps. Every point is reproducible by hand.
5. **Supply_Coverage** — per RDZ item: remaining, allocated, physical, non-Target allocations, dated/TBD inbound, cumulative demand by week, weeks of cover, first SHORT week, UNKNOWN reasons, shared-pool and kit-component warnings (K-DIS-2BAB-WHI repack from P-DIS-WHI; no BOM rows for any Target kit).
6. **Forward_POs** — booked lines (PO id, TCIN, units, ship window, ETA, received) and FWD_CANDIDATE plan spikes with confirm/edit column.
7. **Owner_Reconciliation** — month × TCIN: owner vs model vs DFE vs actual-to-date, OWNER_GAP; Jun-Aug owner scoring (27 SKUs × 3 months) with WAPE and bias per SKU.
8. **Accuracy** — leads with the ex-ante block (last N published forecasts vs what Target actually ordered), then the backtest by horizon/group/class with benchmarks (plan-only, naive lag1, mean4, owner/4.33, DFE) in the same table, gate results, `a_h` grid, PI80 coverage, per-DC fill table.
9. **Item_Master**, **Inputs_Snapshot** (owner rows as parsed), **Exceptions** (unmapped TCINs, casepack conflicts, P-60WIP-DSN-PIN, negative RDZ, stale feeds, NO_SIGNAL rows).

Flag vocabulary: `PLAN_ZERO_HISTORY_POSITIVE, NO_SIGNAL, NEW_TCIN_NO_HISTORY, FWD_CANDIDATE, FORWARD_INDICATOR_UNEXPLAINED, SUPPLY_SHORT, SUPPLY_TIGHT, SUPPLY_UNKNOWN, SUPPLY_NEGATIVE_RDZ, INBOUND_TBD_ONLY, CASEPACK_CONFLICT, PDQ_UNRESOLVED, STALE_PLAN, SUPPLY_STALE, OWNER_GAP, OWNER_OVERRIDE, DISCONTINUED_CANDIDATE` (Mint disinfect 94928289, Sani20 94979716; baby D7-C7 collapse to 804/128 units flagged as assortment transition).

## 6. Validation

- **Rolling-origin backtest** over the 16 complete weeks 2026-05-17..2026-08-23, h=1..8, scored against `act_rep`; forward scored separately as passthrough hit rate (did the booked line ship in its predicted week?). Pipeline-fill weeks reported separately.
- **Leakage discipline:** as-of rules fixed in code (plan `BUSINESS_D ≤ o`; sales/inventory ≤ o; DFE `LAST_UPDATE_D < w` from the raw table; order snapshots ≤ o for any as-of position), plus a unit test asserting no feature's source timestamp exceeds `o` for every backtest row. Verified fact used as a test fixture: post-W snapshots show ORDERED_Q = 0 for past order dates.
- **Metrics** at TCIN, item-group and chain level: WAPE (primary), bias, median APE, exact-match %, within-10% %, PI80 coverage. Pooled MAPE deliberately omitted (inflated by small-actual rows).
- **Ex-ante log:** every run appends its forecast to `runs/forecast_log.csv`; the next run scores it against realised POs. After ~8 weeks the live log replaces the backtest as the source of gate results, `a_h`, `p0` and intervals.
- **Fail-loud integrity tests** (pytest live tier, reusing bullseye's byte caps): QUALIFY reduces to ~7.8k lines not 150k; three replen POs found in the latest complete week; plan filtered to one BUSINESS_D; RDZ row count and identity; item master covers every TCIN in plan and orders; casepack non-null on every forecast row.
- **v1:** Crstl 850/856 backfill (5,313 PO-DC docs from 2024-03-15; 5,604 ASNs) extends the backtest from 16 weeks to ~29 months and makes ASN `units_shipped` the shipment truth, settling the 64.5% fill question.

## 7. Architecture and run mechanics

```
shipcast/
  pyproject.toml            # uv, py3.11; deps: bpd-mcp @ git+…/bullseye (pinned), google-cloud-bigquery, pandas, pyarrow, openpyxl, pydantic, typer
  config/target.yaml        # groups, ship offsets, DC transit, every threshold with `basis:`
  src/shipcast/
    channels/base.py        # ChannelAdapter Protocol + declares which tiers it populates
    channels/target/        # signals.py (bpd_mcp.bq.build + raw DFE/snapshot SQL), calendar.py, forward.py, actuals.py
    supply/base.py          # SupplyAdapter Protocol
    supply/rdz_sheet.py, manual_csv.py, doss.py (stub)
    inputs/owner_forecast.py, aliases.py, item_master.py
    model/signals.py, gate.py, blend.py, allocation.py, owner.py, casepack.py, intervals.py, supply_ledger.py
    backtest/rolling.py, scoring.py, leakage.py
    output/workbook.py
    cli.py                  # shipcast run | backtest | score
  data/item_master_target.csv, sku_aliases_target.tsv, dc_share_target.csv
  inputs/ (gitignored), runs/ (gitignored or Drive)
  tests/unit (fixtures from today's scratchpad CSVs), tests/live (BigQuery)
  .claude/skills/po-forecast/SKILL.md
```

```python
class ChannelAdapter(Protocol):
    tiers: frozenset[str]                     # {"retailer_plan","retailer_forecast","pos","naive","owner"}
    def calendar(self) -> ChannelCalendar     # week anchor, PO->ship offsets, DC transit
    def signals(self, as_of: date) -> SignalPanel   # long: item_id, week, signal, value, source_ts
    def actuals(self, start, end) -> Actuals  # replen + forward streams
    def item_master(self) -> ItemMaster

class SupplyAdapter(Protocol):
    def on_hand(self, as_of) -> OnHand        # item, available, physical, allocated_by_customer
    def inbound(self, as_of) -> Inbound       # shipment_id, item, qty, eta, confidence, status
```
Amazon 1P plugs in with ASIN as `item_id`, tiers without `retailer_plan` (so the cascade starts at owner/naive explicitly); DTC uses vigilant-engine's Shopify data as `actuals`; DOSS replaces `rdz_sheet.py`.

**Running.** `/po-forecast target` in a Claude Code cloud session: (1) export the RDZ sheet and the owner forecast via Drive MCP into `inputs/`; (2) `uv run shipcast run --channel target --as-of today`; (3) upload the workbook to the Drive folder; (4) post the Shipments chain totals for the next 4 weeks, SHORT/UNKNOWN items and last week's ex-ante WAPE to Slack. Routine: Monday 07:00 ET (scores last week, forecasts 16 weeks; Sunday's D3-C2 PO appears as CREATED). A Sunday 05:00 ET variant is added once decision 8 tells us the Saturday plan file has landed; until then the Thursday snapshot (WAPE 0.303) is the guaranteed bound and the run labels which it used. Laptop CLI is identical with `gcloud` ADC. Nothing writes to BigQuery.

## 8. Build sequence today (~9 h)

1. **Scaffold (0.75 h).** Repo, uv, bullseye git dep, `bq.py` wrapper around `bpd_mcp.bq.build`, pull logical tables + raw DFE/snapshot SQL to parquet (reuse `build_panel.py`). Test: ~7.8k lines.
2. **Item master + aliases (0.5 h).** Commit `item_master_target.csv`, write `sku_aliases_target.tsv`, resolver tests on the 27 owner SKUs incl. row `30`.
3. **Signal panel + model (2.25 h).** Sunday panel; replen/forward split; gate; h=1 rule with p0; h=2-8 FWD_CANDIDATE exclusion + `a_h` grid; rho owner extension; casepack; log-ratio intervals by class; fallback rungs with attribution.
4. **RDZ + owner parsers + ledger (1.5 h).** Header-text parser per `sheet_specs.md` with three fail-loud checks; three owner formats; overrides tab; `inbound_manual.csv`; single-count supply ledger with shared FLU-FRA pool.
5. **Backtest + Accuracy (1.25 h).** Rolling origin, leakage test, metrics, benchmarks table, gate output, PI80 coverage, Jun-Aug owner scoring.
6. **Workbook (1 h).** openpyxl, Shipments pivot first, then audit sheets; README auto-filled from run metadata.
7. **Skill + Routine + first run (0.75 h).** SKILL.md, Monday Routine, live run for week 2026-09-06, sanity check against week 08-30 (19,802 units already created).
8. **Buffer (1 h)** for the profilers' known surprises (LAV style placeholder, week 08-30 partial, go-packs without casepack).

**v0 today:** TCIN × week, 16 weeks, grades + intervals, supply flags, forward passthrough, Shipments/Accuracy/Attribution sheets, slash command, Monday Routine.
**v1 (1-2 weeks):** DC-line split with per-DC casepack rounding and ETA; ex-ante log driving weights; Crstl 850/856 backfill to 29 months; Sunday Routine; Slack exceptions digest; re-admit DFE if the feed resumes and passes the gate.
**Later:** Amazon 1P (ASIN) and DTC adapters; other retail via Crstl; DOSS supply adapter; Target kit BOM explosion; pooled/hierarchical model only once ≥ 52 weeks exist and the extended backtest shows plan-as-is leaving lift on the table.

## 9. What Aubrey must provide or decide (default assumed if unanswered)

1. **Repo:** create `aubrey-biom/shipcast` and confirm the cloud environment can install bullseye from GitHub. *Default: I create it under that name in your org.*
2. **Supply basis:** physical (Remaining + Allocated) with all open Target POs counted once as demand. *Default: physical, as specified.*
3. **PDQ rows:** does Target draw P-40WIP-6IN-SAN-STL and P-40WIP-LIT-FRA from the (PDQ) lines (21,717 / 1,872), plain lines, or both? *Default: PDQ only (STORE_SHIPPACK_Q = 4), flag PDQ_UNRESOLVED.*
4. **Casepack of record:** P-DIS-BLK 6 or 12; K-DIS-2BAB-PUR 6 or 4. *Default: Target orders mode (6 / 6), flag CASEPACK_CONFLICT.*
5. **Inbound since March 2026:** fill `inbound_manual.csv` (sku, qty, eta, po_ref); ETAs or "unknown" for the three negative-balance SKUs and the 6 TBD rows. *Default: only the 4 dated + 1 PO Placed RDZ rows count; everything else SUPPLY_UNKNOWN.*
6. **P-60WIP-DSN-PIN:** 43,968 planned units, no RDZ item, no dim_product row, no PO — real launch or artifact? *Default: forecast from plan, supply UNKNOWN, Exceptions row.*
7. **Authoritative owner sheet:** live Retail Inventory Forecast or Signals for Demand Plan_03_30 (drifted, e.g. DSN-CIT 38,700 vs 20,000). *Default: Retail Inventory Forecast tab 0.*
8. **Plan-file timing:** when does Saturday's `dly_po_plan_tcin` BUSINESS_D land relative to Sunday PO creation? *Default: Monday Routine with Thursday fallback; run labels the snapshot used.*
9. **K-DIS-2FLU-WHI BOM** (assumed P-DIS-WHI ×1 + P-60WIP-FLU-FRA ×2) and the shipping UPC for P-60WIP-FLU-FRA-2PK. *Default: assumed BOM, flagged inferred.*
10. **Drive output folder and Slack channel** for the Routine. *Default: same folder as the RDZ sheet; post to the channel `crstl-po-alert` uses.*

Everything else in the profilers' risk lists is handled by flags, not blocked on you.

## Repo recommendation
New repo aubrey-biom/shipcast, depending on bullseye as a pinned git library. Import bpd_mcp.bq.build(sql) directly (public, pure, at bq.py line 1052; no bullseye PR needed) to inherit the orders QUALIFY, week anchors and latest_state_note semantics for orders_daily / po_plan_daily / po_plan_biweekly / sales_daily / inventory_daily / item_attr. Read DFE from bpd_raw.dfe_wkly_item_loc_forecast with LAST_UPDATE_D < w and as-of open-PO positions from daily_order_tcin_loc snapshot history rather than bullseye's latest-state forecast_weekly / orders_daily, to avoid backtest leakage; later register shipcast's as-of SQL back into bullseye as LogicalTable entries via depends_on. Reuse fastidious-lion's item-aliases.tsv (extended with sku_aliases_target.tsv) and its Slack routine pattern; vigilant-engine becomes the DTC actuals adapter later, not the home. Not bullseye (read-only MCP with strict cost contract, no pandas/openpyxl, Target-only), not fastidious-lion (prompt-only), not vigilant-engine (Shopify connector).

## Decisions needed
- Create aubrey-biom/shipcast (or bless another name) and confirm the Claude Code cloud environment can install bullseye from GitHub via uv. Default: create it under that name.
- Supply basis: RDZ physical on-hand (Qty Remaining + Qty Allocated) with every open Target PO counted once as demand in its ship week, vs plain ATP (Qty Remaining). Default: physical.
- PDQ rows: does Target draw P-40WIP-6IN-SAN-STL and P-40WIP-LIT-FRA from the '(PDQ)' RDZ lines (21,717 / 1,872), the plain lines, or both summed? Default: PDQ only (STORE_SHIPPACK_Q=4), rows flagged PDQ_UNRESOLVED.
- Casepack of record for P-DIS-BLK (Target 6 vs RDZ 12) and K-DIS-2BAB-PUR (Target 6|4 vs RDZ 4). Default: mode of Target orders VENDOR_CASEPACK_Q, flagged CASEPACK_CONFLICT.
- Fill inputs/inbound_manual.csv (sku, qty, eta, po_ref, confidence) with every supplier PO placed since March 2026, and give ETAs or 'unknown' for P-20WIP-SAN-BER-TRV (-102,672), P-30WIP-LIT-FRA (-30,600), P-30WIP-BAB-FRA (-27,000) and the 6 TBD rows. Default: only the 4 dated + 1 'PO Placed' RDZ inbound rows count as supply; everything else SUPPLY_UNKNOWN.
- P-60WIP-DSN-PIN: 43,968 planned units in dly_po_plan, no RDZ item, no dim_product row, no PO. Real launch or plan artifact? Default: forecast from plan, supply UNKNOWN, listed on Exceptions.
- Which owner sheet is authoritative: the live Retail Inventory Forecast or Signals for Demand Plan_03_30 (drifted, e.g. P-60WIP-DSN-CIT 38,700 vs 20,000 Jun-Dec)? Default: Retail Inventory Forecast tab 0.
- When does the Saturday dly_po_plan_tcin BUSINESS_D file land in BigQuery relative to Sunday/Monday PO creation? Determines Sunday vs Monday Routine and whether grade A rests on WAPE 0.17 or 0.30. Default: Monday 07:00 ET Routine with Thursday-snapshot fallback; run labels the snapshot used.
- Confirm K-DIS-2FLU-WHI BOM (assumed P-DIS-WHI x1 + P-60WIP-FLU-FRA x2) and which of the three barcodes (850056298926 / 850056298483 / 850078481818) ships for P-60WIP-FLU-FRA-2PK. Default: assumed BOM, mapping flagged inferred.
- Drive output folder for the workbook and Slack channel for the Routine digest. Default: same Drive folder as the RDZ sheet; same Slack channel crstl-po-alert posts to.