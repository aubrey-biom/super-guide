"""Entrypoint for the Target shipment forecast (Shipcast) — the Cloud Run job's CMD.

Does the whole cycle in ONE process, because a Cloud Run task starts with an empty
filesystem every time:

    1. pull    every signal for `as_of` from BigQuery -> runs/<as_of>/*.parquet
    2. assemble the workbook + CSVs + readme.json          (run.execute_run)
    3. upload  the run directory to gs://<bucket>/shipcast/<as_of>/
    4. record  biom_monitoring.shipcast_run_log + biom_admin.pipeline_state

Before 2026-09-08 this runner did step 2 only and REQUIRED a manifest a previous
invocation had written, so it could not be deployed at all (D-3). Steps 1, 3 and 4 are
what made it deployable; step 1 calls `run.execute_pull`, the same function
`cli.py pull` calls, so the two paths cannot drift.

    python3 runners/run_target_shipment_forecast.py --as-of 2026-09-13
    python3 runners/run_target_shipment_forecast.py --as-of 2026-09-13 --skip-upload
    python3 runners/run_target_shipment_forecast.py --plan-only     # prints the plan, touches nothing

`--as-of` defaults to today (UTC), which is what the scheduler wants: Sunday 09:00 UTC.
The richer interactive interface (`check`, `backtest`, `score`, every option) stays in
`pipelines/target_shipment_forecast/cli.py`.

WRITES. Steps 1-3 are read-only against BigQuery; step 4 writes exactly two rows and no
data table. The job runs as `biom-data-pipeline@`, which has the write access step 4
needs — Shipcast's "read-only" posture was about the laptop credential
(`claude-code-bq-readonly@`), which still cannot do step 4 and does not need to.

Needs python3.11+ (the package uses `X | Y` at runtime inside `isinstance`); every
Dockerfile in this repo is already python:3.12-slim.
"""

import argparse
import json
import os
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ID = "biom-reporting-s26"
PIPELINE_NAME = "target_shipment_forecast"
ENTITY_NAME = "target_forecast_workbook"
RUN_LOG_TABLE = f"{PROJECT_ID}.biom_monitoring.shipcast_run_log"
PIPELINE_STATE_TABLE = f"{PROJECT_ID}.biom_admin.pipeline_state"
DEFAULT_BUCKET = "biom-target-bpd-staging"
GCS_PREFIX = "shipcast"

# Uploaded, in this order. The workbook first so a partial upload still leaves the
# deliverable present rather than only its supporting CSVs.
UPLOAD_GLOBS = ("*.xlsx", "readme.json", "manifest.json", "csv/*.csv")


def _require_backtest_panel() -> Path:
    """Fail LOUD if the committed backtest panel is missing from the image.

    This is the `COPY dml` failure class (Engineering Guide §10.1) applied to this
    pipeline. `run.default_panel()` returns None when
    `tests/fixtures/signal_panel.csv` is absent, and `pipeline.run_forecast` then
    produces NO empirical bands and NO accuracy_* frames at all. The Accuracy sheet's
    block writer `continue`s past a missing frame WITHOUT a note or a cell
    (`output/report.py`), so the workbook renders, the job exits 0, and the grades lose
    their evidence silently. Better to refuse to run.
    """
    from pipelines.target_shipment_forecast.config import repo_root

    panel = repo_root() / "tests" / "fixtures" / "signal_panel.csv"
    if not panel.exists():
        raise SystemExit(
            f"FATAL: backtest panel missing at {panel}.\n"
            "Without it the run silently produces no empirical bands and no Accuracy "
            "blocks. If this is a container, Dockerfile.target_shipment_forecast is "
            "missing its `COPY pipelines/target_shipment_forecast/tests/fixtures/"
            "signal_panel.csv` line."
        )
    return panel


