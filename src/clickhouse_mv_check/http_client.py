"""A minimal ClickHouse HTTP-interface client, standard library only.

This exists so the CLI's ``live`` subcommand and this package's own tests can
talk to a real server without adding a runtime dependency on a driver. It is
not a general-purpose client: no connection pooling, no retries, no async.
If you already use ``clickhouse-connect`` or ``clickhouse-driver``, wrap
whichever object you have — anything with a ``.query(sql) -> rows`` method
works with the rest of this package (see ``live.py``).
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from typing import Any

# Statements that return a result set worth asking for as JSON. Appending a
# FORMAT clause to anything else — confirmed live — can break parsing: e.g.
# `INSERT INTO t VALUES (1) FORMAT JSONCompact` fails with CANNOT_PARSE_INPUT
# because ClickHouse reads the trailing FORMAT as naming the *input* format
# for inline VALUES data, not the output format.
_RETURNS_ROWS = re.compile(r"^\s*(SELECT|WITH|SHOW|DESCRIBE|DESC|EXISTS|EXPLAIN)\b", re.IGNORECASE)


class ClickHouseHTTPError(RuntimeError):
    """Raised when the server returns a non-2xx response."""


class HttpClient:
    """Talks to ClickHouse's HTTP interface (``FORMAT JSONCompact``)."""

    def __init__(
        self,
        url: str,
        *,
        user: str = "default",
        password: str = "",
        database: str = "default",
        timeout: float = 10.0,
    ) -> None:
        self._url = url.rstrip("/")
        self._user = user
        self._password = password
        self._database = database
        self._timeout = timeout

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run ``sql`` and return its rows as tuples.

        Statements that produce a result set (SELECT and friends) get
        ``FORMAT JSONCompact`` appended so counts come back as JSON numbers
        and everything else as strings; DDL/INSERT statements are sent as
        given (see ``_RETURNS_ROWS`` for why appending FORMAT to those is
        unsafe) and always return ``[]``.
        """
        wants_format = bool(_RETURNS_ROWS.match(sql))
        body = (f"{sql}\nFORMAT JSONCompact" if wants_format else sql).encode()
        headers = {
            "Authorization": "Basic "
            + base64.b64encode(f"{self._user}:{self._password}".encode()).decode(),
            "X-ClickHouse-Database": self._database,
        }
        request = urllib.request.Request(self._url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise ClickHouseHTTPError(f"ClickHouse returned {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ClickHouseHTTPError(f"could not reach {self._url}: {exc.reason}") from exc

        if not wants_format or not payload.strip():
            return []
        parsed = json.loads(payload)
        return [tuple(row) for row in parsed.get("data", [])]
