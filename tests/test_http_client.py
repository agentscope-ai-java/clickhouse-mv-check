from __future__ import annotations

import pytest

from clickhouse_mv_check.http_client import ClickHouseHTTPError, HttpClient


def test_select_round_trips_through_json_compact(ch_client: HttpClient) -> None:
    assert ch_client.query("SELECT 1, 'x'") == [(1, "x")]


def test_ddl_and_insert_are_sent_without_a_format_clause(ch_client: HttpClient, unique_name: str) -> None:
    # This is the exact bug this client was built to avoid: appending FORMAT
    # to a DDL/INSERT statement breaks it (confirmed: FORMAT after an inline
    # VALUES list is parsed as the *input* format and raises CANNOT_PARSE_INPUT).
    ch_client.query(f"CREATE TABLE {unique_name} (id UInt32) ENGINE = Memory")
    ch_client.query(f"INSERT INTO {unique_name} VALUES (1), (2), (3)")
    assert ch_client.query(f"SELECT count() FROM {unique_name}") == [(3,)]


def test_bad_query_raises_clickhouse_http_error(ch_client: HttpClient) -> None:
    with pytest.raises(ClickHouseHTTPError):
        ch_client.query("SELECT * FROM this_table_does_not_exist_xyz")
