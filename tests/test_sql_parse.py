"""Parser tests against real ``SHOW CREATE TABLE`` output captured from a
live ClickHouse 26.9.3 server (see the README transcript) — not invented
syntax guesses.
"""

from __future__ import annotations

from clickhouse_mv_check.sql_parse import (
    parse_create_table,
    parse_materialized_view,
    parse_select_columns,
    parse_select_sources,
    parse_source,
    split_statements,
    split_top_level,
)

MV_TO_TABLE = """
CREATE MATERIALIZED VIEW default.mv6b TO default.final6
(
    `id` UInt32,
    `val` UInt64
)
AS SELECT
    id,
    val * 10 AS val
FROM default.mid6
"""

MV_IMPLICIT_STORAGE = """
CREATE MATERIALIZED VIEW default.mv7
(
    `id` UInt32,
    `val` UInt32
)
ENGINE = MergeTree
ORDER BY id
SETTINGS index_granularity = 8192
AS SELECT
    id,
    val
FROM default.src6
"""

MV_WITH_JOIN = """
CREATE MATERIALIZED VIEW default.mv2 TO default.orders_enriched
(
    `order_id` UInt32,
    `cust_id` UInt32,
    `name` String
)
AS SELECT
    o.order_id,
    o.cust_id,
    c.name
FROM default.orders AS o
LEFT JOIN default.customers AS c ON o.cust_id = c.cust_id
"""

MV_AGGREGATE_WRONG = """
CREATE MATERIALIZED VIEW default.mv5_wrong TO default.agg5_wrong
(
    `key` String,
    `amount` UInt64
)
AS SELECT
    key,
    sum(amount) AS amount
FROM default.src5
GROUP BY key
"""

TABLE_AGGREGATING_WRONG = """
CREATE TABLE default.agg5_wrong
(
    `key` String,
    `amount` UInt64
)
ENGINE = AggregatingMergeTree
ORDER BY key
SETTINGS index_granularity = 8192
"""


def test_parse_mv_with_explicit_to_table() -> None:
    mv = parse_materialized_view(MV_TO_TABLE)
    assert mv is not None
    assert mv.name == "mv6b"
    assert mv.database == "default"
    assert mv.has_explicit_to is True
    assert mv.target_table == "default.final6"
    assert mv.populate is False
    assert mv.source_table == "default.mid6"
    assert mv.joined_tables == ()
    assert mv.subquery_tables == ()


def test_parse_mv_without_to_table_is_implicit_storage() -> None:
    mv = parse_materialized_view(MV_IMPLICIT_STORAGE)
    assert mv is not None
    assert mv.has_explicit_to is False
    assert mv.target_table is None
    assert mv.source_table == "default.src6"


def test_parse_mv_with_join_reports_both_tables() -> None:
    mv = parse_materialized_view(MV_WITH_JOIN)
    assert mv is not None
    assert mv.source_table == "default.orders"
    assert mv.joined_tables == ("default.customers",)
    assert mv.subquery_tables == ()


def test_parse_select_columns_reports_aggregate_function() -> None:
    mv = parse_materialized_view(MV_AGGREGATE_WRONG)
    assert mv is not None
    names_and_funcs = [(c.name, c.aggregate_function) for c in mv.select_columns]
    assert names_and_funcs == [("key", None), ("amount", "sum")]


def test_parse_create_table_engine_and_columns() -> None:
    table = parse_create_table(TABLE_AGGREGATING_WRONG)
    assert table is not None
    assert table.name == "agg5_wrong"
    assert table.engine == "AggregatingMergeTree"
    assert table.column("amount") is not None
    assert table.column("amount").type == "UInt64"  # type: ignore[union-attr]
    assert table.column("missing") is None


def test_parse_source_finds_both_statements_in_one_file() -> None:
    views, tables = parse_source(MV_AGGREGATE_WRONG + ";\n" + TABLE_AGGREGATING_WRONG)
    assert len(views) == 1
    assert len(tables) == 1
    assert views[0].name == "mv5_wrong"
    assert tables[0].name == "agg5_wrong"


def test_populate_keyword_is_detected_before_as_select() -> None:
    sql = "CREATE MATERIALIZED VIEW mv TO tgt POPULATE AS SELECT id FROM src"
    mv = parse_materialized_view(sql)
    assert mv is not None
    assert mv.populate is True


def test_subquery_table_is_distinguished_from_top_level_join() -> None:
    sql = (
        "CREATE MATERIALIZED VIEW mv TO tgt AS SELECT id, "
        "(SELECT max(x) FROM other_table) AS m FROM src"
    )
    mv = parse_materialized_view(sql)
    assert mv is not None
    assert mv.source_table == "src"
    assert mv.joined_tables == ()
    assert mv.subquery_tables == ("other_table",)


def test_split_statements_ignores_semicolons_inside_parens() -> None:
    sql = "CREATE TABLE t (a String DEFAULT 'x;y') ENGINE=Memory; SELECT 1;"
    statements = split_statements(sql)
    assert len(statements) == 2
    assert "x;y" in statements[0]


def test_split_top_level_respects_nesting() -> None:
    assert split_top_level("a, f(b, c), d") == ["a", "f(b, c)", "d"]


def test_parse_select_sources_returns_none_when_no_from() -> None:
    source, joined, subq = parse_select_sources("SELECT 1")
    assert source is None
    assert joined == ()
    assert subq == ()


def test_parse_select_columns_handles_bare_column_with_table_prefix() -> None:
    columns = parse_select_columns("SELECT o.order_id, o.cust_id FROM orders o")
    assert [c.name for c in columns] == ["order_id", "cust_id"]
    assert all(c.aggregate_function is None for c in columns)


def test_not_a_materialized_view_returns_none() -> None:
    assert parse_materialized_view("CREATE TABLE t (a Int) ENGINE=Memory") is None
    assert parse_create_table("CREATE MATERIALIZED VIEW mv AS SELECT 1") is None
