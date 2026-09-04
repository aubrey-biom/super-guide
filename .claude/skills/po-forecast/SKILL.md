---
name: po-forecast
description: Run the shipcast Target PO / shipment forecast in a Claude Code cloud session — pull the RDZ sheet and owner forecast from Drive into inputs/, run `uv run shipcast run`, upload the workbook, post the digest.
---

# /po-forecast [channel] [as_of]

Runs shipcast for one channel (v1: `target`) as of a date (default today).

## Environment

| Variable | Purpose |
|---|---|
| `GCP_SA_KEY_B64` (or `GOOGLE_APPLICATION_CREDENTIALS`) | read-only BigQuery service account for `biom-reporting-s26`; materialised to `~/.config/gcloud/biom-bq-sa.json` (0600) on first use, never printed |
| GitHub access to `aubrey-biom/bullseye` | `uv sync` fetches the pinned `bpd-mcp` dependency |
| Google Drive MCP (`mcp__Google_Drive__*`) | to export the RDZ sheet and the owner forecast into `inputs/` |
| Slack MCP (`mcp__Slack__slack_send_message`) | optional digest |

Nothing writes to BigQuery. Do not paste credential values into the transcript.

## Procedure

1. **Install.** `uv sync --extra dev` in the repo root. If bullseye cannot be
   fetched, stop and report — do not vendor it.
2. **Inputs via Drive MCP** into `inputs/` (gitignored):
   * RDZ 3PL Inventory sheet → `inputs/RDZ.txt` (plain-text export; the parser finds
     the Inventory Summary by header text) AND `inputs/rdz_inventory.xlsx` via
     `mcp__Google_Drive__download_file_content` with
     `exportMimeType=application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`
     (base64 in the result; decode to a file). The text export truncates long tabs.
   * Inbound Freight Tracker → `inputs/inbound_freight_tracker.xlsx` the same way
     (sheet id 1va-c_gGmH6DwDrqqE7oy28xmjVlcDk5R70Kq7AiKpdI). Never the text export.
   * Brick & Mortar Master Forecast → `inputs/bm_master_forecast.txt` (the raw
     Drive export string; the parser takes the "Target Schedule" tab and ignores
     "TARGET WORST CASE (DO NOT USE)").
3. **Check.** `uv run shipcast check --rdz inputs/RDZ.txt` must print PASS for
   `bigquery.session_user`, `orders.dedup` (~7.8k lines), `plan.one_business_d`
   and `rdz.parse`. A FAIL stops the run.
4. **Pull + run.**
   ```bash
   uv run shipcast pull --as-of <as_of>
   uv run shipcast run --channel target --as-of <as_of> --rdz inputs/RDZ.txt --owner inputs/bm_master_forecast.txt
   ```
   Exit code 2 means model stages are still stubs; the message lists them.
   Report that verbatim instead of improvising numbers.
5. **Upload** `runs/<as_of>/target_po_forecast_<as_of>.xlsx` to the Drive folder
   that holds the RDZ sheet (`mcp__Google_Drive__create_file`), and keep the
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
