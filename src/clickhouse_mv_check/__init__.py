"""clickhouse-mv-check: catch materialized-view bugs before they go silent.

A materialized view in ClickHouse is an insert trigger, not a maintained
view — see the README for a real transcript proving each consequence of
that. This package provides static checks over ``CREATE MATERIALIZED VIEW``
definitions and live checks against a running server.
"""

from .live import (
    ClickHouseClient,
    LiveMVInfo,
    check_backfill_plausibility,
    fetch_mv_info,
    fetch_table_info,
    list_materialized_views,
    run_live_checks,
)
from .models import Finding, MaterializedView, SelectColumn, Severity, TableColumn, TableInfo
from .sql_parse import parse_create_table, parse_materialized_view, parse_source
from .static_checks import (
    check_aggregate_mismatch,
    check_join_or_subquery,
    check_missing_to,
    check_populate,
    run_static_checks,
)

__all__ = [
    "ClickHouseClient",
    "Finding",
    "LiveMVInfo",
    "MaterializedView",
    "SelectColumn",
    "Severity",
    "TableColumn",
    "TableInfo",
    "check_aggregate_mismatch",
    "check_backfill_plausibility",
    "check_join_or_subquery",
    "check_missing_to",
    "check_populate",
    "fetch_mv_info",
    "fetch_table_info",
    "list_materialized_views",
    "parse_create_table",
    "parse_materialized_view",
    "parse_source",
    "run_live_checks",
    "run_static_checks",
]
