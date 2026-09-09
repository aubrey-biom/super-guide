"""shipcast: demand and shipment forecaster for Biom retail channels (v1: Target).

Turns Target's own replenishment signals (BigQuery project biom-reporting-s26,
read through the logical tables in `bq.LOGICAL_TABLES`) into expected PO units by TCIN by fiscal
week, graded by measured accuracy, plus a monthly consumption view for S&OP.

Every input is a warehouse read. There is no spreadsheet, no Google Drive call and no
manually placed file anywhere in `check | pull | run`: the monthly POS forecast is
`model.consumption.dist_velocity` (BPD selling stores x units per selling store per week),
`planned_launch` comes from Target's own PO plan plus the live `wkly_tcin_item` item state,
and the only curated human assumptions live in `biom_admin.seed_target_launch_velocity`.
The ability-to-ship layer that used to read Biom's RDZ inventory sheet was removed on
2026-09-08 along with the sheet; see scratchpad/drive_residuals_removed.md.
"""

__version__ = "0.1.0"
