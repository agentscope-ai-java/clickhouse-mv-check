"""A small, deliberately narrow SQL reader.

This is not a general SQL parser. It knows exactly two shapes: ClickHouse's
``CREATE MATERIALIZED VIEW`` and ``CREATE TABLE`` statements, in either the
form a person writes by hand or the form ``SHOW CREATE TABLE`` prints back.
Anything else in a ``.sql`` file (comments, other DDL, blank lines) is
tolerated and ignored.

The one thing this module intentionally cannot promise: whether ``POPULATE``
was used. ClickHouse only records it as an action taken at creation time, not
as persisted metadata (confirmed by capturing ``SHOW CREATE TABLE`` on a view
created with ``POPULATE`` — the keyword does not come back). So
``MaterializedView.populate`` is only meaningful when parsed from the
original ``.sql`` source that was actually run, never from a live
``SHOW CREATE`` / ``system.tables.as_select`` snapshot. Live checks fill that
gap a different way: by comparing row counts (see ``live.py``).
"""

from __future__ import annotations

import re

from .models import MaterializedView, SelectColumn, TableColumn, TableInfo

_IDENT = r"`?\"?[\w.]+`?\"?"

_MV_HEAD = re.compile(
    r"CREATE\s+MATERIALIZED\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    rf"(?P<name>{_IDENT})",
    re.IGNORECASE,
)
_TO_CLAUSE = re.compile(rf"\bTO\s+(?P<target>{_IDENT})", re.IGNORECASE)
_POPULATE = re.compile(r"\bPOPULATE\b", re.IGNORECASE)
_AS_SELECT = re.compile(r"\bAS\s+(?P<select>SELECT\b.*)$", re.IGNORECASE | re.DOTALL)

_CREATE_TABLE_HEAD = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    rf"(?P<name>{_IDENT})\s*\((?P<cols>.*?)\)\s*ENGINE\s*=\s*(?P<engine>\w+)",
    re.IGNORECASE | re.DOTALL,
)

_JOIN_KEYWORD = re.compile(
    r"\b(?:GLOBAL\s+)?(?:INNER|LEFT|RIGHT|FULL|CROSS)?\s*(?:OUTER\s+)?JOIN\s+"
    rf"(?P<table>{_IDENT})",
    re.IGNORECASE,
)
_FROM_KEYWORD = re.compile(rf"\bFROM\s+(?P<table>{_IDENT})", re.IGNORECASE)

# ClickHouse aggregate combinators: sumState, avgMerge, countIf, uniqState, ...
# We only need to recognise "<base><Combinator>" well enough to strip it back
# to the base function name for the -State/-Merge cross-check.
_AGG_STATE_SUFFIX = re.compile(r"^(?P<base>\w+?)State$")


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return sql


def split_statements(sql: str) -> list[str]:
    """Split a ``.sql`` file into individual statements on top-level ``;``.

    Semicolons inside parentheses (subqueries, column lists) are not split
    points. Good enough for DDL; not a general SQL tokenizer.
    """
    sql = _strip_comments(sql)
    statements: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in sql:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == ";" and depth == 0:
            stmt = "".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
            continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def _depth_at(s: str, pos: int) -> int:
    return s.count("(", 0, pos) - s.count(")", 0, pos)


def _strip_ident(ident: str) -> str:
    return ident.strip().strip("`").strip('"')


