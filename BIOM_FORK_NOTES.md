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

## Not included in this push — lives outside `pipelines/target_shipment_forecast/`

The instruction that produced this branch was scoped to that one directory. Two things named in
biom_sql's own commit history are **not here**, and are flagged rather than silently included or
silently omitted:

- **Deploy scaffolding** (`8b740c7`): `Dockerfile.target_shipment_forecast`,
  `cloudbuild.target_shipment_forecast.yaml`, `runners/run_target_shipment_forecast.py`,
  `.gcloudignore` — all at `biom_sql` repo root, self-contained to this pipeline. Nothing has
  actually been deployed from them yet (no image built, no Cloud Run job created).
- **Monitoring integration** (also `8b740c7`): `ddl/monitoring/008_shipcast_run_log.sql`,
  additions to `pipelines/monitoring/biomcheck_alert_job.py` and `scripts/biomcheck.py` — these
  are biom_sql-platform-wide files (biomcheck covers dozens of pipelines), not portable to a
  single-purpose repo without pulling in unrelated context.

Say the word if you want either bundled in on a follow-up push.
