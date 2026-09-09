# biom-fork-2026-09-09 — read before diffing against `claude/sales-forecast-shipment-tool-lw0x5w`

This branch is a **snapshot of `pipelines/target_shipment_forecast/`** as it stands in the
`biom_sql` monorepo (`santush-biom/biom-sql`, `main`, commit `e35126a`), where this code was
forked to on 2026-09-08 (`64e0e6c`) and has been developed independently since. **Not squashed
into one commit for provenance reasons** — see "History" below; this is a **single new commit**
against your `main`, carrying the current file state, so it can be reviewed as one diff.

**Nothing on your repo was touched to make this.** `main` and
`claude/sales-forecast-shipment-tool-lw0x5w` are exactly as they were; this is a new branch only.

## What changed since the fork (2026-09-08 → 2026-09-09), in commit order

| Commit (biom_sql) | What |
|---|---|
| `64e0e6c` | Forked from your branch; fixed 6 audit findings; added a TCIN↔SKU mapping seed + BigQuery dimension (`biom_canvas.dim_target_tcin`) |
| `b999a42` | Replaced `owner_velocity` (the Brick & Mortar sheet's POS blend candidate) with `dist_velocity` — a BPD-measured store-count × units-per-selling-store-per-week decomposition. Retired the blend entirely (single estimator, not a weighted average). Live-measured WAPE 0.1887 vs `owner_velocity`'s published 0.3422 |
| `179977a` | Removed the last Drive/RDZ code paths — `inputs/drive_fetch.py`, the owner-sheet parser, the RDZ Google Sheets puller. **The pipeline is now BigQuery-only**; RDZ read from a local file if present, feeding Exceptions only |
| `8b740c7` | Deploy scaffolding for Cloud Run (D-3) — **lives outside this directory**, see "Not included" below |
| `e35126a` | Re-integrated the Brick & Mortar Master Forecast (`inputs/bm_master_forecast.py`, `model/bm_combine.py`) as a **local-file-only, guarded parser** — no Drive dependency reintroduced. Combines B&M's stated store-count plan (shape) with `dist_velocity`'s BPD-measured level, never letting the sheet override a measured number. Also live RDZ inventory ingestion (separate biom_sql pipeline, not in this directory) |

## Structural differences from this branch's own layout — flagged, not resolved

**Import paths.** Every module in this snapshot imports as `pipelines.target_shipment_forecast.*`
(49 absolute imports, zero relative), matching its location inside the `biom_sql` monorepo. Your
`claude/sales-forecast-shipment-tool-lw0x5w` branch uses `src/shipcast/*` as an installable
package. **These are incompatible as-is** — dropping this snapshot into `src/shipcast/` would
break every import. Reconciling them means either rewriting ~49 import lines here, or restructuring
your branch to the `pipelines.target_shipment_forecast` path. Neither was done; this is pushed
as a faithful copy of the biom_sql state, not adapted to your package layout.

**No `bullseye` / `bpd_mcp` dependency.** Your branch imports `bpd_mcp.bq.build()` from
`aubrey-biom/bullseye` (the private BPD MCP server) for its logical-table SQL and de-dup
`QUALIFY` handling. This snapshot queries `bpd_raw` directly — `bq.py`'s logical-table SQL bodies
were **copied verbatim from bullseye's registry** at fork time (see `bq.py`'s module docstring)
and are maintained independently since. `bullseye` appears only in code comments now, documenting
where each query body originated — there is no runtime import of it anywhere in this snapshot.

**`config.py` vs `config/`.** This snapshot has both a `config.py` module (the loader) and a
`config/` directory (`target.yaml`, `report_target.yaml`) at the same path level. Your `main`-tree
layout appears to keep `config/` at repo root, separate from `src/shipcast/`; this snapshot nests
both under `pipelines/target_shipment_forecast/`, matching its monorepo location.

## Deploy scaffolding — added in a follow-up push (same branch, second commit)

The three **portable** deploy files from `8b740c7` are now included, unmodified:

- `Dockerfile.target_shipment_forecast` (root)
- `cloudbuild.target_shipment_forecast.yaml` (root)
- `runners/run_target_shipment_forecast.py` (root-level `runners/`)

**Nothing has actually been deployed from these** — no image has been built from this Dockerfile,
no Cloud Run job or scheduler exists for it anywhere. They describe an intended deploy, sized and
written but never executed.

### 🔴 `biom-reporting-s26` (BIOM's own GCP project) is hardcoded in two of the three — you will need to adapt these before deploying in your own environment

```
cloudbuild.target_shipment_forecast.yaml:10   us-central1-docker.pkg.dev/biom-reporting-s26/biom-containers/target-shipment-forecast
cloudbuild.target_shipment_forecast.yaml:13   (same, in the `images:` block)

runners/run_target_shipment_forecast.py:40    PROJECT_ID = "biom-reporting-s26"
runners/run_target_shipment_forecast.py:43    RUN_LOG_TABLE = f"{PROJECT_ID}.biom_monitoring.shipcast_run_log"
runners/run_target_shipment_forecast.py:44    PIPELINE_STATE_TABLE = f"{PROJECT_ID}.biom_admin.pipeline_state"
```

`biom_monitoring.shipcast_run_log` and `biom_admin.pipeline_state` are BIOM-side BigQuery tables
that do not exist in your project (`pipeline_state` in particular is a cross-pipeline watermark
table biom_sql's whole platform writes to — not something a single deploy creates on its own). The
runner also writes to a GCS bucket via a `--bucket` CLI arg (not hardcoded, but the bucket itself
is BIOM's and won't exist for you either) and runs as `biom-data-pipeline@` — a BIOM service
account with write access to those specific tables and bucket (see the runner's own module
docstring, line 25).

**`Dockerfile.target_shipment_forecast` itself has no project-specific hardcoding** — it's plain
`COPY`/`pip install` instructions relative to the build context, and the Artifact Registry
push target lives only in `cloudbuild.yaml`. So the adaptation needed, in order:

1. `cloudbuild.target_shipment_forecast.yaml` — swap the Artifact Registry path (project ID +
   region, if different) on both lines.
2. `runners/run_target_shipment_forecast.py` — swap `PROJECT_ID`, and either create equivalent
   `biom_monitoring.shipcast_run_log` / `biom_admin.pipeline_state`-shaped tables in your own
   project or point the two constants at wherever you want run history and pipeline-state
   watermarks recorded (the runner's `_write_run_log` / `_write_pipeline_state` functions are the
   only two places that write to them — self-contained, not spread through the file).
3. Wire up a service account with `bigquery.dataEditor` on those tables and write access to
   whichever GCS bucket you pass as `--bucket`, or strip the upload/record steps if you just want
   the forecast run locally.

No config values inside `pipelines/target_shipment_forecast/config/*.yaml` reference
`biom-reporting-s26` — that project ID only appears in these deploy-layer files, not in the
forecasting logic itself.

## Not included — still lives at the biom_sql repo root, not pushed

**Monitoring integration**: `ddl/monitoring/008_shipcast_run_log.sql`, plus additions inside
`pipelines/monitoring/biomcheck_alert_job.py` and `scripts/biomcheck.py`. These are
biom_sql-platform-wide files — `biomcheck` covers dozens of unrelated pipelines — so pushing them
here would pull in context that has nothing to do with Shipcast. `008_shipcast_run_log.sql` is the
DDL for the `biom_monitoring.shipcast_run_log` table the runner writes to above; if you want the
exact schema rather than inferring it from the runner's `_write_run_log`, say so and I'll push that
one DDL file (not the biomcheck additions, which are genuinely BIOM-platform-specific).
