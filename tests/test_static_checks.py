"""Static rule tests, built on definitions that were actually run against a
live ClickHouse server (see the README transcript for the raw reproduction).
"""

from __future__ import annotations

from clickhouse_mv_check.models import Severity
from clickhouse_mv_check.sql_parse import parse_create_table, parse_materialized_view
from clickhouse_mv_check.static_checks import run_static_checks

MV_CLEAN = """
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

MV_WITH_JOIN = """
CREATE MATERIALIZED VIEW default.mv2 TO default.orders_enriched
AS SELECT
    o.order_id,
    o.cust_id,
    c.name
FROM default.orders AS o
LEFT JOIN default.customers AS c ON o.cust_id = c.cust_id
"""

MV_NO_TO = """
CREATE MATERIALIZED VIEW default.mv7
ENGINE = MergeTree
ORDER BY id
AS SELECT id, val FROM default.src6
"""

MV_POPULATE = "CREATE MATERIALIZED VIEW mv TO tgt POPULATE AS SELECT id FROM src"

MV_AGGREGATE_WRONG = """
CREATE MATERIALIZED VIEW default.mv5_wrong TO default.agg5_wrong
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
"""

MV_AGGREGATE_RIGHT = (
    "CREATE MATERIALIZED VIEW mv5_right TO agg5_right AS "
    "SELECT key, sumState(amount) AS amount FROM src5 GROUP BY key"
)

TABLE_AGGREGATING_RIGHT = (
    "CREATE TABLE agg5_right (key String, amount AggregateFunction(sum, UInt64)) "
    "ENGINE = AggregatingMergeTree ORDER BY key"
)

MV_AGGREGATE_STATE_FN_MISMATCH = (
    "CREATE MATERIALIZED VIEW mv5_mismatch TO agg5_mismatch AS "
    "SELECT key, avgState(amount) AS amount FROM src5 GROUP BY key"
)

TABLE_AGGREGATING_STATE_FN_MISMATCH = (
    "CREATE TABLE agg5_mismatch (key String, amount AggregateFunction(sum, UInt64)) "
    "ENGINE = AggregatingMergeTree ORDER BY key"
)


def _mv(sql: str):
    mv = parse_materialized_view(sql)
    assert mv is not None
    return mv


def _table(sql: str):
    table = parse_create_table(sql)
    assert table is not None
    return table


def test_clean_definition_has_no_findings() -> None:
    findings = run_static_checks(_mv(MV_CLEAN))
    assert findings == []


def test_join_on_foreign_table_is_flagged() -> None:
    findings = run_static_checks(_mv(MV_WITH_JOIN))
    rules = [f.rule for f in findings]
    assert "join-reads-foreign-table" in rules
    finding = next(f for f in findings if f.rule == "join-reads-foreign-table")
    assert finding.severity == Severity.WARNING
    assert "customers" in finding.message


def test_missing_to_table_is_flagged() -> None:
    findings = run_static_checks(_mv(MV_NO_TO))
    assert any(f.rule == "missing-to-table" for f in findings)


def test_populate_is_flagged() -> None:
    findings = run_static_checks(_mv(MV_POPULATE))
    assert any(f.rule == "populate-used" for f in findings)


def test_aggregate_state_mismatch_flags_plain_column_under_aggregating_engine() -> None:
    mv = _mv(MV_AGGREGATE_WRONG)
    target = _table(TABLE_AGGREGATING_WRONG)
    findings = run_static_checks(mv, target)
    agg_findings = [f for f in findings if f.rule == "aggregate-state-mismatch"]
    assert len(agg_findings) == 1
    assert agg_findings[0].severity == Severity.ERROR
    assert "amount" in agg_findings[0].message


def test_correct_state_merge_pairing_is_not_flagged() -> None:
    mv = _mv(MV_AGGREGATE_RIGHT)
    target = _table(TABLE_AGGREGATING_RIGHT)
    findings = run_static_checks(mv, target)
    assert [f for f in findings if f.rule == "aggregate-state-mismatch"] == []


def test_mismatched_state_function_name_is_flagged() -> None:
    mv = _mv(MV_AGGREGATE_STATE_FN_MISMATCH)
    target = _table(TABLE_AGGREGATING_STATE_FN_MISMATCH)
    findings = run_static_checks(mv, target)
    agg_findings = [f for f in findings if f.rule == "aggregate-state-mismatch"]
    assert len(agg_findings) == 1


def test_aggregate_check_is_skipped_without_target_info() -> None:
    mv = _mv(MV_AGGREGATE_WRONG)
    findings = run_static_checks(mv, target=None)
    assert [f for f in findings if f.rule == "aggregate-state-mismatch"] == []


def test_aggregate_check_ignores_non_aggregating_engines() -> None:
    mv = _mv(MV_AGGREGATE_WRONG)
    plain_table = _table(
        "CREATE TABLE agg5_wrong (key String, amount UInt64) ENGINE = MergeTree ORDER BY key"
    )
    findings = run_static_checks(mv, plain_table)
    assert [f for f in findings if f.rule == "aggregate-state-mismatch"] == []
