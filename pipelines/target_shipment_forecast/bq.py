"""BigQuery access: credentials, client, cost-gated queries, logical-table CTEs.

**Migration note (biom_sql, 2026-09-07).** The original upstream module delegated
credential resolution and CTE injection to `bpd_mcp.bq` (the bullseye MCP server).
Both are plain Python/SQL text work — nothing bullseye did needed an MCP server or
anything a BigQuery client cannot do — so both are now local and the third-party
dependency is gone, matching this repo's standing pattern (`google.cloud.bigquery`
client, no MCP wrappers).

What is deliberately NOT simplified away in that move: the four CTE bodies below
are copied **verbatim** from bullseye's registry, because "just read `bpd_raw`
directly" would be wrong in two specific, measured ways:

* `orders_daily` — `bpd_raw.daily_order_tcin_loc` ACCUMULATES every daily
  snapshot. Naive: 147,166 rows / 14,160,189 open units. Latest-state: 7,710 rows
  / 497,728 open units. **A 28.4x overstatement if the QUALIFY is dropped.** The
  `ORDER BY` must stay a TOTAL order: 1,430 (po, tcin, location) groups tie on the
  latest `SNAPSHOT_D`, and with no tiebreaker the open-unit headline moved between
  497,728 / 502,347 / 504,606 on four consecutive runs.
* `sales_weekly` / `inventory_weekly` — the CANVAS weekly grains stop at
  2026-05-02 while the raw weekly feeds run to the present, so a raw-only read
  loses history and a canvas-only read loses recent weeks. The union's `MAX()`
  boundary is dynamic ON PURPOSE: it self-heals the day `canvas_delta` catches up.
  Do not substitute a static date. Do NOT add `bpd_raw.history_sales_weekly` as a
  third branch — it overlaps `weekly_sales_tcin_loc` on 2026-04-04..05-02 and
  would double-count.

None of the bodies reference another logical name, so injection needs no
topological sort (upstream had one; it was dead code for every BPD table).

Surface:
* `resolve_credentials()` — `GOOGLE_APPLICATION_CREDENTIALS`, or `GCP_SA_KEY_B64`
  materialised to `~/.config/gcloud/biom-bq-sa.json` at mode 0600, else ADC. Key
  bytes are never returned or logged; only the path and a label.
* `client()` — `bigquery.Client` on `biom-reporting-s26` / `us-central1`
  (location is mandatory: without it INFORMATION_SCHEMA silently returns nothing).
* `query()` — dry-run first (0 bytes billed), refuse above the cap, then run with
  `maximum_bytes_billed` as a second guard.
* `logical(sql)` — prepend the CTE for every logical table the statement
  references. Pure, no network.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import stat
from collections.abc import Mapping
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

_P = PROJECT


class CredentialsUnavailable(RuntimeError):
    """No usable BigQuery credential; the message carries the remediation."""


class QueryTooExpensive(RuntimeError):
    """The dry-run estimate (or the server-side cap) exceeded `max_bytes_billed`."""

    def __init__(self, message: str, *, required_bytes: int | None = None) -> None:
        super().__init__(message)
        self.required_bytes = required_bytes


# --------------------------------------------------------------------------------------
# Logical tables — CTE bodies copied verbatim from bullseye's registry (see module docstring)
# --------------------------------------------------------------------------------------

LOGICAL_TABLES: dict[str, str] = {
    # THE QUALIFY IS THE WHOLE POINT OF THIS ENTRY. See the module docstring.
    "orders_daily": f"""