def _upload(run_dir: Path, as_of: date, bucket_name: str) -> str | None:
    """Upload the run directory to gs://<bucket>/shipcast/<as_of>/; return the workbook URI."""
    from google.cloud import storage

    bucket = storage.Client(project=PROJECT_ID).bucket(bucket_name)
    base = f"{GCS_PREFIX}/{as_of.isoformat()}"
    workbook_uri = None
    for pattern in UPLOAD_GLOBS:
        for src in sorted(run_dir.glob(pattern)):
            blob_name = f"{base}/{src.relative_to(run_dir).as_posix()}"
            bucket.blob(blob_name).upload_from_filename(str(src))
            uri = f"gs://{bucket_name}/{blob_name}"
            print(f"uploaded {uri}")
            if src.suffix == ".xlsx":
                workbook_uri = uri
    # Deliberately NOT uploaded: forecast_log.csv. It is an append-only local log whose
    # value is the accumulated history of one machine; uploading it per-run would
    # overwrite the accumulation with whatever this container happened to have.
    return workbook_uri


def _record(client, row: dict) -> None:
    """Append the run-log row and MERGE pipeline_state. Never masks a successful run."""
    errors = client.insert_rows_json(RUN_LOG_TABLE, [row])
    if errors:
        print(f"WARNING: shipcast_run_log insert returned errors: {errors}")

    from google.cloud import bigquery

    client.query(
        f"""
MERGE `{PIPELINE_STATE_TABLE}` T
USING (SELECT
  '{PIPELINE_NAME}'   AS pipeline_name,
  '{ENTITY_NAME}'     AS entity_name,
  @status             AS pipeline_status,
  @row_count          AS last_row_count,
  @run_id             AS last_run_id,
  CURRENT_TIMESTAMP() AS last_successful_run_utc
) S
ON T.pipeline_name = S.pipeline_name AND T.entity_name = S.entity_name
WHEN MATCHED THEN UPDATE SET
  T.pipeline_status         = S.pipeline_status,
  T.last_row_count          = IF(S.last_row_count > 0, S.last_row_count, T.last_row_count),
  T.last_run_id             = S.last_run_id,
  T.last_successful_run_utc = S.last_successful_run_utc
WHEN NOT MATCHED THEN INSERT (
  pipeline_name, entity_name, pipeline_status, last_row_count, last_run_id,
  last_successful_run_utc
) VALUES (
  S.pipeline_name, S.entity_name, S.pipeline_status, S.last_row_count, S.last_run_id,
  S.last_successful_run_utc
)
""",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("status", "STRING", row["status"]),
                bigquery.ScalarQueryParameter("row_count", "INT64", row["weekly_rows"] or 0),
                bigquery.ScalarQueryParameter("run_id", "STRING", row["run_id"]),
            ]
        ),
    ).result()


