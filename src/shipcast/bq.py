"""BigQuery access for shipcast: credentials, client, cost-gated queries, logical SQL.

Reuse, not re-derivation: credential resolution and the logical-table registry
come from bullseye (`bpd_mcp.bq`). This module adds only what a batch
forecaster needs on top of a read-only MCP server's data layer:

* `resolve_credentials()` — `GOOGLE_APPLICATION_CREDENTIALS`, or
  `GCP_SA_KEY_B64` materialised to `~/.config/gcloud/biom-bq-sa.json` with mode
  0600. Key bytes are never returned or logged; only the path and a label.
* `client()` — a `bigquery.Client` on project `biom-reporting-s26`,
  location `us-central1` (location is mandatory: without it INFORMATION_SCHEMA
  silently returns nothing).
* `query(sql, max_bytes_billed=...)` — dry-run first (0 bytes billed, returns
  the exact scan estimate), refuse above the cap, then run with
  `maximum_bytes_billed` as a second guard. Returns a DataFrame with DATE
  columns as `datetime64[ns]`.
* `logical(sql)` — `bpd_mcp.bq.build`, which prepends the CTEs for every
  bullseye logical table the statement references (`orders_daily` with its
  mandatory de-dup QUALIFY, `po_plan_daily`, `sales_weekly`, ...).

`bpd_mcp` is imported lazily so that the pure-python parts of shipcast
(parsers, scoring, aliases) import without bullseye installed.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

PROJECT = "biom-reporting-s26"
LOCATION = "us-central1"
DEFAULT_MAX_BYTES_BILLED = 2 * 1024**3  # 2 GiB; config/target.yaml bq.max_bytes_billed

SA_KEY_ENV = "GCP_SA_KEY_B64"
ADC_ENV = "GOOGLE_APPLICATION_CREDENTIALS"
SA_KEY_DEST = Path.home() / ".config" / "gcloud" / "biom-bq-sa.json"


class CredentialsUnavailable(RuntimeError):
    """No usable BigQuery credential; the message carries the remediation."""


class QueryTooExpensive(RuntimeError):
    """The dry-run estimate (or the server-side cap) exceeded `max_bytes_billed`."""

    def __init__(self, message: str, *, required_bytes: int | None = None) -> None:
        super().__init__(message)
        self.required_bytes = required_bytes


class BullseyeUnavailable(ImportError):
    """`bpd_mcp` (bullseye) is not installed in this environment."""


def _bpd_bq() -> Any:
    """Import `bpd_mcp.bq` lazily with an actionable error."""
    try:
        from bpd_mcp import bq as bpd_bq
    except ImportError as e:  # pragma: no cover - depends on the environment
        raise BullseyeUnavailable(
            "bullseye (bpd-mcp) is not installed. `uv sync` fetches it from GitHub "
            "(private repo: see README for the deploy-key / PAT setup), or for local "
            "development `uv add --editable /path/to/bullseye`."
        ) from e
    return bpd_bq


def credentials_available() -> bool:
    """Cheap, network-free check used to gate live tests and `shipcast check`."""
    return bool(os.environ.get(SA_KEY_ENV) or os.environ.get(ADC_ENV))


def resolve_credentials() -> tuple[Path | None, str]:
    """Make a service-account credential usable; returns `(path_or_None, label)`.

    Delegates to `bpd_mcp.bq.resolve_credentials`, which writes `GCP_SA_KEY_B64`
    atomically to `~/.config/gcloud/biom-bq-sa.json` (mode 0600) and exports
    `GOOGLE_APPLICATION_CREDENTIALS`. The label is safe to log; the key is not
    and never is.
    """
    bq = _bpd_bq()
    try:
        path, label = bq.resolve_credentials()
    except bq.CredentialsUnavailable as e:
        raise CredentialsUnavailable(str(e)) from e
    log.info("bigquery credentials: %s", label)
    return path, label


def client(project: str = PROJECT, location: str = LOCATION) -> Any:
    """A `google.cloud.bigquery.Client` with credentials resolved first."""
    resolve_credentials()
    from google.cloud import bigquery

    return bigquery.Client(project=project, location=location)


def _param(name: str, value: Any) -> Any:
    """Map a Python value to a BigQuery query parameter (scalar or array)."""
    from google.cloud import bigquery

    def scalar_type(v: Any) -> str:
        if isinstance(v, bool):
            return "BOOL"
        if isinstance(v, int):
            return "INT64"
        if isinstance(v, float):
            return "FLOAT64"
        if isinstance(v, datetime):
            return "TIMESTAMP"
        if isinstance(v, date):
            return "DATE"
        if isinstance(v, str):
            return "STRING"
        raise TypeError(f"unsupported query parameter type for {name!r}: {type(v).__name__}")

    if isinstance(value, list | tuple | set | frozenset):
        values = list(value)
        t = scalar_type(values[0]) if values else "STRING"
        return bigquery.ArrayQueryParameter(name, t, values)
    return bigquery.ScalarQueryParameter(name, scalar_type(value), value)


def _job_config(params: Mapping[str, Any] | None, *, dry_run: bool, max_bytes: int | None) -> Any:
    from google.cloud import bigquery

    cfg = bigquery.QueryJobConfig(dry_run=dry_run, use_query_cache=not dry_run)
    if params:
        cfg.query_parameters = [_param(k, v) for k, v in params.items()]
    if max_bytes is not None and not dry_run:
        cfg.maximum_bytes_billed = int(max_bytes)
    return cfg


def dry_run(sql: str, *, params: Mapping[str, Any] | None = None, bq_client: Any = None) -> int:
    """Bytes the statement would process. Bills nothing; validates the SQL."""
    c = bq_client or client()
    job = c.query(sql, job_config=_job_config(params, dry_run=True, max_bytes=None))
    return int(job.total_bytes_processed or 0)


def _dates_to_datetime(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise db-dtypes `dbdate` / object-date columns to `datetime64[ns]`."""
    for c in df.columns:
        s = df[c]
        if str(s.dtype) == "dbdate":
            df[c] = pd.to_datetime(s.astype(object))
        elif s.dtype == object:
            nn = s.dropna()
            if len(nn) and isinstance(nn.iloc[0], date | datetime):
                df[c] = pd.to_datetime(s)
    return df