SELECT SNAPSHOT_D AS snapshot_d, PURCHASE_ORDER_CREATE_D AS purchase_order_create_d,
       PURCHASE_ORDER_ID AS purchase_order_id, PURCHASE_ORDER_ACTIVE_F AS purchase_order_active_f,
       IMPORT_ORDER_F AS import_order_f, VENDOR_ID AS vendor_id, UPC AS upc, TCIN AS tcin,
       DEPARTMENT_ID AS department_id, CLASS_ID AS class_id, ITEM_ID AS item_id, DPCI AS dpci,
       VENDOR_STYLE_ID AS vendor_style_id, PRODUCT_DESCRIPTION AS product_description,
       RECEIVING_LOCATION_ID AS receiving_location_id,
       RECEIVING_LOCATION_TYPE_C AS receiving_location_type_c,
       ORIGINAL_ORDER_Q AS original_order_q, REVISED_ORDER_Q AS revised_order_q,
       CANCEL_REMAINING_ORDER_Q AS cancel_remaining_order_q,
       ORIGINAL_ESTIMATED_ARRIVAL_D AS original_estimated_arrival_d,
       REVISED_ESTIMATED_ARRIVAL_D AS revised_estimated_arrival_d,
       ITEM_RECEIVED_Q AS item_received_q,
       ITEM_RECEIVED_TOTAL_COST_A AS item_received_total_cost_a,
       ITEM_RECEIVED_TOTAL_RETAIL_A AS item_received_total_retail_a,
       -- Target's "not cancelled" sentinel in this column is DATE '0001-01-01', on
       -- 156,856 of 156,863 rows since the feed began (2026-05-04). pandas' nanosecond
       -- datetime64 floor is 1677-09-21, so to_dataframe() raises OutOfBoundsDatetime on
       -- it and the ENTIRE `pull` dies at orders_latest. NULLIF is also the honest
       -- semantic: no cancel date means not cancelled.
       -- Found 2026-09-08 while sizing the deploy: it was LATENT because pandas 3.0.5
       -- coerced the value silently, and the `pandas>=2.2,<3` pin (recommended by the
       -- first-run report, applied in round 2) put a 2.x runtime under code nobody had
       -- re-pulled since. A deployed image built from requirements.txt would have failed
       -- on its first scheduled run. Only this column is affected -- every DATE column on
       -- the other five feeds Shipcast reads was scanned for pre-1900 values: all clean.
       NULLIF(PURCHASE_ORDER_CANCEL_D, DATE '0001-01-01') AS purchase_order_cancel_d,
       PURCHASE_ORDER_CANCELED_F AS purchase_order_canceled_f,
       ON_ORDER_1_WEEK_OUT_Q AS on_order_1_week_out_q,
       ON_ORDER_2_WEEK_OUT_Q AS on_order_2_week_out_q,
       ON_ORDER_3_WEEK_OUT_Q AS on_order_3_week_out_q,
       ON_ORDER_4_8_WEEK_OUT_Q AS on_order_4_8_week_out_q,
       ON_ORDER_9_WEEK_OUT_Q AS on_order_9_week_out_q
FROM `{_P}.bpd_raw.daily_order_tcin_loc` AS o
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY PURCHASE_ORDER_ID, TCIN, RECEIVING_LOCATION_ID
  ORDER BY SNAPSHOT_D DESC, ITEM_RECEIVED_Q DESC, CANCEL_REMAINING_ORDER_Q DESC,
           REVISED_ORDER_Q DESC, ORIGINAL_ORDER_Q DESC, TO_JSON_STRING(o) ASC) = 1
""",
    # canvas history_weekly grain ∪ raw feed beyond it, on a self-healing MAX() boundary.
    "sales_weekly": f"""
SELECT sales_date, tcin, location_id, origination_channel, reporting_channel, fulfillment_type,
       vendor_id, barcode, dpci, manufacturer_style, dept, class, item_description,
       sale_amount, sale_quantity, drive_up_sale_a, drive_up_sale_q
FROM `{_P}.biom_canvas.fct_target_sales`
WHERE is_current AND NOT is_deleted AND data_grain IN ('weekly','history_weekly')
UNION ALL
SELECT SALES_DATE, TCIN, LOCATION_ID, ORIGINATION_CHANNEL, REPORTING_CHANNEL, FULFILLMENT_TYPE,
       VENDOR_ID, BARCODE, DPCI, MANUFACTURER_STYLE, DEPT, CLASS, ITEM_DESCRIPTION,
       SALE_AMOUNT, SALE_QUANTITY, DRIVE_UP_SALE_A, DRIVE_UP_SALE_Q
FROM `{_P}.bpd_raw.weekly_sales_tcin_loc`
WHERE SALES_DATE > (SELECT MAX(sales_date) FROM `{_P}.biom_canvas.fct_target_sales`
                    WHERE is_current AND NOT is_deleted AND data_grain IN ('weekly','history_weekly'))
