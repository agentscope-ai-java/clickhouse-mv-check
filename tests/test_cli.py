from __future__ import annotations

import json
from pathlib import Path

import pytest

from clickhouse_mv_check.cli import main
from clickhouse_mv_check.http_client import HttpClient

from .conftest import CH_DATABASE, CH_PASSWORD, CH_URL, CH_USER, exec_all

MV_WITH_JOIN_AND_TABLE = """
CREATE TABLE customers (cust_id UInt32, name String) ENGINE = MergeTree ORDER BY cust_id;
CREATE TABLE orders (order_id UInt32, cust_id UInt32) ENGINE = MergeTree ORDER BY order_id;
CREATE TABLE orders_enriched (order_id UInt32, cust_id UInt32, name String) ENGINE = MergeTree ORDER BY order_id;
CREATE MATERIALIZED VIEW mv2 TO orders_enriched AS
SELECT o.order_id, o.cust_id, c.name
FROM orders AS o
LEFT JOIN customers AS c ON o.cust_id = c.cust_id;
"""

CLEAN_SQL = """
CREATE TABLE src (id UInt32) ENGINE = MergeTree ORDER BY id;
CREATE TABLE tgt (id UInt32) ENGINE = MergeTree ORDER BY id;
CREATE MATERIALIZED VIEW mv TO tgt AS SELECT id FROM src;
"""


def test_static_command_reports_findings_and_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sql_file = tmp_path / "schema.sql"
    sql_file.write_text(MV_WITH_JOIN_AND_TABLE)

    exit_code = main(["static", str(sql_file), "--json"])

    out = capsys.readouterr().out
    findings = json.loads(out)
    assert any(f["rule"] == "join-reads-foreign-table" for f in findings)
    assert exit_code == 1


def test_static_command_clean_definition_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sql_file = tmp_path / "schema.sql"
    sql_file.write_text(CLEAN_SQL)

    exit_code = main(["static", str(sql_file), "--json"])

    findings = json.loads(capsys.readouterr().out)
    assert findings == []
    assert exit_code == 0


def test_static_command_human_readable_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sql_file = tmp_path / "schema.sql"
    sql_file.write_text(MV_WITH_JOIN_AND_TABLE)

    main(["static", str(sql_file)])

    out = capsys.readouterr().out
    assert "join-reads-foreign-table" in out
    assert "Consequence:" in out
    assert "Fix:" in out


def test_live_command_all_flag(
    ch_client: HttpClient, unique_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    src, mv = f"{unique_name}_src", f"{unique_name}_mv"
    exec_all(
        ch_client,
        f"CREATE TABLE {src} (id UInt32) ENGINE = MergeTree ORDER BY id",
        f"CREATE MATERIALIZED VIEW {mv} ENGINE = MergeTree ORDER BY id AS SELECT id FROM {src}",
    )

    exit_code = main(
        [
            "live",
            "--url",
            CH_URL,
            "--user",
            CH_USER,
            "--password",
            CH_PASSWORD,
            "--database",
            CH_DATABASE,
            "--view",
            mv,
            "--json",
        ]
    )

    findings = json.loads(capsys.readouterr().out)
    assert any(f["rule"] == "missing-to-table" for f in findings)
    assert exit_code == 1


def test_live_command_reports_connection_error(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        [
            "live",
            "--url",
            "http://localhost:1",  # nothing listens here
            "--view",
            "whatever",
        ]
    )
    assert exit_code == 2
    assert "error" in capsys.readouterr().err
