"""Static checks over a parsed ``CREATE MATERIALIZED VIEW`` definition.

Every rule here was validated against a real ClickHouse server before being
written down (see ``examples/reproduce.sh`` and the README's transcript) —
none of it is guessed from the documentation alone.
"""

from __future__ import annotations

from .models import Finding, MaterializedView, Severity, TableInfo

_AGGREGATING_ENGINES = {"AggregatingMergeTree", "ReplicatedAggregatingMergeTree"}


def _short(name: str | None) -> str | None:
    if name is None:
        return None
    return name.rsplit(".", 1)[-1]


def check_join_or_subquery(mv: MaterializedView) -> Finding | None:
    """Flag a JOIN or subquery that reads a table other than the source.

    Confirmed live: a materialized view is a trigger on inserts to its
    *source* table. A joined or sub-queried table is read as whatever state
    it happens to be in at the moment the trigger fires — not the state it
    will eventually reach. In our reproduction, an order inserted for a
    customer that did not yet exist in the joined ``customers`` table got a
    permanently empty name, even though the customer was inserted moments
    later; a second order for the same customer, inserted after, joined
    correctly. Nothing re-evaluates the first row.
    """
    source = _short(mv.source_table)
    foreign = sorted(
        {t for t in (*mv.joined_tables, *mv.subquery_tables) if _short(t) and _short(t) != source}
    )
    if not foreign:
        return None

    return Finding(
        rule="join-reads-foreign-table",
        severity=Severity.WARNING,
        view=mv.name,
        message=(
            f"SELECT reads {', '.join(foreign)} via JOIN/subquery in addition to the "
            f"source table {mv.source_table!r}."
        ),
        consequence=(
            "The view only re-runs its SELECT on the block just inserted into the source "
            "table; the joined/sub-queried table is read at whatever state it is in at that "
            "instant, not re-evaluated when that table changes afterwards. A row inserted "
            "into the source before its match exists on the other side is written with a "
            "missing/empty join result forever — confirmed by reproduction, not assumed."
        ),
        fix=(
            "Make sure both sides are present before the source insert fires (load the "
            "dimension first, or put both in one batch), keep the lookup as a Dictionary or "
            "`Join`-engine table that is always ahead of the fact table, or replace the "
            "push-based MV with a scheduled INSERT ... SELECT / refreshable materialized "
            "view that re-reads both tables in full on each run."
        ),
    )


def check_populate(mv: MaterializedView) -> Finding | None:
    """Flag ``POPULATE`` usage.

    Confirmed live: rows already in the source table before ``CREATE
    MATERIALIZED VIEW`` are invisible to the view unless POPULATE (or a
    manual backfill) is used — and POPULATE is not recorded anywhere once it
    runs; ``SHOW CREATE TABLE`` on a view created with POPULATE does not
    mention it. So this check only fires when reading the original ``.sql``
    source that was actually executed, not a live snapshot.
    """
    if not mv.populate:
        return None
    return Finding(
        rule="populate-used",
        severity=Severity.WARNING,
        view=mv.name,
        message="View is created with POPULATE.",
        consequence=(
            "POPULATE backfills existing rows via a snapshot SELECT, but ClickHouse's own docs "
            "describe rows inserted into the source while POPULATE is running as a race that can "
            "be missed. Just as importantly, POPULATE is a one-time action, not persisted state: "
            "once it has run, nothing in the view's metadata (including SHOW CREATE TABLE) shows "
            "that it happened or that it succeeded, so this exact source file can't be used later "
            "to confirm the backfill actually took place."
        ),
        fix=(
            "Prefer creating the view without POPULATE, then running an explicit, auditable "
            "`INSERT INTO target SELECT ... FROM source WHERE <cutoff before the view existed>` "
            "as a separate, re-runnable step you can verify with a row count afterwards."
        ),
    )


def check_missing_to(mv: MaterializedView) -> Finding | None:
    """Flag a materialized view with no explicit ``TO`` table.

    Confirmed live: without TO, ClickHouse creates a hidden inner table
    (visible in ``system.tables.target_table`` as ``.inner_id.<uuid>``) that
    doesn't show up in normal listings and is dropped silently along with the
    view.
    """
    if mv.has_explicit_to:
        return None
    return Finding(
        rule="missing-to-table",
        severity=Severity.WARNING,
        view=mv.name,
        message="View has no explicit TO table; ClickHouse will create a hidden inner table.",
        consequence=(
            "The data lives in an auto-named `.inner_id.<uuid>` table that isn't listed by "
            "normal tooling, can't have its engine or settings altered independently of the "
            "view, and is dropped without confirmation the moment the view is dropped — there "
            "is no separate 'drop the view but keep the data' step."
        ),
        fix="Always declare an explicit target: `CREATE MATERIALIZED VIEW ... TO db.target ...`.",
    )


