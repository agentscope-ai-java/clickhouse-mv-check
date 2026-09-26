# clickhouse-mv-check

A materialized view in ClickHouse is an insert trigger, not a view.

Everything below follows from that one sentence, and none of it is inferred
from documentation — it's a transcript of a real ClickHouse 26.9.3 server,
started in Docker for this purpose. Every command shown was actually run;
every result shown is what came back.

## Prove it: the view misses rows that already existed

```sql
CREATE TABLE src (id UInt32, val String) ENGINE = MergeTree ORDER BY id;
INSERT INTO src VALUES (1,'a'),(2,'b'),(3,'c');   -- 3 rows, before the view exists

CREATE TABLE tgt (id UInt32, val String) ENGINE = MergeTree ORDER BY id;
CREATE MATERIALIZED VIEW mv TO tgt AS SELECT id, val FROM src;   -- no POPULATE
```

```
SELECT count() FROM src   -- 3
SELECT count() FROM tgt   -- 0   <- the view saw none of them

INSERT INTO src VALUES (4,'d');
SELECT count() FROM tgt   -- 1   <- only the new row
```

The view isn't "a query that stays up to date." It's a trigger that runs its
`SELECT` once, on the block just inserted, and writes the result to `tgt`.
Rows already in `src` when the trigger was created never pass through it —
not now, not ever, unless someone runs a manual backfill.

## Prove it: a JOIN reads the other table's state *right now*, not eventually

```sql
CREATE MATERIALIZED VIEW mv TO orders_enriched AS
SELECT o.order_id, o.cust_id, c.name
FROM orders o LEFT JOIN customers c ON o.cust_id = c.cust_id;
```

```
INSERT INTO orders VALUES (102, 3);      -- customer 3 doesn't exist yet
INSERT INTO customers VALUES (3, 'carol');

SELECT * FROM orders_enriched WHERE order_id = 102;
-- 102  3  ''      <- permanently empty; carol arrived a moment too late

INSERT INTO orders VALUES (103, 3);      -- now carol exists
SELECT * FROM orders_enriched WHERE order_id = 103;
-- 103  3  'carol' <- this one's fine, nothing else changed
```

The view only re-runs on inserts to `orders`. When it fires, it reads
`customers` however it looks at that instant and never again. Nothing about
`customers` changing later re-triggers anything. If your join partner tends
to lag the fact table, some fraction of rows will permanently carry an
empty/null match — and there's no error, no log line, nothing to notice
except a report that's quietly wrong.

## Prove it: an erroring view fails the insert — but doesn't roll back the source

```sql
CREATE MATERIALIZED VIEW mv TO tgt AS
SELECT id, intDiv(100, denom) AS result FROM src;
```

```
INSERT INTO src VALUES (2, 5), (3, 0), (4, 2);
-- Code: 153. DB::Exception: Division by zero ... while pushing to view default.mv
--  ... (ILLEGAL_DIVISION)

SELECT count() FROM src   -- 4   <- all three new rows are THERE
SELECT count() FROM tgt   -- 1   <- but the view got none of them
```

This is the sharpest edge in the whole system. The client sees an error and
a caller will often retry — but the base-table insert already committed. A
naive retry duplicates rows in `src` while `tgt` stays wrong forever, because
ClickHouse never retries the failed view push on its own. One bad row in a
batch takes the whole batch's contribution to every view down with it, and
sends a misleading "insert failed" back to whoever's waiting.

## Prove it: what POPULATE actually does (and why it isn't in `SHOW CREATE`)

```sql
CREATE TABLE src (id UInt32, val String) ENGINE = MergeTree ORDER BY id;
INSERT INTO src VALUES (1,'a'),(2,'b'),(3,'c');

CREATE MATERIALIZED VIEW mv TO tgt POPULATE AS SELECT id, val FROM src;
```

```
SELECT count() FROM tgt   -- 3   <- POPULATE backfilled the pre-existing rows

SHOW CREATE TABLE mv;
-- CREATE MATERIALIZED VIEW default.mv TO default.tgt (...) AS SELECT id, val FROM default.src
-- (no mention of POPULATE anywhere)
```

`POPULATE` does back-fill existing rows — but it's an action taken once at
creation, not a stored property of the view. Once it's run, there is no way
to ask ClickHouse "was this view populated?" — the DDL you'd re-run from
`SHOW CREATE` looks identical either way. We also tried, deliberately, to
force ClickHouse's own documented race ("rows inserted during POPULATE can be
missing from the destination") by inserting concurrently while a 50M-row
`POPULATE` was running; in this version (26.9.3.38) every concurrent row
landed exactly once, no misses, no duplicates. We're reporting that
honestly rather than asserting a race we couldn't reproduce — but the
one-time, unrecorded nature of `POPULATE` is real and reproducible, and it's
exactly why this package's live check compares row counts instead of trusting
the DDL.

## Prove it: AggregatingMergeTree silently drops values with the wrong column type

```sql
CREATE TABLE agg (key String, amount UInt64) ENGINE = AggregatingMergeTree ORDER BY key;
CREATE MATERIALIZED VIEW mv TO agg AS SELECT key, sum(amount) AS amount FROM src GROUP BY key;

INSERT INTO src VALUES ('a', 10);
INSERT INTO src VALUES ('a', 20);   -- two parts, two partial sums for the same key
```

```
SELECT sum(amount) FROM agg   -- 30   <- correct, but only because parts haven't merged

OPTIMIZE TABLE agg FINAL;
SELECT sum(amount) FROM agg   -- 10   <- one part's row was kept, the other silently dropped
```

