> **biom_sql fork, 2026-09-07.** This package was copied from
> `aubrey-biom/super-guide` @ `claude/sales-forecast-shipment-tool-lw0x5w` and is now
> owned by `biom_sql` independently — it is a COPY, not a shared fork, and nothing is
> pushed back upstream. Two things changed on arrival:
>
> 1. **The bullseye (`bpd-mcp`) dependency is gone.** Credential resolution and
>    logical-table CTE injection are local in `bq.py`; the four CTE bodies were copied
>    verbatim (they carry the `orders_daily` de-dup QUALIFY and the canvas ∪ raw unions,
>    which a plain `bpd_raw` read would lose — see that module's docstring).
> 2. **Six defects found in the 4 Sep audit are fixed or explicitly deferred.** See
>    `scratchpad/shipcast_migration_and_fixes.md` in the repo root.
>
> Paths below that say "repo root" now mean this package directory
> (`pipelines/target_shipment_forecast/`), which is what `config.repo_root()` returns.

# shipcast

Demand and shipment forecaster for Biom (plant-based wipes and refillable
dispensers). **v1 scope: the Target retail channel.** It turns Target's own
replenishment signals (BigQuery project `biom-reporting-s26`, read through the logical
tables inlined in `bq.LOGICAL_TABLES`) into expected PO units by TCIN by fiscal week,
graded by measured accuracy, plus a monthly consumption-based view for S&OP.

Every input is a warehouse read. The bullseye dependency was inlined on 2026-09-07 and
the last spreadsheet inputs were removed on 2026-09-08, so a run needs nothing but a
BigQuery credential.

Design record: [`docs/PLAN.md`](docs/PLAN.md). Every threshold lives in
[`config/target.yaml`](config/target.yaml) with a `basis:` that is either a
measured number with its date or the literal "operational choice, not measured".

## Status

**8 Sep 2026 — v1 demand side is live and input-complete from BigQuery alone.**
`shipcast run` produces the workbook end to end from a BigQuery pull and nothing else.
First live run (upstream, when the spreadsheets were still inputs):
`docs/samples/2026-09-04/`. ⚠️ That sample therefore predates `dist_velocity` and the
stream split — its POS numbers and its `planned_forward` labels are not what the code
now produces. Not deployed anywhere: see Deployment.

Implemented:
- Weekly plan-anchored PO forecast (`model/plan_anchor.py`): Target's daily PO plan used
  as-is from the freshest snapshot before each order day, graded A/B/C by lead days,
  bands fitted leave-one-week-out on the backtest panel (realised P10-P90 coverage 0.79
  against an 80% target), booked launch POs passed through, plan spikes shown as a
  separate `planned_forward` stream.
- Monthly consumption model for S&OP (`model/consumption.py`): the POS forecast is
  **`dist_velocity`** — units per SELLING store per week × the store count projected
  forward on its own ramp, i.e. Target's own `Stores × UPSPW = Velocity` identity
  estimated from BPD. **One estimator, no blend** (the two candidates' errors correlate
  0.686, so `1/WAPE²` weighting scored worse than the single one: 0.2035 vs 0.1998).
  An inventory drawdown controller calibrated by grid search each run. A month-of-year
  index that admits a month only on ≥ 2 distinct calendar years and prints its `n_years`.
- Config-driven workbook (`config/report_target.yaml`): Monthly grid with a grade grid
  beneath, Monthly detail, weekly Shipments, Weekly detail, Accuracy, Booked forward,
  Exceptions. Change the layout by editing the YAML.
- **BigQuery is the only input.** No Google Drive call, no spreadsheet and no manually
  placed file anywhere in `check | pull | run`. The owner forecast, the RDZ Inventory
  Summary, the Inbound Freight Tracker and every Drive code path were removed on
  2026-09-08; curated human assumptions, when any exist, come from
  `biom_admin.seed_target_launch_velocity` (append-only, as-of read, always graded E).

Not yet: **Biom-side ability-to-ship is not modelled at all** — no forecast cell is capped
by what Biom can ship. That was already true in v1 (the RDZ read only ever produced an
Exceptions row) and the modules went with the sheet. Also outstanding: DC-line split,
Crstl backfill for a longer monthly backtest, empirical MONTHLY prediction bands (the
monthly grid still uses the symmetric grade-legend %), Slack digest formatting, and a
deploy path (no Dockerfile, no Cloud Run job, no scheduler, no `biomcheck` entry).

## Quickstart

```bash
# In biom_sql: python >= 3.11 (the code uses zip(strict=True)); the repo default python3 is 3.9.
python3.12 -m venv .venv && .venv/bin/pip install -r pipelines/target_shipment_forecast/requirements.txt pytest
export GCP_SA_KEY_B64=...                   # or GOOGLE_APPLICATION_CREDENTIALS=/path/key.json, or gcloud ADC
P=pipelines.target_shipment_forecast.cli
.venv/bin/python -m $P check                # BigQuery as SESSION_USER, de-dup ~7.8k lines, one BUSINESS_D
.venv/bin/python -m $P pull --as-of 2026-09-03   # signals -> runs/2026-09-03/*.parquet + manifest.json
.venv/bin/python -m $P run  --channel target --as-of 2026-09-03
.venv/bin/python -m $P backtest && .venv/bin/python -m $P score
.venv/bin/python -m pytest pipelines/target_shipment_forecast/tests -q -m "not bq_live"   # pure-python tier
.venv/bin/python -m pytest pipelines/target_shipment_forecast/tests -q -m bq_live         # bills bytes; skipped without a credential
```

### Credentials

`GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json`, or
`GCP_SA_KEY_B64=<base64 of the JSON>` which is materialised once to
`~/.config/gcloud/biom-bq-sa.json` (mode 0600) by bullseye's
`resolve_credentials`. The key is never logged or committed; `.gitignore`
blocks the materialised path. The service account is read-only
(`bigquery.dataViewer` + `bigquery.jobUser`). Every job is dry-run first and
refused above `bq.max_bytes_billed` (2 GiB) before a byte is billed.

### The bullseye dependency (private repo)

`pyproject.toml` pins `bpd-mcp` to a bullseye commit via `[tool.uv.sources]`.
Three ways to satisfy it:

* **Local development** against a checkout: `uv add --editable /path/to/bullseye`
  (or `uv pip install -e /path/to/bullseye` into the venv).
* **GitHub Actions**: a deploy key or a fine-grained PAT with read access to
  `aubrey-biom/bullseye` stored as the `BULLSEYE_TOKEN` secret, then
  `git config --global url."https://x-access-token:${BULLSEYE_TOKEN}@github.com/".insteadOf "https://github.com/"`
  before `uv sync`. See `.github/workflows/forecast.yml`.
* **Claude Code cloud sessions**: the environment's GitHub credentials cover
  it when the bullseye repo is enabled for the workspace.

`uv.lock` is not committed (mirrors bullseye). Commit it if you want CI to be
byte-for-byte reproducible.

## What is reused, not re-derived

* **bullseye logical tables** via `bpd_mcp.bq.build` (public, pure): shipcast
  SQL is written against `orders_daily`, `sales_weekly`, `inventory_weekly`, ...
  and inherits the `orders_daily` QUALIFY (~150k accumulated snapshot rows →
  ~7.8k latest-state lines), the weekly canvas ∪ raw unions, and bullseye's
  "Target schema quirks" (Saturday week-end dates, `""` NULL placeholders,
  Sunday-anchored DFE weeks). Every SQL string in
  `src/shipcast/channels/target/signals.py` names the bullseye body it relies on.
* **Two raw as-of exceptions**, because bullseye's tables are latest-state and
  would leak in a backtest: the PO plan is read from
  `bpd_raw.dly_po_plan_tcin` filtered to specific `BUSINESS_D` values (never
  unfiltered: 5.8M accumulating rows), and DFE from
  `bpd_raw.dfe_wkly_item_loc_forecast` filtered to `LAST_UPDATE_D <= as_of`
  before the newest-snapshot QUALIFY.
* **Item aliases** from fastidious-lion's `crstl-po-alert` skill
  (`data/item_aliases_upstream.tsv`, copied with its origin commit) plus
  Target spellings in `data/sku_aliases_target.tsv`.
* **Item master** `data/item_master_target.csv` (43 TCINs; TCIN is the only
  join key).

## Layout

```
config/target.yaml                 thresholds with basis
data/                              item master, alias maps
src/shipcast/bq.py                 credentials, client, cost-gated query, logical()
src/shipcast/channels/base.py      ChannelAdapter protocol
src/shipcast/channels/target/      calendar.py signals.py forward.py
src/shipcast/inputs/               aliases.py item_master.py
src/shipcast/model/                plan_anchor gate grade consumption intervals casepack
src/shipcast/backtest/             rolling.py scoring.py leakage.py
src/shipcast/output/workbook.py    openpyxl writer
src/shipcast/cli.py                shipcast check | pull | run | backtest | score
tests/                             unit tier + bq_live tier; fixtures = today's backtest panel
.github/workflows/forecast.yml     Monday 12:00 UTC + manual dispatch
.claude/skills/po-forecast/        how to run in a Claude Code cloud session
docs/PLAN.md                       design record
```

## Inputs

Every input is a BigQuery read, pulled by `shipcast pull` into
`runs/<as_of>/*.parquet` with a manifest. There is nothing else — no file to place, no
sheet to export, no Drive scope on the identity that runs this.

* **Pulled signals**: plan snapshots (`dly_po_plan_tcin`, filtered to explicit
  `BUSINESS_D` values), latest-state orders, weekly PO actuals split
  replenishment/forward, weekly sales **with the selling-store count** (which is
  `dist_velocity`'s denominator), weekly inventory, DFE as-of, live `item_state`
  (`wkly_tcin_item`), and `launch_seed`.
* **`launch_seed`** — `biom_admin.seed_target_launch_velocity`, read AS-OF
  `snapshot_date` so a replay sees the assumption that stood at the origin. Two bases:
  `velocity` (monthly POS, used ONLY where `dist_velocity` has no row, never overriding a
  measured number) and `load_orders` (launch volume Target's own plan does not carry yet,
  netted against booked-forward and the planned launch/forward streams). Both graded E.
  Empty is the normal state.
* **Committed reference data** (`data/`): the 43-TCIN item master and the SKU alias maps.
  Hand-maintained, in the repo, not fetched — the `rdz_item` / `rdz_base_qty_multiplier`
  columns there are Biom SKU identity and casepack provenance, and have nothing to do
  with the removed RDZ sheet.

**Removed 2026-09-08** (recover from git if ever needed): the owner forecast parser, the
RDZ Inventory Summary parser, the Inbound Freight Tracker parser, the supply-ledger stub
and every Drive fetch/export module. See `scratchpad/drive_residuals_removed.md`.

## Graceful degradation

| Missing | Behaviour |
|---|---|
| Plan snapshot > 4 d old | `STALE_PLAN`, grade A → B |
| Plan = 0 for a TCIN | naive fallback, grade C; `NO_SIGNAL` if no PO in 4 weeks |
| TCIN unmapped | forecast by TCIN, blank SKU label, `UNMAPPED_ITEM` Exceptions row |
| TCIN has never sold anywhere | **no monthly velocity at all, never a zero** — `NEW_TCIN_NO_POS_HISTORY`; the plan still drives its weeks |
| TCIN has history but no open selling store | `POS_NO_SELLING_STORES` — a drawdown or a delist, told apart from a new item on purpose |
| Weekly POS feed > 14 d stale | `POS_FEED_STALE`; the monthly view is single-sourced on that feed, so every monthly number is suspect |
| A month-of-year has < 2 calendar years of history | the seasonal factor is exactly 1.000 and `SEASON_INDEX_UNSUPPORTED` names the month with its `(n, n_years)` |

No row is silently dropped: every TCIN in the universe appears with a status.

## Deployment

**There is none yet.** No Dockerfile, no `cloudbuild.*.yaml`, no Cloud Run job, no Cloud
Scheduler entry, and no registration in `scripts/biomcheck.py` /
`pipelines/monitoring/biomcheck_alert_job.py`. It runs from a venv only.

The upstream GitHub Actions runner (`.github/workflows/forecast.yml`, Monday 12:00 UTC)
was deliberately not migrated: `biom_sql` schedules through Cloud Scheduler → Cloud Run.
When it is built, the identity needs **BigQuery read only** — `dataViewer` + `jobUser`.
No Drive scope, no sheet sharing, no Shared Drive for output: that whole setup went away
with the spreadsheets on 2026-09-08.
