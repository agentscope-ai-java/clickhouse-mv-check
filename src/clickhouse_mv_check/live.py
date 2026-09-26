"""Checks that need a live connection: what a view's definition currently is,
and whether its row count still makes sense next to its source.

Zero runtime dependencies: this module never imports a ClickHouse driver.
Instead it accepts anything with a ``.query(sql) -> rows`` method — a
``clickhouse-connect`` client, a ``clickhouse-driver`` client wrapped to
expose ``.query``, this package's own ``http_client.HttpClient``, or a stub
in a test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from .models import Finding, MaterializedView, Severity, TableColumn, TableInfo
from .sql_parse import parse_select_columns, parse_select_sources
from .static_checks import run_static_checks

_IDENT_RE = re.compile(r"^\w+$")


class ClickHouseClient(Protocol):
    """The only contract this package needs from a client."""

    def query(self, sql: str) -> Any: ...


def _rows(result: Any) -> list[tuple[Any, ...]]:
    """Normalise the return value of ``client.query()`` into a list of row
    tuples, whatever shape the caller's client returns them in."""
    if hasattr(result, "result_rows"):  # clickhouse-connect QueryResult
        return [tuple(row) for row in result.result_rows]
    if hasattr(result, "rows") and not isinstance(result, (list, tuple)):
        return [tuple(row) for row in result.rows]
    return [tuple(row) for row in result]


def _check_ident(name: str, what: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"{what} {name!r} is not a plain identifier — refusing to interpolate it into SQL.")
    return name


@dataclass(frozen=True, slots=True)
class LiveMVInfo:
    """What ``system.tables`` knows about one materialized view right now."""

    name: str
    database: str
    target_database: str
    target_table: str
    as_select: str
    metadata_modification_time: str


def fetch_mv_info(client: ClickHouseClient, database: str, name: str) -> LiveMVInfo | None:
    """Look up one materialized view's current definition via
    ``system.tables``. Returns ``None`` if no such view exists."""
    _check_ident(database, "database")
    _check_ident(name, "view name")
    sql = (
        "SELECT target_database, target_table, as_select, metadata_modification_time "
        "FROM system.tables "
        f"WHERE database = '{database}' AND name = '{name}' AND engine = 'MaterializedView'"
    )
    rows = _rows(client.query(sql))
    if not rows:
        return None
    target_database, target_table, as_select, modified = rows[0]
    return LiveMVInfo(
        name=name,
        database=database,
        target_database=str(target_database),
        target_table=str(target_table),
        as_select=str(as_select),
        metadata_modification_time=str(modified),
    )


def list_materialized_views(client: ClickHouseClient, database: str) -> list[str]:
    """List every materialized view's name in ``database``."""
    _check_ident(database, "database")
    sql = f"SELECT name FROM system.tables WHERE database = '{database}' AND engine = 'MaterializedView'"
    return [str(row[0]) for row in _rows(client.query(sql))]


def fetch_table_info(client: ClickHouseClient, database: str, name: str) -> TableInfo | None:
    """Look up one table's engine and column types via ``system.tables`` /
    ``system.columns``."""
    _check_ident(database, "database")
    _check_ident(name, "table name")
    engine_rows = _rows(
        client.query(f"SELECT engine FROM system.tables WHERE database = '{database}' AND name = '{name}'")
    )
    if not engine_rows:
        return None
    engine = str(engine_rows[0][0])
    column_rows = _rows(
        client.query(
            "SELECT name, type FROM system.columns "
            f"WHERE database = '{database}' AND table = '{name}' ORDER BY position"
        )
    )
    columns = tuple(TableColumn(name=str(n), type=str(t)) for n, t in column_rows)
    return TableInfo(name=name, engine=engine, columns=columns)


def to_materialized_view(info: LiveMVInfo) -> MaterializedView:
    """Build the same model the static parser produces, from live metadata.

    ``populate`` is always ``False`` here — see the module docstring in
    ``sql_parse``: ClickHouse does not persist whether POPULATE was used, so
    a live snapshot can never answer that question. The backfill-plausibility
    check exists precisely to catch what this can't see.
    """
    source_table, joined_tables, subquery_tables = parse_select_sources(info.as_select)
    select_columns = parse_select_columns(info.as_select)
    has_explicit_to = not info.target_table.startswith(".inner")
    return MaterializedView(
        name=info.name,
        database=info.database,
        target_table=info.target_table if has_explicit_to else None,
        has_explicit_to=has_explicit_to,
        populate=False,
        select_sql=info.as_select,
        source_table=source_table,
        joined_tables=joined_tables,
        subquery_tables=subquery_tables,
        select_columns=select_columns,
    )