""",
    # There is NO data_grain='weekly' in fct_target_inventory — only 'daily' and 'history_weekly'.
    "inventory_weekly": f"""
SELECT inventory_date AS business_d, tcin, location_id, primary_vendor_id, department_id, class_id,
       dpci, manufacturer_style, item_description,
       ending_on_hand_a, ending_on_hand_q, ending_on_transfer_a, ending_on_transfer_q,
       ending_on_purchase_a, ending_on_purchase_q, instock_q, instock_percentage,
       out_of_stock_q, out_of_stock_percentage, tracked_item_out_of_stock_q
FROM `{_P}.biom_canvas.fct_target_inventory`
WHERE is_current AND NOT is_deleted AND data_grain = 'history_weekly'
UNION ALL
SELECT BUSINESS_D, TCIN, LOCATION_ID, PRIMARY_VENDOR_ID, DEPARTMENT_ID, CLASS_ID,
       DPCI, MANUFACTURER_STYLE, ITEM_DESCRIPTION,
       ENDING_ON_HAND_A, ENDING_ON_HAND_Q, ENDING_ON_TRANSFER_A, ENDING_ON_TRANSFER_Q,
       ENDING_ON_PURCHASE_A, ENDING_ON_PURCHASE_Q, INSTOCK_Q, INSTOCK_PERCENTAGE,
       OUT_OF_STOCK_Q, OUT_OF_STOCK_PERCENTAGE, TRACKED_ITEM_OUT_OF_STOCK_Q
FROM `{_P}.bpd_raw.weekly_inv_tcin_loc`
WHERE BUSINESS_D > (SELECT MAX(inventory_date) FROM `{_P}.biom_canvas.fct_target_inventory`
                    WHERE is_current AND NOT is_deleted AND data_grain = 'history_weekly')
""",
    # NO dedup, deliberately: this feed accumulates snapshots and every caller filters
    # to the BUSINESS_D values it wants. Exactly one layer owns that reduction and it is
    # the caller (`channels.target.signals.plan_snapshots`), never this body.
    "po_plan_daily": f"""
SELECT BUSINESS_D AS business_d, TCIN AS tcin, ORDER_D AS order_d,
       RECEIVING_LOCATION_ID AS receiving_location_id, DEPARTMENT_ID AS department_id,
       DPCI AS dpci, VENDOR_CASE_PACK_Q AS vendor_case_pack_q,
       ORDERED_Q AS ordered_q, RECEIVED_Q AS received_q,
       SCHEDULED_RECEIPT_Q AS scheduled_receipt_q,
       NET_STORE_MEAN_DEMAND_Q AS net_store_mean_demand_q,
       BEGINNING_SALESFLOOR_PRESENTATION_UNIT_Q AS beginning_salesfloor_presentation_unit_q,
       ENDING_SALESFLOOR_PRESENTATION_UNIT_Q AS ending_salesfloor_presentation_unit_q
FROM `{_P}.bpd_raw.dly_po_plan_tcin`
""",
    # Item attributes, item grain. Source columns are SPACE-SEPARATED (`ITEM STATE`,
    # `VENDOR ID`, ...) so every reference needs backticks. `LAUNCH DATE` is a STRING
    # carrying Target's `""` placeholder — never CAST it, and `LAST UPDATE DATE` is the
    # only populated DATE/TIMESTAMP column. This is the live source of `item_state`
    # (biom_sql fix (d), 2026-09-07): the sibling `weekly_item_mta` feed that bullseye
    # exposes as `item_attr` is STALE (max PROCESSED_CT_DATE 2026-07-25) and must not be
    # used for it.
    "item_attr_extended": f"""
SELECT TCIN AS tcin, UPC AS upc, DPCI AS dpci, `ITEM STATE` AS item_state,
       DESCRIPTION AS description, `VENDOR ID` AS vendor_id, `VENDOR NAME` AS vendor_name,
       `BRAND NAME` AS brand_name, `DEPT NO` AS dept_no, `DEPT DESCRIPTION` AS dept_description,
       `CLASS NO` AS class_no, `CLASS DESCRIPTION` AS class_description,
       `PARENT TCIN` AS parent_tcin, `PRODUCT TYPE NAME` AS product_type_name,
       `LAUNCH DATE` AS launch_date, `LAST UPDATE DATE` AS last_update_date