def main(
    *,
    as_of: date,
    months: int | None = None,
    horizon_weeks: int | None = None,
    weeks_back: int = 110,
    bucket: str = DEFAULT_BUCKET,
    skip_upload: bool = False,
    skip_record: bool = False,
) -> None:
    from pipelines.target_shipment_forecast.config import load_config, repo_root
    from pipelines.target_shipment_forecast.run import execute_pull, execute_run

    _require_backtest_panel()
    cfg = load_config()
    run_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc)
    run_dir = repo_root() / "runs" / as_of.isoformat()
    print(f"run_id {run_id} as_of {as_of} -> {run_dir}")

    row: dict = {
        "run_id": run_id,
        "as_of": as_of.isoformat(),
        "started_at": started.isoformat(),
        "git_sha": os.environ.get("GIT_SHA"),
        "status": "FAILED",
        "weekly_rows": 0,
    }
    client = None
    if not skip_record:
        from google.cloud import bigquery

        client = bigquery.Client(project=PROJECT_ID)

    try:
        manifest = execute_pull(
            as_of=as_of, run_dir=run_dir, weeks_back=weeks_back, snapshots=2
        )
        row["bytes_billed"] = int(manifest.get("bytes_billed_total") or 0)
        print(f"pulled {len(manifest['tables'])} signals, {row['bytes_billed']:,} bytes billed")

        bundle, path = execute_run(
            as_of=as_of,
            run_dir=run_dir,
            horizon_weeks=horizon_weeks or int(cfg["horizon"]["po_weeks"]),
            months=months or int(cfg["horizon"]["months"]),
        )
        for w in bundle.warnings:
            print(f"warning: {w}")

        rm, frames = bundle.readme, bundle.frames
        pos = rm.get("pos_forecast") or {}
        cand = frames.get("accuracy_pos_candidates")
        used = None
        if cand is not None and not cand.empty and "used" in cand:
            hit = cand[cand["used"]]
            used = hit.iloc[0] if not hit.empty else None
        row.update(
            {
                "plan_snapshot_used": _as_date(rm.get("plan_snapshot_used")),
                "sales_last_week_end": _as_date(rm.get("sales_last_week_end")),
                "inventory_last_week_end": _as_date(rm.get("inventory_last_week_end")),
                "pos_age_days": pos.get("age_days"),
                "weekly_rows": int(len(frames.get("weekly", []))),
                "monthly_rows": int(len(frames.get("monthly", []))),
                "exception_rows": int(len(frames.get("exceptions", []))),
                "forecast_units_16wk": int(frames["weekly"]["expected_po_units"].sum())
                if "weekly" in frames and not frames["weekly"].empty
                else 0,
                "pos_estimator": pos.get("estimator"),
                "pos_wape": float(used["wape"]) if used is not None else None,
                "pos_bias": float(used["bias"]) if used is not None else None,
                "season_months_applied": len(pos.get("season_applied_months") or []),
            }
        )
        print(
            f"workbook -> {path}  "
            f"weekly {row['weekly_rows']} monthly {row['monthly_rows']} "
            f"exceptions {row['exception_rows']} units {row['forecast_units_16wk']:,}"
        )

        if not skip_upload:
            row["workbook_gcs_uri"] = _upload(run_dir, as_of, bucket)
            if row["workbook_gcs_uri"] is None:
                raise RuntimeError(f"no .xlsx found to upload in {run_dir}")
        row["status"] = "SUCCESS"
    except Exception as e:
        row["error_message"] = f"{type(e).__name__}: {e}"[:1024]
        print(f"FAILED: {row['error_message']}")
        raise
    finally:
        finished = datetime.now(timezone.utc)
        row["finished_at"] = finished.isoformat()
        row["duration_seconds"] = int((finished - started).total_seconds())
        row["created_at"] = finished.isoformat()
        if client is not None:
            try:
                _record(client, row)
                print(f"recorded {row['status']} in {row['duration_seconds']}s")
            except Exception as e:  # noqa: BLE001 - never let bookkeeping mask the run
                print(f"WARNING: could not record run ({type(e).__name__}: {e})")
        else:
            print(json.dumps(row, indent=2, default=str))


def _as_date(v) -> str | None:
    """README values are ISO strings or the literal 'none'."""
    return v if isinstance(v, str) and v not in ("none", "") else None


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--as-of", default=datetime.now(timezone.utc).date().isoformat())
    p.add_argument("--months", type=int)
    p.add_argument("--horizon-weeks", type=int)
    p.add_argument(
        "--weeks-back",
        type=int,
        default=110,
        help="sales/inventory history window (~2.1 y: the seasonal index needs 2 years)",
    )
    p.add_argument("--bucket", default=DEFAULT_BUCKET)
    p.add_argument("--skip-upload", action="store_true", help="local run: leave output on disk")
    p.add_argument(
        "--skip-record",
        action="store_true",
        help="local run: print the run-log row instead of writing BigQuery",
    )
    p.add_argument(
        "--plan-only",
        action="store_true",
        help="print what a real run would do and exit without touching anything",
    )
    a = p.parse_args()
    as_of = date.fromisoformat(a.as_of)
    if a.plan_only:
        print(
            f"PLAN ONLY - nothing executed.\n"
            f"  as_of              {as_of}\n"
            f"  1. pull            {'BigQuery -> runs/' + as_of.isoformat()}/*.parquet "
            f"(8 signals, weeks_back={a.weeks_back}, ~1.06 GB billed cold / ~386 MB warm)\n"
            f"  2. assemble        workbook + 14 CSVs + readme.json\n"
            f"  3. upload          gs://{a.bucket}/{GCS_PREFIX}/{as_of.isoformat()}/"
            f"{'  [SKIPPED]' if a.skip_upload else ''}\n"
            f"  4. record          {RUN_LOG_TABLE} + {PIPELINE_STATE_TABLE}"
            f"{'  [SKIPPED]' if a.skip_record else ''}"
        )
        raise SystemExit(0)
    main(
        as_of=as_of,
        months=a.months,
        horizon_weeks=a.horizon_weeks,
        weeks_back=a.weeks_back,
        bucket=a.bucket,
        skip_upload=a.skip_upload,
        skip_record=a.skip_record,
    )
