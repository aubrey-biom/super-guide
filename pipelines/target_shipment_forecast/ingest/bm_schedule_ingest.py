"""Land the Brick & Mortar Master Forecast (Target Schedule tab) in BigQuery as snapshots.

    python -m pipelines.target_shipment_forecast.ingest.bm_schedule_ingest            # Drive
    python -m pipelines.target_shipment_forecast.ingest.bm_schedule_ingest --dry-run
    python -m pipelines.target_shipment_forecast.ingest.bm_schedule_ingest --local /path/to/sheet.xlsx

WHY A SEPARATE JOB. The forecast engine reads BigQuery only, so a scheduled run can never
depend on a file somebody placed by hand. The sheet lives on Google Drive and is edited by
the channel owner, so this job -- and only this job -- talks to Drive: it downloads the
workbook as the service account it runs as, parses the tab with the strict parser in
`inputs/bm_master_forecast.py` (which aborts rather than guess), and appends one snapshot
to `biom_admin.bm_target_schedule_snapshot`. The engine then reads the newest snapshot on
or before its as-of date (`channels/target/signals.py::bm_schedule_asof`), which is what
lets a replay see the plan that stood at the time. Same pattern as the RDZ inventory
pipeline.

ACCESS. The Drive grant on the sheet is one-way to the service account that runs this job
(`biom-data-pipeline@biom-reporting-s26.iam.gserviceaccount.com` in production); the
engine's read-only credential has no Drive scope and needs none. The file id is NOT
committed: pass `--file-id` or set `BM_SCHEDULE_FILE_ID`. The job needs
`bigquery.dataEditor` on `biom_admin` and Viewer on the file, and nothing else.

IDEMPOTENT. A snapshot is keyed by the source file's `modifiedTime`; re-running against an
unchanged sheet loads nothing (pass `--force` to load anyway). `snapshot_date` is the
modifiedTime's date, never today(), so two edits on one day still land as one snapshot
per edit and a replay picks the newest that existed at its origin.

DEPENDENCIES. `requirements-ingest.txt`, not the engine's `requirements.txt`: the Drive
client is deliberately kept out of the forecast image.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from pipelines.target_shipment_forecast import bq
from pipelines.target_shipment_forecast.inputs.bm_master_forecast import (
    SNAPSHOT_COLUMNS,
    BmForecast,
    BmParseError,
    parse_target_schedule,
    snapshot_rows,
)

TABLE = f"{bq.PROJECT}.biom_admin.bm_target_schedule_snapshot"
FILE_ID_ENV = "BM_SCHEDULE_FILE_ID"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"

# BigQuery schema, kept in step with ddl/bm_target_schedule_snapshot.sql. Given explicitly
# on load so pandas datetime64 columns land as DATE / TIMESTAMP rather than all TIMESTAMP.
SCHEMA: tuple[tuple[str, str], ...] = (
    ("snapshot_date", "DATE"),
    ("source_file_id", "STRING"),
    ("source_name", "STRING"),
    ("source_modified_time", "TIMESTAMP"),
    ("loaded_at", "TIMESTAMP"),
    ("source_row", "INT64"),
    ("bm_sku", "STRING"),
    ("unique_key", "STRING"),
    ("description", "STRING"),
    ("tcin", "INT64"),
    ("month_start", "DATE"),
    ("bm_stores", "FLOAT64"),
    ("bm_upspw", "FLOAT64"),
    ("bm_velocity", "FLOAT64"),
    ("bm_load_orders", "FLOAT64"),
    ("bm_quote", "FLOAT64"),
    ("bm_total_demand", "FLOAT64"),
    ("bm_revenue", "FLOAT64"),
    ("bm_placeholder", "BOOL"),
)
assert tuple(c for c, _ in SCHEMA) == tuple(SNAPSHOT_COLUMNS)


class IngestError(RuntimeError):
    """The job could not complete; the message says what to fix."""


# --------------------------------------------------------------------------------------
# Drive
# --------------------------------------------------------------------------------------


def _drive_credentials() -> Any:
    """Service-account credentials with the Drive read-only scope, via the same
    resolution `bq` uses (GOOGLE_APPLICATION_CREDENTIALS, GCP_SA_KEY_B64, then ADC)."""
    path, _label = bq.resolve_credentials()
    if path is not None:
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_file(
            str(path), scopes=[DRIVE_SCOPE]
        )
    import google.auth

    creds, _project = google.auth.default(scopes=[DRIVE_SCOPE])
    return creds


def fetch_drive_file(file_id: str, *, credentials: Any = None) -> tuple[bytes, dict[str, Any]]:
    """Download the workbook bytes and its metadata (id, name, mimeType, modifiedTime).

    A native .xlsx on Drive is downloaded as-is; a Google Sheet is exported to .xlsx. The
    Drive client is imported here, not at module top, so the engine never needs it.
    """
    from googleapiclient.discovery import build

    svc = build(
        "drive", "v3", credentials=credentials or _drive_credentials(), cache_discovery=False
    )
    meta = (
        svc.files()
        .get(fileId=file_id, fields="id,name,mimeType,modifiedTime", supportsAllDrives=True)
        .execute()
    )
    if meta.get("mimeType") == GOOGLE_SHEET:
        data = svc.files().export(fileId=file_id, mimeType=XLSX).execute()
    else:
        data = svc.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
    if not data:
        raise IngestError(f"Drive returned no bytes for {file_id}")
    return bytes(data), dict(meta)


def _parse_modified(meta: Mapping[str, Any]) -> datetime:
    raw = str(meta.get("modifiedTime") or "")
    if not raw:
        raise IngestError("Drive metadata carries no modifiedTime; refusing to stamp a snapshot")
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


# --------------------------------------------------------------------------------------
# BigQuery
# --------------------------------------------------------------------------------------


def latest_loaded(client: Any, table: str = TABLE) -> dict[str, Any] | None:
    """The newest snapshot already in the table, or None when the table is empty/absent."""
    sql = f"""