FROM `{_P}.bpd_raw.wkly_tcin_item`
""",
}

_COMMENT = re.compile(r"--[^\n]*")


def _strip_comments(sql: str) -> str:
    """Line comments removed, so a name merely *mentioned* in a comment is not injected."""
    return _COMMENT.sub("", sql)


def logical(sql: str) -> str:
    """Prepend the CTE for every logical table `sql` references. Pure: no network.

    Statements that reference no logical table come back **unchanged**, which is how
    the raw as-of exceptions in `channels.target.signals` pass through untouched.
    """
    body = _strip_comments(sql)
    needed = [n for n in LOGICAL_TABLES if re.search(rf"\b{re.escape(n)}\b", body)]
    if not needed:
        return sql
    ctes = ",\n".join(f"{n} AS ({LOGICAL_TABLES[n]})" for n in needed)
    return f"WITH {ctes}\n{sql}"


def logical_names() -> frozenset[str]:
    """Names of the logical tables available to `logical()`."""
    return frozenset(LOGICAL_TABLES)


# --------------------------------------------------------------------------------------
# Credentials and client
# --------------------------------------------------------------------------------------


ADC_WELL_KNOWN = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def credentials_available() -> bool:
    """Cheap, network-free check used to gate live tests and `check`.

    Includes gcloud ADC (the well-known file), because `resolve_credentials` accepts it:
    upstream this function looked only at the two env vars, which was right when bullseye
    owned credential resolution and only handled those two. With the local ADC fallback,
    an env-var-only check makes `check` report "no credential" on a laptop where every
    query would in fact succeed. Deliberately a FILE test, not `google.auth.default()`:
    that call can probe the GCE metadata server, which is neither cheap nor network-free.
    """
    return bool(
        os.environ.get(SA_KEY_ENV) or os.environ.get(ADC_ENV) or ADC_WELL_KNOWN.exists()
    )


def resolve_credentials() -> tuple[Path | None, str]:
    """Make a service-account credential usable; returns `(path_or_None, label)`.

    `GCP_SA_KEY_B64` is written atomically to `~/.config/gcloud/biom-bq-sa.json` at
    mode 0600 and exported as `GOOGLE_APPLICATION_CREDENTIALS`. The label is safe to
    log; the key bytes are not, and are never returned.
    """
    existing = os.environ.get(ADC_ENV)
    if existing and Path(existing).exists():
        log.info("bigquery credentials: %s=%s", ADC_ENV, existing)
        return Path(existing), f"{ADC_ENV}={existing}"

    b64 = os.environ.get(SA_KEY_ENV)
    if b64:
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception as e:
            raise CredentialsUnavailable(f"{SA_KEY_ENV} is not valid base64") from e
        SA_KEY_DEST.parent.mkdir(parents=True, exist_ok=True)
        tmp = SA_KEY_DEST.with_suffix(".tmp")
        tmp.write_bytes(raw)
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600 before it is visible at the real path
        tmp.replace(SA_KEY_DEST)
        os.environ[ADC_ENV] = str(SA_KEY_DEST)
        log.info("bigquery credentials: %s materialised to %s", SA_KEY_ENV, SA_KEY_DEST)
        return SA_KEY_DEST, f"{SA_KEY_ENV} -> {SA_KEY_DEST}"

    # Fall through to Application Default Credentials (gcloud ADC on a laptop).
    try:
        import google.auth

        google.auth.default()
    except Exception as e:
        raise CredentialsUnavailable(
            f"no BigQuery credential. Set {ADC_ENV}=/path/key.json, or {SA_KEY_ENV}="
            "<base64 of the key JSON>, or run `gcloud auth application-default login`."
        ) from e
    log.info("bigquery credentials: application default")
    return None, "application default credentials"


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

    The dry-run estimate is attached as `df.attrs["bytes_processed_estimate"]` and the
    real job's `total_bytes_billed` as `df.attrs["bytes_billed"]`.
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


def session_user(bq_client: Any = None) -> str:
    """`SELECT SESSION_USER()` — the principal BigQuery sees; 0 bytes billed."""
    df = query("SELECT SESSION_USER() AS user", max_bytes_billed=1, bq_client=bq_client)
    return str(df["user"].iloc[0])
