---
name: po-forecast
description: SUPERSEDED 2026-09-08 — kept as the record of the upstream skill. Its Drive/RDZ/owner-forecast steps no longer exist; see the note below.
---

> ⚠️ **SUPERSEDED 2026-09-08 — DO NOT FOLLOW THESE STEPS.** This runbook tells you to
> export the RDZ sheet and the owner forecast from Google Drive into `inputs/`. Both
> inputs, their parsers and the whole Drive path were **deleted**: the monthly POS
> forecast is `model.consumption.dist_velocity` (BPD selling stores x units per selling
> store per week), `planned_launch` comes from Target's own PO plan plus the live
> `wkly_tcin_item` item state, and curated human assumptions live in
> `biom_admin.seed_target_launch_velocity`. **The current run is
> `pull` then `run`, with no `--rdz`, no `--owner` and no Drive step:**
>
> ```
> python -m pipelines.target_shipment_forecast.cli check
> python -m pipelines.target_shipment_forecast.cli pull --as-of <as_of>
> python -m pipelines.target_shipment_forecast.cli run  --as-of <as_of>
> ```
>
> Kept unedited below as the record of the upstream skill. See
> `scratchpad/drive_residuals_removed.md`.


# /po-forecast [channel] [as_of]

Runs shipcast for one channel (v1: `target`) as of a date (default today).

## Environment

| Variable | Purpose |
|---|---|
| `GCP_SA_KEY_B64` (or `GOOGLE_APPLICATION_CREDENTIALS`) | read-only BigQuery service account for `biom-reporting-s26`; materialised to `~/.config/gcloud/biom-bq-sa.json` (0600) on first use, never printed |
| Slack MCP (`mcp__Slack__slack_send_message`) | optional digest |

The bullseye GitHub access and the Google Drive MCP rows were removed: bullseye's CTEs
were inlined into `bq.LOGICAL_TABLES` on 2026-09-07, and every spreadsheet input went on
2026-09-08. A BigQuery credential is the whole requirement.

Nothing writes to BigQuery. Do not paste credential values into the transcript.

## Procedure

1. **Install.** A python >= 3.11 venv with
   `pipelines/target_shipment_forecast/requirements.txt`. There is no private dependency
   and no optional extra to fetch.
2. **Inputs.** None to place. Every input is a BigQuery read, including the curated
   launch assumptions (`biom_admin.seed_target_launch_velocity`, pulled as
   `launch_seed`). Step 2 used to export three spreadsheets from Drive; all three were
   removed on 2026-09-08.
3. **Check.** `python -m pipelines.target_shipment_forecast.cli check` must print PASS
   for `bigquery.session_user`, `orders.dedup` (~7.8k lines) and
   `plan.one_business_d`. A FAIL stops the run. (The `rdz.parse` probe is gone with the
   sheet — `check` has no non-BigQuery probe left.)
4. **Pull + run.**
   ```bash
   python -m pipelines.target_shipment_forecast.cli pull --as-of <as_of>
   python -m pipelines.target_shipment_forecast.cli run  --channel target --as-of <as_of>
   ```
   Report the output verbatim instead of improvising numbers.
5. **Upload** `runs/<as_of>/target_shipment_forecast_<as_of>.xlsx` wherever the team
   agrees (there is no longer a Drive folder tied to a source sheet), and keep the
   `manifest.json` next to it.
6. **Digest** (Slack, same channel `crstl-po-alert` posts to): chain PO units
   for the next 4 weeks by item group, items flagged SUPPLY_SHORT / SUPPLY_UNKNOWN,
   last week's ex-ante WAPE, and the as-of of every source (plan BUSINESS_D,
   orders SNAPSHOT_D, RDZ banner, owner sheet modifiedTime). If any source is
   stale (`STALE_PLAN`, `SUPPLY_STALE`) say so first.

## Rules

* Never read `bpd_raw.dly_po_plan_tcin` unfiltered; `shipcast pull` filters to
  specific BUSINESS_D values.
* Never sum `tbd` / `po_placed` inbound into supply; they are upside only.
* Every threshold you quote must cite its `basis` from `config/target.yaml`.