def split_top_level(s: str, sep: str = ",") -> list[str]:
    """Split ``s`` on ``sep`` at paren-depth 0."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def parse_select_sources(select_sql: str) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    """Return ``(source_table, joined_tables, subquery_tables)`` for a
    ``SELECT ...`` body.

    ``source_table`` is the table named in the first top-level ``FROM``.
    ``joined_tables`` are tables pulled in by a top-level ``JOIN``.
    ``subquery_tables`` are tables named in a ``FROM``/``JOIN`` nested inside
    parentheses (a subquery) — these are read at whatever state they are in
    when the view's trigger fires, not re-evaluated later.
    """
    from_matches = [
        (m.start(), _strip_ident(m.group("table"))) for m in _FROM_KEYWORD.finditer(select_sql)
    ]
    join_matches = [
        (m.start(), _strip_ident(m.group("table"))) for m in _JOIN_KEYWORD.finditer(select_sql)
    ]

    top_level_from = [pos_table for pos_table in from_matches if _depth_at(select_sql, pos_table[0]) == 0]
    nested_from = [pos_table for pos_table in from_matches if _depth_at(select_sql, pos_table[0]) > 0]

    top_level_join = [pos_table for pos_table in join_matches if _depth_at(select_sql, pos_table[0]) == 0]
    nested_join = [pos_table for pos_table in join_matches if _depth_at(select_sql, pos_table[0]) > 0]

    source_table = top_level_from[0][1] if top_level_from else None
    joined_tables = tuple(t for _, t in top_level_join)

    subquery_tables = tuple(
        dict.fromkeys(t for _, t in (*nested_from, *nested_join))  # dedupe, keep order
    )

    return source_table, joined_tables, subquery_tables


def _select_list_text(select_sql: str) -> str:
    """The text between ``SELECT`` and its first top-level ``FROM``."""
    m = re.match(r"SELECT\s+(?:DISTINCT\s+)?", select_sql, re.IGNORECASE)
    start = m.end() if m else len("SELECT")
    from_matches = [m for m in _FROM_KEYWORD.finditer(select_sql) if _depth_at(select_sql, m.start()) == 0]
    end = from_matches[0].start() if from_matches else len(select_sql)
    return select_sql[start:end]


_FUNC_CALL = re.compile(r"^(?P<func>\w+)\s*\((?P<args>.*)\)$", re.DOTALL)
_ALIAS = re.compile(r"\bAS\s+(?P<alias>`?\w+`?)\s*$", re.IGNORECASE)


def parse_select_columns(select_sql: str) -> tuple[SelectColumn, ...]:
    """Parse the top-level SELECT list into (output name, aggregate fn) pairs."""
    list_text = _select_list_text(select_sql)
    columns: list[SelectColumn] = []
    for item in split_top_level(list_text, ","):
        alias_match = _ALIAS.search(item)
        if alias_match:
            alias = _strip_ident(alias_match.group("alias"))
            expr = item[: alias_match.start()].strip()
        else:
            expr = item.strip()
            alias = None

        func_match = _FUNC_CALL.match(expr)
        if func_match:
            func_name = func_match.group("func")
            name = alias or func_name
            columns.append(SelectColumn(name=name, aggregate_function=func_name))
        else:
            # A bare column reference, e.g. "key" or "t.key".
            name = alias or _strip_ident(expr).rsplit(".", 1)[-1]
            columns.append(SelectColumn(name=name, aggregate_function=None))
    return tuple(columns)


def parse_materialized_view(stmt: str) -> MaterializedView | None:
    """Parse one ``CREATE MATERIALIZED VIEW`` statement.

    Returns ``None`` if ``stmt`` isn't a materialized view definition.
    """
    head_match = _MV_HEAD.search(stmt)
    if not head_match:
        return None

    as_select_match = _AS_SELECT.search(stmt)
    if not as_select_match:
        return None

    full_name = _strip_ident(head_match.group("name"))
    database_part, _, name = full_name.rpartition(".")
    database: str | None = database_part or None
    if database is None:
        name = full_name

    head = stmt[head_match.end() : as_select_match.start()]
    select_sql = as_select_match.group("select").strip()

    to_match = _TO_CLAUSE.search(head)
    target_table = _strip_ident(to_match.group("target")) if to_match else None
    has_explicit_to = to_match is not None
    populate = bool(_POPULATE.search(head))

    source_table, joined_tables, subquery_tables = parse_select_sources(select_sql)
    select_columns = parse_select_columns(select_sql)

    return MaterializedView(
        name=name,
        database=database,
        target_table=target_table,
        has_explicit_to=has_explicit_to,
        populate=populate,
        select_sql=select_sql,
        source_table=source_table,
        joined_tables=joined_tables,
        subquery_tables=subquery_tables,
        select_columns=select_columns,
    )


def parse_create_table(stmt: str) -> TableInfo | None:
    """Parse one ``CREATE TABLE`` statement's name, engine and column types."""
    match = _CREATE_TABLE_HEAD.search(stmt)
    if not match:
        return None

    full_name = _strip_ident(match.group("name"))
    _, _, name = full_name.rpartition(".")
    name = name or full_name
    engine = match.group("engine")

    columns: list[TableColumn] = []
    for col_def in split_top_level(match.group("cols"), ","):
        col_def = col_def.strip()
        if not col_def or re.match(r"^(INDEX|CONSTRAINT|PROJECTION)\b", col_def, re.IGNORECASE):
            continue
        parts = col_def.split(None, 1)
        if len(parts) != 2:
            continue
        col_name = _strip_ident(parts[0])
        col_type = parts[1].strip()
        columns.append(TableColumn(name=col_name, type=col_type))

    return TableInfo(name=name, engine=engine, columns=tuple(columns))


def parse_source(sql_text: str) -> tuple[list[MaterializedView], list[TableInfo]]:
    """Parse every ``CREATE MATERIALIZED VIEW`` and ``CREATE TABLE`` statement
    found in ``sql_text`` (typically the contents of one ``.sql`` file)."""
    views: list[MaterializedView] = []
    tables: list[TableInfo] = []
    for stmt in split_statements(sql_text):
        mv = parse_materialized_view(stmt)
        if mv is not None:
            views.append(mv)
            continue
        table = parse_create_table(stmt)
        if table is not None:
            tables.append(table)
    return views, tables
