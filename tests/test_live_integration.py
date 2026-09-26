"""Integration tests against a real ClickHouse server.

These reproduce the same behaviours documented in the README (an MV missing
pre-existing rows, a JOIN reading a foreign table's current state, an
erroring MV, POPULATE, and the AggregatingMergeTree silent-merge bug) and
then check that this package's live checks actually catch them. Skipped
automatically if no server is reachable (see ``conftest.ch_client``).
"""

from __future__ import annotations

import time

from clickhouse_mv_check.http_client import ClickHouseHTTPError, HttpClient
from clickhouse_mv_check.live import (
    fetch_mv_info,
    fetch_table_info,
    list_materialized_views,
    run_live_checks,
)
from clickhouse_mv_check.models import Severity

from .conftest import exec_all


def test_mv_misses_rows_that_predate_it(ch_client: HttpClient, unique_name: str) -> None:
    src, tgt, mv = f"{unique_name}_src", f"{unique_name}_tgt", f"{unique_name}_mv"
    exec_all(
        ch_client,
        f"CREATE TABLE {src} (id UInt32, val String) ENGINE = MergeTree ORDER BY id",
        f"INSERT INTO {src} VALUES (1,'a'),(2,'b'),(3,'c')",
        f"CREATE TABLE {tgt} (id UInt32, val String) ENGINE = MergeTree ORDER BY id",
        f"CREATE MATERIALIZED VIEW {mv} TO {tgt} AS SELECT id, val FROM {src}",
    )
    assert ch_client.query(f"SELECT count() FROM {tgt}") == [(0,)]

    exec_all(ch_client, f"INSERT INTO {src} VALUES (4,'d')")
    assert ch_client.query(f"SELECT count() FROM {tgt}") == [(1,)]
    assert ch_client.query(f"SELECT count() FROM {src}") == [(4,)]


def test_join_reads_foreign_table_at_trigger_time_not_live(
    ch_client: HttpClient, unique_name: str
) -> None:
    customers, orders, tgt, mv = (
        f"{unique_name}_customers",
        f"{unique_name}_orders",
        f"{unique_name}_enriched",
        f"{unique_name}_mv",
    )
    exec_all(
        ch_client,
        f"CREATE TABLE {customers} (cust_id UInt32, name String) ENGINE = MergeTree ORDER BY cust_id",
        f"CREATE TABLE {orders} (order_id UInt32, cust_id UInt32) ENGINE = MergeTree ORDER BY order_id",
        f"CREATE TABLE {tgt} (order_id UInt32, cust_id UInt32, name String) ENGINE = MergeTree ORDER BY order_id",
        f"CREATE MATERIALIZED VIEW {mv} TO {tgt} AS "
        f"SELECT o.order_id, o.cust_id, c.name FROM {orders} o LEFT JOIN {customers} c ON o.cust_id = c.cust_id",
    )
    # Order for a customer that doesn't exist yet in the joined table.
    exec_all(ch_client, f"INSERT INTO {orders} VALUES (102, 3)")
    exec_all(ch_client, f"INSERT INTO {customers} VALUES (3, 'carol')")
    row = ch_client.query(f"SELECT name FROM {tgt} WHERE order_id = 102")
    assert row == [("",)]  # confirmed: permanently empty, the JOIN saw customers before carol existed

    # A second order placed after carol exists joins correctly.
    exec_all(ch_client, f"INSERT INTO {orders} VALUES (103, 3)")
    row = ch_client.query(f"SELECT name FROM {tgt} WHERE order_id = 103")
    assert row == [("carol",)]

    findings = run_live_checks(ch_client, "default", mv)
    assert any(f.rule == "join-reads-foreign-table" for f in findings)


def test_erroring_mv_select_fails_the_client_visible_insert_but_source_keeps_the_rows(
    ch_client: HttpClient, unique_name: str
) -> None:
    src, tgt, mv = f"{unique_name}_src", f"{unique_name}_tgt", f"{unique_name}_mv"
    exec_all(
        ch_client,
        f"CREATE TABLE {src} (id UInt32, denom UInt32) ENGINE = MergeTree ORDER BY id",
        f"CREATE TABLE {tgt} (id UInt32, result UInt32) ENGINE = MergeTree ORDER BY id",
        f"CREATE MATERIALIZED VIEW {mv} TO {tgt} AS SELECT id, intDiv(100, denom) AS result FROM {src}",
        f"INSERT INTO {src} VALUES (1, 10)",
    )
    assert ch_client.query(f"SELECT count() FROM {tgt}") == [(1,)]

    try:
        ch_client.query(f"INSERT INTO {src} VALUES (2, 5), (3, 0), (4, 2)")
        raised = False
    except ClickHouseHTTPError:
        raised = True
    assert raised, "an MV whose SELECT divides by zero must surface as a client-visible error"

    # Confirmed: the base table insert is NOT rolled back even though the client saw an error.
    assert ch_client.query(f"SELECT count() FROM {src}") == [(4,)]
    # But the MV target only has what it managed to write before the error — permanently short.
    assert ch_client.query(f"SELECT count() FROM {tgt}") == [(1,)]