def check_backfill_plausibility(
    client: ClickHouseClient,
    info: LiveMVInfo,
    source_table: str,
    *,
    ratio_threshold: float = 0.8,
) -> Finding | None:
    """Does the target's row count plausibly match the source for the period
    since the view was created?

    Confirmed live: a view created over a source table that already had
    rows, without POPULATE, ends up with a target row count matching only
    the rows inserted *after* creation — while the source table keeps
    growing from its full history. We approximate "existed before the view"
    using ``system.parts.modification_time``, which is a part's own
    creation/last-write time, against the view's
    ``metadata_modification_time``. This is a heuristic, not a proof: heavy
    merging can shift a part's ``modification_time`` forward, and
    ``metadata_modification_time`` reflects the last DDL change, not
    necessarily the original creation, if the view was later altered.
    """
    _check_ident(info.database, "database")
    _check_ident(source_table, "source table")
    _check_ident(info.target_database, "target database")
    # info.target_table (and the timestamp we splice in below) both come from
    # system.tables metadata, not from untrusted input; it can legitimately
    # contain dots (a hidden `.inner_id.<uuid>` table), so it is backtick-quoted
    # below rather than passed through the plain-identifier check.

    total_rows, pre_existing_rows = _rows(
        client.query(
            "SELECT sum(rows), sumIf(rows, modification_time <= '"
            + info.metadata_modification_time
            + "') FROM system.parts WHERE database = '"
            + info.database
            + "' AND table = '"
            + source_table
            + "' AND active"
        )
    )[0]
    total_rows = int(total_rows or 0)
    pre_existing_rows = int(pre_existing_rows or 0)

    if pre_existing_rows == 0 or total_rows == 0:
        # Nothing predates the view (or the source is empty) — no useful signal.
        return None

    target_sql = f"SELECT count() FROM `{info.target_database}`.`{info.target_table}`"
    target_rows = int(_rows(client.query(target_sql))[0][0])

    ratio = target_rows / total_rows
    if ratio >= ratio_threshold:
        return None

    return Finding(
        rule="backfill-plausibility",
        severity=Severity.WARNING,
        view=info.name,
        message=(
            f"Source `{source_table}` has {total_rows} rows ({pre_existing_rows} of them in parts "
            f"older than the view's last metadata change at {info.metadata_modification_time}), but "
            f"target `{info.target_table}` only has {target_rows} rows ({ratio:.0%} of source)."
        ),
        consequence=(
            "This matches the exact signature of a materialized view created without POPULATE and "
            "never backfilled: rows that existed before the view was created are invisible to it "
            "forever, and only look this way from a row-count comparison — the view itself never "
            "errors."
        ),
        fix=(
            "Backfill explicitly: `INSERT INTO target SELECT ... FROM source WHERE <condition "
            "matching rows written before the view existed>`, then re-run this check to confirm the "
            "counts converge."
        ),
    )


def run_live_checks(
    client: ClickHouseClient,
    database: str,
    name: str,
    *,
    ratio_threshold: float = 0.8,
) -> list[Finding]:
    """Run every check this package has against one live materialized view:
    the same static rules (built from ``system.tables.as_select``, which
    cannot reveal POPULATE — see ``check_backfill_plausibility`` for how that
    gap is covered) plus the backfill-plausibility row-count check."""
    info = fetch_mv_info(client, database, name)
    if info is None:
        raise ValueError(f"No materialized view named {name!r} in database {database!r}.")

    mv = to_materialized_view(info)
    target: TableInfo | None = None
    if mv.has_explicit_to and mv.target_table:
        target_db, _, target_name = mv.target_table.rpartition(".")
        target = fetch_table_info(client, target_db or database, target_name)

    findings = run_static_checks(mv, target)

    if mv.source_table:
        source_db, _, source_name = mv.source_table.rpartition(".")
        backfill_finding = check_backfill_plausibility(
            client, info, source_name, ratio_threshold=ratio_threshold
        )
        if backfill_finding is not None:
            findings.append(backfill_finding)

    return findings