SELECT snapshot_date, source_modified_time, COUNT(*) AS rows_
FROM `{table}`
GROUP BY 1, 2
ORDER BY snapshot_date DESC, source_modified_time DESC
LIMIT 1"""
    try:
        df = client.query(sql).result().to_dataframe()
    except Exception as e:  # noqa: BLE001 - only a missing table is tolerated
        if "Not found: Table" in str(e) or "404" in str(e):
            return None
        raise
    if df.empty:
        return None
    r = df.iloc[0]
    return {
        "snapshot_date": pd.Timestamp(r["snapshot_date"]).date(),
        "source_modified_time": pd.Timestamp(r["source_modified_time"]).to_pydatetime(),
        "rows": int(r["rows_"]),
    }


def load_snapshot(client: Any, rows: pd.DataFrame, *, table: str = TABLE) -> int:
    """Append `rows` (from `snapshot_rows`) to the snapshot table. Returns rows loaded."""
    from google.cloud import bigquery

    schema = [bigquery.SchemaField(n, t) for n, t in SCHEMA]
    df = rows.copy()
    for c in ("snapshot_date", "month_start"):
        df[c] = pd.to_datetime(df[c]).dt.date
    job = client.load_table_from_dataframe(
        df,
        table,
        job_config=bigquery.LoadJobConfig(schema=schema, write_disposition="WRITE_APPEND"),
    )
    job.result()
    return int(len(df))


# --------------------------------------------------------------------------------------
# the job
# --------------------------------------------------------------------------------------


def ingest(
    *,
    file_id: str | None = None,
    local: Path | None = None,
    table: str = TABLE,
    dry_run: bool = False,
    force: bool = False,
    snapshot_date: date | None = None,
) -> dict[str, Any]:
    """Fetch (or read), parse, and append one snapshot. Returns a summary dict.

    `local` bypasses Drive for a file already on disk (its mtime stands in for Drive's
    modifiedTime); everything else is identical, including the idempotency check.
    """
    if local is not None:
        p = Path(local)
        if not p.exists():
            raise IngestError(f"{p} does not exist")
        data = p.read_bytes()
        modified = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        meta = {
            "id": f"local:{p.name}",
            "name": p.name,
            "mimeType": XLSX,
            "modifiedTime": modified.isoformat(),
        }
    else:
        fid = file_id or os.environ.get(FILE_ID_ENV)
        if not fid:
            raise IngestError(f"no file id: pass --file-id or set {FILE_ID_ENV}")
        data, meta = fetch_drive_file(fid)
        modified = _parse_modified(meta)

    snap_date = snapshot_date or modified.date()
    # The parser wants a path; keep the bytes in a temp file only for as long as it needs.
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        bm: BmForecast = parse_target_schedule(tmp.name, snapshot_date=snap_date)

    loaded_at = datetime.now(timezone.utc)
    rows = snapshot_rows(
        bm,
        snapshot_date=snap_date,
        source_file_id=str(meta.get("id", "")),
        source_name=str(meta.get("name", "")),
        source_modified_time=modified,
        loaded_at=loaded_at,
    )
    summary: dict[str, Any] = {
        "source_name": meta.get("name"),
        "source_file_id": meta.get("id"),
        "source_modified_time": modified.isoformat(),
        "snapshot_date": snap_date.isoformat(),
        "sku_blocks": int(rows["unique_key"].nunique()),
        "months": len(bm.months),
        "rows": int(len(rows)),
        "tcins_resolved": len(bm.tcins),
        "skus_unresolved": [u["bm_sku"] for u in bm.unresolved],
        "warnings": list(bm.warnings),
        "table": table,
        "loaded": 0,
        "skipped_reason": None,
    }
    if dry_run:
        summary["skipped_reason"] = "dry run"
        return summary

    client = bq.client()
    prior = latest_loaded(client, table)
    if prior is not None and not force and prior["source_modified_time"] >= modified:
        summary["skipped_reason"] = (
            f"snapshot for source_modified_time {prior['source_modified_time'].isoformat()} "
            "already loaded; pass --force to load again"
        )
        return summary
    summary["loaded"] = load_snapshot(client, rows, table=table)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--file-id", default=None, help=f"Drive file id of the .xlsx (default ${FILE_ID_ENV})"
    )
    ap.add_argument(
        "--local", default=None, help="parse a local .xlsx instead of fetching from Drive"
    )
    ap.add_argument("--table", default=TABLE, help="destination table")
    ap.add_argument(
        "--snapshot-date",
        default=None,
        help="override YYYY-MM-DD (default: the file's modified date)",
    )
    ap.add_argument("--dry-run", action="store_true", help="parse and report; load nothing")
    ap.add_argument(
        "--force",
        action="store_true",
        help="load even if this modifiedTime is already in the table",
    )
    a = ap.parse_args(argv)
    try:
        summary = ingest(
            file_id=a.file_id,
            local=Path(a.local) if a.local else None,
            table=a.table,
            dry_run=a.dry_run,
            force=a.force,
            snapshot_date=date.fromisoformat(a.snapshot_date) if a.snapshot_date else None,
        )
    except (BmParseError, IngestError) as e:
        print(f"ABORT: {e}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