def test_populate_backfills_pre_existing_rows(ch_client: HttpClient, unique_name: str) -> None:
    src, tgt, mv = f"{unique_name}_src", f"{unique_name}_tgt", f"{unique_name}_mv"
    exec_all(
        ch_client,
        f"CREATE TABLE {src} (id UInt32, val String) ENGINE = MergeTree ORDER BY id",
        f"INSERT INTO {src} VALUES (1,'a'),(2,'b'),(3,'c')",
        f"CREATE TABLE {tgt} (id UInt32, val String) ENGINE = MergeTree ORDER BY id",
        f"CREATE MATERIALIZED VIEW {mv} TO {tgt} POPULATE AS SELECT id, val FROM {src}",
    )
    assert ch_client.query(f"SELECT count() FROM {tgt}") == [(3,)]

    exec_all(ch_client, f"INSERT INTO {src} VALUES (4,'d')")
    assert ch_client.query(f"SELECT count() FROM {tgt}") == [(4,)]

    # A live snapshot cannot tell POPULATE was used — SHOW CREATE never mentions it.
    show_create = ch_client.query(f"SHOW CREATE TABLE {mv}")[0][0]
    assert "POPULATE" not in show_create


def test_aggregating_merge_tree_drops_values_on_merge_with_wrong_column_type(
    ch_client: HttpClient, unique_name: str
) -> None:
    src, tgt, mv = f"{unique_name}_src", f"{unique_name}_tgt", f"{unique_name}_mv"
    exec_all(
        ch_client,
        f"CREATE TABLE {src} (key String, amount UInt64) ENGINE = MergeTree ORDER BY key",
        f"CREATE TABLE {tgt} (key String, amount UInt64) ENGINE = AggregatingMergeTree ORDER BY key",
        f"CREATE MATERIALIZED VIEW {mv} TO {tgt} AS SELECT key, sum(amount) AS amount FROM {src} GROUP BY key",
        f"INSERT INTO {src} VALUES ('a', 10)",
        f"INSERT INTO {src} VALUES ('a', 20)",
    )
    assert ch_client.query(f"SELECT sum(amount) FROM {tgt}") == [(30,)]  # correct before merge

    exec_all(ch_client, f"OPTIMIZE TABLE {tgt} FINAL")
    assert ch_client.query(f"SELECT sum(amount) FROM {tgt}") == [(10,)]  # silently wrong after merge

    findings = run_live_checks(ch_client, "default", mv)
    agg_findings = [f for f in findings if f.rule == "aggregate-state-mismatch"]
    assert len(agg_findings) == 1
    assert agg_findings[0].severity == Severity.ERROR


def test_missing_to_table_creates_hidden_inner_storage(ch_client: HttpClient, unique_name: str) -> None:
    src, mv = f"{unique_name}_src", f"{unique_name}_mv"
    exec_all(
        ch_client,
        f"CREATE TABLE {src} (id UInt32) ENGINE = MergeTree ORDER BY id",
        f"CREATE MATERIALIZED VIEW {mv} ENGINE = MergeTree ORDER BY id AS SELECT id FROM {src}",
    )
    info = fetch_mv_info(ch_client, "default", mv)
    assert info is not None
    assert info.target_table.startswith(".inner")

    findings = run_live_checks(ch_client, "default", mv)
    assert any(f.rule == "missing-to-table" for f in findings)


def test_backfill_plausibility_flags_a_missed_backfill(ch_client: HttpClient, unique_name: str) -> None:
    src, tgt, mv = f"{unique_name}_src", f"{unique_name}_tgt", f"{unique_name}_mv"
    exec_all(
        ch_client,
        f"CREATE TABLE {src} (id UInt32, val String) ENGINE = MergeTree ORDER BY id",
        f"INSERT INTO {src} VALUES (1,'a'),(2,'b'),(3,'c')",
    )
    time.sleep(2)  # separate part modification_time from the view's creation time
    exec_all(
        ch_client,
        f"CREATE TABLE {tgt} (id UInt32, val String) ENGINE = MergeTree ORDER BY id",
        f"CREATE MATERIALIZED VIEW {mv} TO {tgt} AS SELECT id, val FROM {src}",
    )
    time.sleep(1)
    exec_all(ch_client, f"INSERT INTO {src} VALUES (4,'d')")

    findings = run_live_checks(ch_client, "default", mv, ratio_threshold=0.8)
    backfill_findings = [f for f in findings if f.rule == "backfill-plausibility"]
    assert len(backfill_findings) == 1
    assert "4" in backfill_findings[0].message  # total source rows


def test_backfill_plausibility_does_not_flag_a_properly_populated_view(
    ch_client: HttpClient, unique_name: str
) -> None:
    src, tgt, mv = f"{unique_name}_src", f"{unique_name}_tgt", f"{unique_name}_mv"
    exec_all(
        ch_client,
        f"CREATE TABLE {src} (id UInt32, val String) ENGINE = MergeTree ORDER BY id",
        f"INSERT INTO {src} VALUES (1,'a'),(2,'b'),(3,'c')",
    )
    time.sleep(2)
    exec_all(
        ch_client,
        f"CREATE TABLE {tgt} (id UInt32, val String) ENGINE = MergeTree ORDER BY id",
        f"CREATE MATERIALIZED VIEW {mv} TO {tgt} POPULATE AS SELECT id, val FROM {src}",
    )

    findings = run_live_checks(ch_client, "default", mv, ratio_threshold=0.8)
    assert [f for f in findings if f.rule == "backfill-plausibility"] == []


def test_list_and_fetch_helpers(ch_client: HttpClient, unique_name: str) -> None:
    src, mv = f"{unique_name}_src", f"{unique_name}_mv"
    exec_all(
        ch_client,
        f"CREATE TABLE {src} (id UInt32) ENGINE = MergeTree ORDER BY id",
        f"CREATE MATERIALIZED VIEW {mv} ENGINE = MergeTree ORDER BY id AS SELECT id FROM {src}",
    )
    assert mv in list_materialized_views(ch_client, "default")
    assert fetch_mv_info(ch_client, "default", "no_such_view_xyz") is None
    assert fetch_table_info(ch_client, "default", "no_such_table_xyz") is None