def query(
    sql: str,
    *,
    params: Mapping[str, Any] | None = None,
    max_bytes_billed: int = DEFAULT_MAX_BYTES_BILLED,
    bq_client: Any = None,
) -> pd.DataFrame:
    """Run `sql` and return a DataFrame, refusing before any byte is billed if too costly.

    The dry-run estimate is attached as `df.attrs["bytes_processed_estimate"]`
    and the real job's `total_bytes_billed` as `df.attrs["bytes_billed"]`.
    """
    c = bq_client or client()
    est = dry_run(sql, params=params, bq_client=c)
    if est > max_bytes_billed:
        raise QueryTooExpensive(
            f"dry run estimates {est:,} bytes > cap {max_bytes_billed:,}. "
            "Narrow the statement (date filter, one BUSINESS_D) or raise the cap deliberately.",
            required_bytes=est,
        )
    job = c.query(sql, job_config=_job_config(params, dry_run=False, max_bytes=max_bytes_billed))
    try:
        result = job.result()
    except Exception as e:  # BigQuery reports the bytes cap as a 500, not a 403
        if "bytesBilledLimitExceeded" in str(e):
            raise QueryTooExpensive(str(e), required_bytes=est) from e
        raise
    df = _dates_to_datetime(result.to_dataframe())
    df.attrs["bytes_processed_estimate"] = est
    df.attrs["bytes_billed"] = int(job.total_bytes_billed or 0)
    log.info(
        "bigquery job %s: %s rows, %s bytes billed", job.job_id, len(df), df.attrs["bytes_billed"]
    )
    return df


def logical(sql: str) -> str:
    """Prepend the bullseye CTEs for every logical table `sql` references.

    Pure: no network. Statements that reference no logical table come back
    unchanged, which is how the two raw as-of exceptions in
    `channels.target.signals` pass through untouched.
    """
    return str(_bpd_bq().build(sql))


def logical_names() -> frozenset[str]:
    """Names of the bullseye logical tables available to `logical()`."""
    return frozenset(_bpd_bq().logical_names())


def session_user(bq_client: Any = None) -> str:
    """`SELECT SESSION_USER()` — the principal BigQuery sees; 0 bytes billed."""
    df = query("SELECT SESSION_USER() AS user", max_bytes_billed=1, bq_client=bq_client)
    return str(df["user"].iloc[0])


def run_sql_file_safe(paths: Sequence[Path]) -> None:  # pragma: no cover - guard only
    """Refuse to exist: shipcast never executes SQL files from disk."""
    raise NotImplementedError("shipcast composes SQL in code; it does not run SQL files")