def check_aggregate_mismatch(mv: MaterializedView, target: TableInfo | None) -> list[Finding]:
    """Flag aggregate states that don't match the target table's engine.

    Confirmed live: an ``AggregatingMergeTree`` column declared as a plain
    numeric type (not ``AggregateFunction(...)``) does not error when a
    materialized view writes a plain ``sum(x)`` into it — it merges as if it
    were a normal MergeTree column, i.e. on merge it just keeps one part's
    value instead of combining them. Two inserts of a plain sum, 10 and 20,
    read back as 30 before any merge and 10 after ``OPTIMIZE ... FINAL`` —
    silently wrong, no error anywhere.
    """
    if target is None or target.engine not in _AGGREGATING_ENGINES:
        return []

    findings: list[Finding] = []
    for col in mv.select_columns:
        if col.aggregate_function is None:
            continue
        target_col = target.column(col.name)
        if target_col is None:
            continue
        col_type = target_col.type.strip()
        is_state_fn = col.aggregate_function.endswith("State")
        declares_aggregate_function = col_type.startswith("AggregateFunction(") or col_type.startswith(
            "SimpleAggregateFunction("
        )

        if not is_state_fn and not declares_aggregate_function:
            findings.append(
                Finding(
                    rule="aggregate-state-mismatch",
                    severity=Severity.ERROR,
                    view=mv.name,
                    message=(
                        f"Target column `{col.name}` is `{col_type}` (not AggregateFunction) but the "
                        f"view writes the final value of `{col.aggregate_function}(...)`, into an "
                        f"{target.engine} table."
                    ),
                    consequence=(
                        "AggregatingMergeTree only combines columns declared as AggregateFunction/"
                        "SimpleAggregateFunction on merge. A plain-typed column just gets one part's "
                        "row kept and the other discarded when parts merge — reproduced: sum() results "
                        "of 10 and 20 for the same key read back as 30 before a merge and 10 after "
                        "`OPTIMIZE ... FINAL`, with no error at any point."
                    ),
                    fix=(
                        f"Declare `{col.name} AggregateFunction({col.aggregate_function.replace('State', '') or 'sum'}, ...)` "
                        f"on the target table and write `{col.aggregate_function if is_state_fn else col.aggregate_function + 'State'}"
                        f"(...)` from the view; read it back with the matching `...Merge` combinator."
                    ),
                )
            )
        elif is_state_fn and col_type.startswith("AggregateFunction("):
            base_fn = col.aggregate_function[: -len("State")]
            declared_fn = col_type[len("AggregateFunction(") :].split(",", 1)[0].strip()
            if declared_fn and declared_fn != base_fn:
                findings.append(
                    Finding(
                        rule="aggregate-state-mismatch",
                        severity=Severity.ERROR,
                        view=mv.name,
                        message=(
                            f"Target column `{col.name}` is `AggregateFunction({declared_fn}, ...)` but "
                            f"the view writes `{col.aggregate_function}(...)`."
                        ),
                        consequence=(
                            "Confirmed live: ClickHouse actually rejects this at CREATE MATERIALIZED VIEW "
                            "time ('Conversion from AggregateFunction(avg, UInt64) to "
                            "AggregateFunction(sum, UInt64) is not supported'), so this is not a silent "
                            "bug — but it still fails a deploy in a way that's cheaper to catch here, "
                            "against the source file, than by parsing that error out of a migration run."
                        ),
                        fix=f"Use `{declared_fn}State(...)` in the view, or change the column to AggregateFunction({base_fn}, ...).",
                    )
                )
    return findings


def run_static_checks(mv: MaterializedView, target: TableInfo | None = None) -> list[Finding]:
    """Run every static rule against one parsed materialized view."""
    findings: list[Finding] = []
    for finding in (
        check_join_or_subquery(mv),
        check_populate(mv),
        check_missing_to(mv),
    ):
        if finding is not None:
            findings.append(finding)
    findings.extend(check_aggregate_mismatch(mv, target))
    return findings
