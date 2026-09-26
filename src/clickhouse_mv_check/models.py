"""Data shapes shared by the static parser, the live checks, and the CLI."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """How serious a finding is."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class Finding:
    """One concrete thing wrong (or worth checking) about a materialized view.

    Every finding names the mechanism, not just the pattern: what will actually
    go wrong at runtime, and what to do about it.
    """

    rule: str
    severity: Severity
    view: str
    message: str
    consequence: str
    fix: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity.value,
            "view": self.view,
            "message": self.message,
            "consequence": self.consequence,
            "fix": self.fix,
        }


@dataclass(frozen=True, slots=True)
class SelectColumn:
    """One output column of a view's SELECT list.

    ``aggregate_function`` is the outermost function name (e.g. ``"sum"``,
    ``"sumState"``, ``"count"``) when the column is a direct call to one,
    otherwise ``None``.
    """

    name: str
    aggregate_function: str | None


@dataclass(frozen=True, slots=True)
class MaterializedView:
    """A parsed ``CREATE MATERIALIZED VIEW`` definition.

    Parsed either from a ``.sql`` source file (in which case ``populate`` is
    trustworthy) or reconstructed from ``system.tables`` on a live server (in
    which case it is not — see the README).
    """

    name: str
    database: str | None
    target_table: str | None
    has_explicit_to: bool
    populate: bool
    select_sql: str
    source_table: str | None
    joined_tables: tuple[str, ...]
    subquery_tables: tuple[str, ...]
    select_columns: tuple[SelectColumn, ...]


@dataclass(frozen=True, slots=True)
class TableColumn:
    name: str
    type: str


@dataclass(frozen=True, slots=True)
class TableInfo:
    """A parsed ``CREATE TABLE`` definition — just enough to cross-check a
    materialized view that writes into it."""

    name: str
    engine: str | None
    columns: tuple[TableColumn, ...]

    def column(self, name: str) -> TableColumn | None:
        for c in self.columns:
            if c.name == name:
                return c
        return None