`AggregatingMergeTree` only *combines* columns declared as
`AggregateFunction(...)` on merge. Declare the column as a plain `UInt64` and
feed it a plain `sum(...)` and ClickHouse accepts it without complaint — merges
just behave like any other MergeTree merge and keep one row per key,
discarding the rest. The fix (`amount AggregateFunction(sum, UInt64)` written
via `sumState(amount)`, read back via `sumMerge(amount)`) is one line
different and gives 30 before and after the merge, every time we checked.

## Install

```sh
pip install clickhouse-mv-check
```

Python >= 3.11. Zero runtime dependencies — standard library only.

## Use

### Static: check `.sql` migration files

```sh
clickhouse-mv-check static schema/*.sql
clickhouse-mv-check static schema/*.sql --json
```

```python
from clickhouse_mv_check import parse_source, run_static_checks

views, tables = parse_source(open("schema.sql").read())
tables_by_name = {t.name: t for t in tables}
for mv in views:
    target = tables_by_name.get((mv.target_table or "").rsplit(".", 1)[-1])
    for finding in run_static_checks(mv, target):
        print(finding.severity.value, finding.rule, finding.message)
```

### Live: check a running server

Zero runtime dependencies means this package never imports a ClickHouse
driver. It accepts anything with a `.query(sql) -> rows` method — pass in a
`clickhouse-connect` client, a `clickhouse-driver` client wrapped to expose
`.query`, or this package's own minimal stdlib HTTP client:

```python
from clickhouse_mv_check import run_live_checks
from clickhouse_mv_check.http_client import HttpClient

client = HttpClient("http://localhost:8123", user="default", password="secret")
for finding in run_live_checks(client, database="default", name="my_view"):
    print(finding.severity.value, finding.rule, finding.message)
```

```sh
clickhouse-mv-check live --url http://localhost:8123 --user default --password secret --all
clickhouse-mv-check live --url http://localhost:8123 --view my_view --json
```

## What each check catches

| Check | Kind | Consequence if it fires |
| --- | --- | --- |
| `join-reads-foreign-table` | static + live | A JOIN/subquery reads a table other than the source; that table is read at trigger time, not kept in sync. |
| `populate-used` | static (`.sql` source only) | `POPULATE` is a one-time, unrecorded action with a documented concurrent-insert race. |
| `missing-to-table` | static + live | No explicit `TO` table means a hidden `.inner_id.<uuid>` table that can't be inspected or altered on its own, and is dropped the moment the view is. |
| `aggregate-state-mismatch` | static + live | An `AggregatingMergeTree`/`SimpleAggregateFunction` column fed the wrong State function (or a plain aggregate into a plain column) — merges silently keep one part's value instead of combining them. |
| `backfill-plausibility` | live only | Target row count is implausibly low next to source rows that predate the view — the row-count fingerprint of a missed backfill. |

Every `Finding` carries `message` (what was seen), `consequence` (what
actually breaks, in mechanism, not just "this is risky"), and `fix`.

## Options (`live` subcommand)

| Option | Default | What it does |
| --- | --- | --- |
| `--view` / `--all` | — | Check one named view, or every materialized view in `--database`. |
| `--backfill-ratio-threshold` | `0.8` | Flag when target rows ÷ source rows falls below this. |
| `--json` | off | Emit findings as a JSON array instead of text. |

Exit codes: `0` clean, `1` findings reported, `2` usage/connection error — safe to wire into CI.

## What it does not do

- **It is not a full SQL parser.** The static checks use a narrow reader built
  for `CREATE MATERIALIZED VIEW`/`CREATE TABLE` shapes as ClickHouse actually
  emits and accepts them, not arbitrary SQL. Exotic formatting (e.g. a
  subquery reusing a keyword as an identifier) can confuse it.
- **`populate-used` cannot see a view that already exists.** As demonstrated
  above, `POPULATE` leaves no trace in `SHOW CREATE TABLE` — this check only
  fires against the original `.sql` source that was actually run. The
  `backfill-plausibility` live check is the intended way to catch a missed
  backfill after the fact.
- **`backfill-plausibility` is a heuristic, not a proof.** It buckets a
  source table's rows into "before" and "after" the view's
  `metadata_modification_time` using `system.parts.modification_time` (a
  part's own write time). Heavy merging can shift a part's timestamp
  forward, and altering a view later moves `metadata_modification_time`
  without changing what it originally missed. Treat a flag as "go look," not
  as certainty.
- **No SSRF protection, no query building, no ORM.** This package reads
  definitions and metadata; it does not construct or run migrations for you.
- **Chained MVs get one advisory rule at most** (via `join-reads-foreign-table`
  when a view reads another view's target); it doesn't model an entire
  dependency graph's ordering.

## Develop

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q                 # unit tests always run;
                                               # integration tests auto-skip
                                               # if no ClickHouse is reachable
.venv/bin/python -m mypy src --strict
```

Integration tests want a real ClickHouse reachable at
`CLICKHOUSE_MV_CHECK_TEST_URL` (default `http://localhost:18123`, matching
the Docker command used to produce every transcript above):

```sh
docker run -d --name mvcheck-ch -p 18123:8123 \
  -e CLICKHOUSE_USER=default -e CLICKHOUSE_PASSWORD=mvcheck \
  clickhouse/clickhouse-server:latest
```

## License

MIT
