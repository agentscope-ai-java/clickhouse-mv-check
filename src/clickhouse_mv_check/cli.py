"""Command-line entry point.

    clickhouse-mv-check static schema/*.sql
    clickhouse-mv-check live --url http://localhost:8123 --user default --password secret --database default --all

Exit codes: 0 = clean, 1 = findings reported, 2 = usage/connection error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from .http_client import ClickHouseHTTPError, HttpClient
from .live import list_materialized_views, run_live_checks
from .models import Finding, Severity
from .sql_parse import parse_source
from .static_checks import run_static_checks

_SEVERITY_ORDER = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2}


def _print_findings(findings: list[Finding], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps([f.to_dict() for f in findings], indent=2))
        return
    if not findings:
        print("No findings.")
        return
    for f in findings:
        print(f"[{f.severity.value.upper()}] {f.view}: {f.rule}")
        print(f"  {f.message}")
        print(f"  Consequence: {f.consequence}")
        print(f"  Fix: {f.fix}")
        print()


def _run_static(args: argparse.Namespace) -> int:
    all_findings: list[Finding] = []
    for path in args.files:
        try:
            with open(path) as handle:
                text = handle.read()
        except OSError as exc:
            print(f"error: could not read {path}: {exc}", file=sys.stderr)
            return 2
        views, tables = parse_source(text)
        tables_by_name = {t.name: t for t in tables}
        for mv in views:
            target = tables_by_name.get(mv.target_table.rsplit(".", 1)[-1]) if mv.target_table else None
            all_findings.extend(run_static_checks(mv, target))
    _print_findings(all_findings, as_json=args.json)
    return 1 if any(f.severity != Severity.INFO for f in all_findings) else 0


def _run_live(args: argparse.Namespace) -> int:
    client = HttpClient(args.url, user=args.user, password=args.password, database=args.database)
    try:
        if args.all:
            names = list_materialized_views(client, args.database)
        else:
            names = [args.view]
        all_findings: list[Finding] = []
        for name in names:
            all_findings.extend(
                run_live_checks(client, args.database, name, ratio_threshold=args.backfill_ratio_threshold)
            )
    except (ClickHouseHTTPError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_findings(all_findings, as_json=args.json)
    return 1 if any(f.severity != Severity.INFO for f in all_findings) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clickhouse-mv-check")
    subparsers = parser.add_subparsers(dest="command", required=True)

    static_parser = subparsers.add_parser("static", help="check .sql files")
    static_parser.add_argument("files", nargs="+")
    static_parser.add_argument("--json", action="store_true")
    static_parser.set_defaults(func=_run_static)

    live_parser = subparsers.add_parser("live", help="check a running server")
    live_parser.add_argument("--url", required=True, help="e.g. http://localhost:8123")
    live_parser.add_argument("--user", default="default")
    live_parser.add_argument("--password", default="")
    live_parser.add_argument("--database", default="default")
    target = live_parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--view", help="check a single materialized view by name")
    target.add_argument("--all", action="store_true", help="check every materialized view in --database")
    live_parser.add_argument("--json", action="store_true")
    live_parser.add_argument(
        "--backfill-ratio-threshold",
        type=float,
        default=0.8,
        help="flag when target rows / source rows falls below this (default 0.8)",
    )
    live_parser.set_defaults(func=_run_live)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
