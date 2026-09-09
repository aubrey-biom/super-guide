# shipcast

Demand and shipment forecaster for Biom (plant-based wipes and refillable
dispensers). **v1 scope: the Target retail channel.** It turns Target's own
replenishment signals (BigQuery project `biom-reporting-s26`, read through
[bullseye](https://github.com/aubrey-biom/bullseye)'s logical tables) into
expected PO units by TCIN by fiscal week, graded by measured accuracy, plus a
monthly consumption-based view for S&OP and an ability-to-ship layer against
the RDZ inventory sheet (Biom's own distribution center). It runs for anyone with repo access: GitHub
Actions on a schedule, or a Claude Code slash command — not one laptop.

Design record: [`docs/PLAN.md`](docs/PLAN.md). Every threshold lives in
[`config/target.yaml`](config/target.yaml) with a `basis:` that is either a
measured number with its date or the literal "operational choice, not measured".

## Status

**4 Sep 2026 — v1 demand side is live.** `shipcast run` produces the workbook end to end
from a BigQuery pull, the Brick & Mortar Master Forecast (Target Schedule tab) and the
RDZ sheet. First live run: `docs/samples/2026-09-04/`.

Implemented:
- Weekly plan-anchored PO forecast (`model/plan_anchor.py`): Target's daily PO plan used
  as-is from the freshest snapshot before each order day, graded A/B/C by lead days,
  bands fitted leave-one-week-out on the backtest panel (realised P10-P90 coverage 0.79
  against an 80% target), booked launch POs passed through, plan spikes shown as a
  separate `planned_forward` stream.
- Monthly consumption model for S&OP (`model/consumption.py`): POS forecast blended from
  trailing run-rate and the owner's velocity (weights 1/WAPE² re-scored every run), an
  inventory drawdown controller calibrated by grid search each run, owner load orders as
  `planned_launch` net of what Target already booked or plans.
- Config-driven workbook (`config/report_target.yaml`): Monthly grid with a grade grid
  beneath, Monthly detail, weekly Shipments, Weekly detail, Accuracy, Booked forward,
  Exceptions. Change the layout by editing the YAML.
- Adapters: RDZ Inventory Summary (text export), Inbound Freight Tracker (.xlsx export;
  `supply/freight_tracker.py`), Drive export with the service account
  (`inputs/drive_fetch.py`), GitHub Actions runner (`.github/workflows/forecast.yml`).

Not yet (v1.5): the supply ledger / ability-to-ship sheet (`model/supply_ledger.py` stub;
RDZ on-hand and the freight tracker are parsed and surfaced on Exceptions only), DC-line
split, Crstl backfill for a longer monthly backtest, Slack digest formatting.

## Quickstart

```bash
uv sync --extra dev                         # needs access to the private bullseye repo, see below
export GCP_SA_KEY_B64=...                   # or GOOGLE_APPLICATION_CREDENTIALS=/path/key.json
uv run shipcast check --rdz inputs/RDZ.txt  # BigQuery as SESSION_USER, de-dup ~7.8k lines, one BUSINESS_D, RDZ parses
uv run shipcast pull --as-of 2026-09-03     # signals -> runs/2026-09-03/*.parquet + manifest.json
uv run shipcast run  --channel target --as-of 2026-09-03 --rdz inputs/RDZ.txt
uv run shipcast backtest && uv run shipcast score
uv run pytest -q -m "not bq_live"           # pure-python tier
uv run pytest -q -m bq_live                 # live probes; bills bytes; skipped without a credential
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
src/shipcast/supply/               SupplyAdapter, rdz_sheet.py parser, doss.py stub
src/shipcast/inputs/               aliases.py item_master.py owner_forecast.py drive_export.py
src/shipcast/model/                plan_anchor gate grade consumption intervals casepack supply_ledger (stubs)
src/shipcast/backtest/             rolling.py scoring.py leakage.py
src/shipcast/output/workbook.py    openpyxl writer
src/shipcast/cli.py                shipcast check | pull | run | backtest | score
tests/                             unit tier + bq_live tier; fixtures = today's backtest panel
.github/workflows/forecast.yml     Monday 12:00 UTC + manual dispatch
.claude/skills/po-forecast/        how to run in a Claude Code cloud session
docs/PLAN.md                       design record
```

## Inputs

* **RDZ Inventory Summary** (`inputs/RDZ.txt`, Drive export of the RDZ sheet):
  located by header text, markdown unescaped, `[merged]` stripped. Fail-loud:
  100-200 item rows, `Last updated:` banner present, identity
  `Remaining = Received + Adj − Allocated − Shipped` on ≥ 99% of rows. Emits
  `on_hand(item, available, physical, allocated, as_of)` (physical = Remaining
  + Allocated) and `inbound(shipment_id, item, qty, eta, confidence
  dated|po_placed|tbd, status)`; only `dated` counts as supply.
* **Owner forecast**: header-detected (`shipcast.inputs.owner_forecast`).
  Implemented: the tool-native CSV and the "Brick & Mortar Master Forecast"
  Drive export, tab "Target Schedule" (7-row blocks per SKU: Stores, UPSPW,
  Velocity, Load_Orders, Quote, Total_Demand, Revenue; the "TARGET WORST CASE
  (DO NOT USE)" tab is ignored). Duplicates collapse with a warning, never sum;
  unresolved keys are returned separately, never dropped.
* **Pulled signals** (`shipcast pull`): plan snapshots, latest orders, weekly PO
  actuals split replenishment/forward, weekly sales, weekly inventory, DFE as-of.

## Graceful degradation

| Missing | Behaviour |
|---|---|
| Plan snapshot > 4 d old | `STALE_PLAN`, grade A → B |
| Plan = 0 for a TCIN | naive fallback, grade C; `NO_SIGNAL` if no PO in 4 weeks |
| RDZ banner > 7 d old / unreadable | supply columns blank, `SUPPLY_STALE`; PO forecast still produced |
| TCIN unmapped | forecast by TCIN, supply blank, Exceptions row |

No row is silently dropped: every TCIN in the universe appears with a status.

## Shared runner (GitHub Actions) — one-time setup

1. Repository secrets: `GCP_SA_KEY_B64` (base64 of the read-only BigQuery service-account
   JSON), `BULLSEYE_TOKEN` (fine-grained PAT with read access to `aubrey-biom/bullseye`),
   optionally `SLACK_WEBHOOK_URL` (#supply-chain) and `DRIVE_FOLDER_ID`.
2. Let the same service account read the sheets and write the output. On a machine with
   `gcloud` logged in as a project owner:

   ```bash
   gcloud config set project biom-reporting-s26
   gcloud services enable drive.googleapis.com sheets.googleapis.com
   gcloud iam service-accounts describe claude-code-bq-readonly@biom-reporting-s26.iam.gserviceaccount.com
   ```

   Then share, as you would with a person, with
   `claude-code-bq-readonly@biom-reporting-s26.iam.gserviceaccount.com`:
   the RDZ Inventory Tracking sheet (Viewer), the Inbound Freight Tracker (Viewer), the
   Brick & Mortar Master Forecast (Viewer), and the output folder (Editor). No IAM role
   change is needed; Drive access is granted by sharing.

   Service accounts have no Drive storage quota, so the output folder must live in a
   **Shared Drive** (add the account as Content manager) for uploads to succeed. Until
   then the workbook is attached to the Actions run and written by the Claude Code path.
3. Run it: Actions → forecast → Run workflow (or wait for Monday 12:00 UTC).

## Reading the sheets from Drive

Always use the .xlsx export (`download_file_content` with the spreadsheet MIME type in a
Claude Code session; `inputs/drive_fetch.py` in the runner). The plain-text rendering
truncates long tabs: on 2026-09-04 it showed 145 of the freight tracker's 256 rows and
hid every supplier PO placed after March.
