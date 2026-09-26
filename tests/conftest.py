from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

from clickhouse_mv_check.http_client import ClickHouseHTTPError, HttpClient

CH_URL = os.environ.get("CLICKHOUSE_MV_CHECK_TEST_URL", "http://localhost:18123")
CH_USER = os.environ.get("CLICKHOUSE_MV_CHECK_TEST_USER", "default")
CH_PASSWORD = os.environ.get("CLICKHOUSE_MV_CHECK_TEST_PASSWORD", "mvcheck")
CH_DATABASE = "default"


@pytest.fixture(scope="session")
def ch_client() -> HttpClient:
    client = HttpClient(CH_URL, user=CH_USER, password=CH_PASSWORD, database=CH_DATABASE)
    try:
        rows = client.query("SELECT 1")
    except (ClickHouseHTTPError, OSError) as exc:
        pytest.skip(f"no live ClickHouse reachable at {CH_URL}: {exc}")
    assert rows == [(1,)]
    return client


@pytest.fixture
def unique_name() -> Iterator[str]:
    yield "t" + uuid.uuid4().hex[:12]


def exec_all(client: HttpClient, *statements: str) -> None:
    for stmt in statements:
        client.query(stmt)
