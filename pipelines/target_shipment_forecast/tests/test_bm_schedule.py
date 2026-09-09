"""The B&M Target Schedule's two ways into a run produce ONE frame.

Scheduled path: ingest/bm_schedule_ingest.py parses the sheet and appends a snapshot to
biom_admin.bm_target_schedule_snapshot; the run reads the newest snapshot on or before
as_of and rebuilds the frame (`frame_from_snapshot`). Hand-run path: `run --bm PATH`
parses the file directly. These tests pin that the two are interchangeable, that the
snapshot read is as-of filtered, and that `bm_schedule_input` prefers the file, then the
snapshot, then nothing -- loudly when the file the operator named does not parse.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from pipelines.target_shipment_forecast import run as run_mod
from pipelines.target_shipment_forecast.channels.target import signals
from pipelines.target_shipment_forecast.inputs.bm_master_forecast import (
    BLOCK_FLOOR,
    METRICS,
    SNAPSHOT_COLUMNS,
    BmParseError,
    frame_from_snapshot,
    resolve_frame,
    snapshot_rows,
)
from pipelines.target_shipment_forecast.tests.test_bm_master_forecast import _parse, _sheet

MODIFIED = datetime(2026, 9, 9, 12, 33, 13, tzinfo=timezone.utc)


class _NoItems:
    def tcins_for(self, *_a, **_k):
        return []


class _OneItem:
    """Resolves exactly one fixture SKU, so re-resolution has something to find."""

    def tcins_for(self, key, **_k):
        return [1000001] if str(key).upper() == "P-SKU-000" else []


class _NoAliases:
    def resolve(self, raw, *, kind="auto"):
        class R:
            canonical_sku = str(raw)

        return R()


def _rows(bm):
    return snapshot_rows(
        bm,
        snapshot_date=MODIFIED.date(),
        source_file_id="local:bm.xlsx",
        source_name="bm.xlsx",
        source_modified_time=MODIFIED,
        loaded_at=datetime.now(timezone.utc),
    )


def test_snapshot_rows_carry_the_table_columns_in_order(tmp_path):
    bm = _parse(_sheet(tmp_path))
    rows = _rows(bm)
    assert list(rows.columns) == list(SNAPSHOT_COLUMNS)
    assert len(rows) == len(bm.frame) == BLOCK_FLOOR * len(bm.months)
    assert rows["source_row"].notna().all()  # every block knows its Stores row
    assert rows["tcin"].isna().all()  # no item master: unresolved, kept, not dropped
    assert rows["snapshot_date"].nunique() == 1


def test_snapshot_round_trips_to_the_same_frame(tmp_path):
    bm = _parse(_sheet(tmp_path))
    back = frame_from_snapshot(_rows(bm), item_master=_NoItems(), aliases=_NoAliases())
    assert back.months == bm.months
    assert back.snapshot_date == MODIFIED.date()
    assert back.source_file == "bm.xlsx"
    a = bm.frame.sort_values(["unique_key", "month_start"]).reset_index(drop=True)
    b = back.frame.sort_values(["unique_key", "month_start"]).reset_index(drop=True)
    for metric in METRICS:
        col = f"bm_{metric.lower()}"
        assert (a[col].to_numpy() == b[col].to_numpy()).all(), col
    assert list(a["unique_key"]) == list(b["unique_key"])
    assert len(back.unresolved) == BLOCK_FLOOR


def test_a_snapshot_resolves_tcins_the_item_master_learned_after_the_load(tmp_path):
    bm = _parse(_sheet(tmp_path))  # loaded with nothing resolved
    back = frame_from_snapshot(_rows(bm), item_master=_OneItem(), aliases=_NoAliases())
    got = back.frame.loc[back.frame["bm_sku"] == "P-SKU-000", "tcin"].dropna().unique()
    assert list(got) == [1000001]
    assert len(back.unresolved) == BLOCK_FLOOR - 1
    # and resolve_frame never overwrites a TCIN already present
    again, _ = resolve_frame(back.frame, item_master=_NoItems(), aliases=_NoAliases())
    assert list(again.loc[again["bm_sku"] == "P-SKU-000", "tcin"].dropna().unique()) == [1000001]


def test_two_snapshots_in_one_frame_are_refused(tmp_path):
    bm = _parse(_sheet(tmp_path))
    r1 = _rows(bm)
    r2 = r1.copy()
    r2["snapshot_date"] = pd.Timestamp("2026-09-10")
    with pytest.raises(BmParseError, match="span 2 distinct snapshot_date"):
        frame_from_snapshot(pd.concat([r1, r2]), item_master=_NoItems(), aliases=_NoAliases())


def test_a_truncated_snapshot_hits_the_same_floor_as_the_parser(tmp_path):
    bm = _parse(_sheet(tmp_path))
    rows = _rows(bm)
    few = rows[rows["unique_key"].isin(sorted(rows["unique_key"].unique())[: BLOCK_FLOOR - 1])]
    with pytest.raises(BmParseError, match="below floor"):
        frame_from_snapshot(few, item_master=_NoItems(), aliases=_NoAliases())


def test_bm_schedule_sql_reads_the_newest_snapshot_on_or_before_as_of():
    sql = signals.bm_schedule_sql()
    assert "biom_admin.bm_target_schedule_snapshot" in sql
    assert "snapshot_date <= @as_of" in sql
    assert "MAX(snapshot_date)" in sql
    assert "source_modified_time = MAX(source_modified_time) OVER ()" in sql
    for c in SNAPSHOT_COLUMNS:
        assert c in sql, c
    assert "bm_schedule" in signals.ALL_SQL
    assert "bm_schedule" in signals.describe()["admin_snapshots"]


def test_bm_schedule_asof_tolerates_a_missing_table_and_returns_the_columns():
    def missing(sql, params=None):
        raise RuntimeError(
            "404 Not found: Table biom-reporting-s26:biom_admin.bm_target_schedule_snapshot"
        )

    df = signals.bm_schedule_asof(date(2026, 9, 8), run=missing)
    assert df.empty and list(df.columns) == list(SNAPSHOT_COLUMNS)

    def other(sql, params=None):
        raise RuntimeError("403 permission denied")

    with pytest.raises(RuntimeError, match="403"):
        signals.bm_schedule_asof(date(2026, 9, 8), run=other)


def test_bm_schedule_input_prefers_file_then_snapshot_then_none(tmp_path):
    p = _sheet(tmp_path)
    fc, meta = run_mod.bm_schedule_input(
        {"bm_schedule": None}, bm_path=p, item_master=_NoItems(), aliases=_NoAliases()
    )
    assert fc is not None and meta["source"] == "local_file" and meta["blocks"] == BLOCK_FLOOR
    assert meta["unresolved"] and meta["tcins_resolved"] == 0

    rows = _rows(fc)
    fc2, meta2 = run_mod.bm_schedule_input(
        {"bm_schedule": rows}, bm_path=None, item_master=_NoItems(), aliases=_NoAliases()
    )
    assert fc2 is not None and meta2["source"] == "bq_snapshot"
    assert fc2.months == fc.months and meta2["snapshot_date"] == MODIFIED.date().isoformat()
    assert meta2["source_modified_time"]

    fc3, meta3 = run_mod.bm_schedule_input(
        {"bm_schedule": pd.DataFrame(columns=list(SNAPSHOT_COLUMNS))},
        bm_path=None,
        item_master=_NoItems(),
        aliases=_NoAliases(),
    )
    assert fc3 is None and meta3["source"] == "none" and "no snapshot" in meta3["reason"]


def test_a_bad_snapshot_degrades_with_the_reason_but_a_bad_file_is_loud(tmp_path):
    bm = _parse(_sheet(tmp_path))
    rows = _rows(bm)
    few = rows[rows["unique_key"].isin(sorted(rows["unique_key"].unique())[:5])]
    fc, meta = run_mod.bm_schedule_input(
        {"bm_schedule": few}, bm_path=None, item_master=_NoItems(), aliases=_NoAliases()
    )
    assert fc is None and meta["source"] == "none" and "snapshot rejected" in meta["reason"]

    bad = _sheet(tmp_path, blocks=BLOCK_FLOOR - 1)
    with pytest.raises(BmParseError, match="below floor"):
        run_mod.bm_schedule_input(
            {"bm_schedule": rows}, bm_path=bad, item_master=_NoItems(), aliases=_NoAliases()
        )
