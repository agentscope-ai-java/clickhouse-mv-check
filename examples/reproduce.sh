#!/usr/bin/env bash
# Reproduces every claim in the README against a real ClickHouse server.
# Requires a server reachable the way the Docker command below starts one;
# point CH_URL/CH_USER/CH_PASSWORD elsewhere if you already have one running.
#
#   docker run -d --name mvcheck-ch -p 18123:8123 \
#     -e CLICKHOUSE_USER=default -e CLICKHOUSE_PASSWORD=mvcheck \
#     clickhouse/clickhouse-server:latest
#
#   bash examples/reproduce.sh

set -euo pipefail

CH_URL="${CH_URL:-http://localhost:18123}"
CH_USER="${CH_USER:-default}"
CH_PASSWORD="${CH_PASSWORD:-mvcheck}"

run() {
  curl -s -u "${CH_USER}:${CH_PASSWORD}" "${CH_URL}/" --data-binary "$1"
}

section() { echo; echo "=== $1 ==="; }

section "0. version"
run "SELECT version()"
echo

section "1. a materialized view misses rows that already existed"
run "DROP TABLE IF EXISTS demo1_src"; run "DROP TABLE IF EXISTS demo1_tgt"; run "DROP TABLE IF EXISTS demo1_mv"
run "CREATE TABLE demo1_src (id UInt32, val String) ENGINE = MergeTree ORDER BY id"
run "INSERT INTO demo1_src VALUES (1,'a'),(2,'b'),(3,'c')"
run "CREATE TABLE demo1_tgt (id UInt32, val String) ENGINE = MergeTree ORDER BY id"
run "CREATE MATERIALIZED VIEW demo1_mv TO demo1_tgt AS SELECT id, val FROM demo1_src"
echo -n "src count: "; run "SELECT count() FROM demo1_src"; echo
echo -n "tgt count (expect 0 - view missed pre-existing rows): "; run "SELECT count() FROM demo1_tgt"; echo
run "INSERT INTO demo1_src VALUES (4,'d')"
echo -n "tgt count after a fresh insert (expect 1): "; run "SELECT count() FROM demo1_tgt"; echo

section "2. a JOIN reads the other table's state at trigger time, not live"
run "DROP TABLE IF EXISTS demo2_customers"; run "DROP TABLE IF EXISTS demo2_orders"
run "DROP TABLE IF EXISTS demo2_enriched"; run "DROP TABLE IF EXISTS demo2_mv"
run "CREATE TABLE demo2_customers (cust_id UInt32, name String) ENGINE = MergeTree ORDER BY cust_id"
run "CREATE TABLE demo2_orders (order_id UInt32, cust_id UInt32) ENGINE = MergeTree ORDER BY order_id"
run "CREATE TABLE demo2_enriched (order_id UInt32, cust_id UInt32, name String) ENGINE = MergeTree ORDER BY order_id"
run "CREATE MATERIALIZED VIEW demo2_mv TO demo2_enriched AS SELECT o.order_id, o.cust_id, c.name FROM demo2_orders o LEFT JOIN demo2_customers c ON o.cust_id = c.cust_id"
run "INSERT INTO demo2_orders VALUES (102, 3)"
run "INSERT INTO demo2_customers VALUES (3,'carol')"
echo -n "order 102 (customer inserted after the order - expect empty name): "; run "SELECT * FROM demo2_enriched WHERE order_id=102 FORMAT TSV"
run "INSERT INTO demo2_orders VALUES (103, 3)"
echo -n "order 103 (customer already existed - expect carol): "; run "SELECT * FROM demo2_enriched WHERE order_id=103 FORMAT TSV"

section "3. an erroring view fails the client-visible insert but not the source write"
run "DROP TABLE IF EXISTS demo3_src"; run "DROP TABLE IF EXISTS demo3_tgt"; run "DROP TABLE IF EXISTS demo3_mv"
run "CREATE TABLE demo3_src (id UInt32, denom UInt32) ENGINE = MergeTree ORDER BY id"
run "CREATE TABLE demo3_tgt (id UInt32, result UInt32) ENGINE = MergeTree ORDER BY id"
run "CREATE MATERIALIZED VIEW demo3_mv TO demo3_tgt AS SELECT id, intDiv(100, denom) AS result FROM demo3_src"
run "INSERT INTO demo3_src VALUES (1, 10)"
echo "inserting a batch where one row divides by zero:"
run "INSERT INTO demo3_src VALUES (2, 5), (3, 0), (4, 2)" || true
echo
echo -n "src count (expect 4 - the write was NOT rolled back): "; run "SELECT count() FROM demo3_src"; echo
echo -n "tgt count (expect 1 - the view got none of the failing batch): "; run "SELECT count() FROM demo3_tgt"; echo

section "4. POPULATE backfills, but leaves no trace in SHOW CREATE"
run "DROP TABLE IF EXISTS demo4_src"; run "DROP TABLE IF EXISTS demo4_tgt"; run "DROP TABLE IF EXISTS demo4_mv"
run "CREATE TABLE demo4_src (id UInt32, val String) ENGINE = MergeTree ORDER BY id"
run "INSERT INTO demo4_src VALUES (1,'a'),(2,'b'),(3,'c')"
run "CREATE TABLE demo4_tgt (id UInt32, val String) ENGINE = MergeTree ORDER BY id"
run "CREATE MATERIALIZED VIEW demo4_mv TO demo4_tgt POPULATE AS SELECT id, val FROM demo4_src"
echo -n "tgt count (expect 3 - POPULATE backfilled): "; run "SELECT count() FROM demo4_tgt"; echo
echo "SHOW CREATE TABLE (note: no POPULATE keyword anywhere):"
run "SHOW CREATE TABLE demo4_mv FORMAT TSVRaw"

section "5. AggregatingMergeTree silently drops values with the wrong column type"
run "DROP TABLE IF EXISTS demo5_src"; run "DROP TABLE IF EXISTS demo5_agg"; run "DROP TABLE IF EXISTS demo5_mv"
run "CREATE TABLE demo5_src (key String, amount UInt64) ENGINE = MergeTree ORDER BY key"
run "CREATE TABLE demo5_agg (key String, amount UInt64) ENGINE = AggregatingMergeTree ORDER BY key"
run "CREATE MATERIALIZED VIEW demo5_mv TO demo5_agg AS SELECT key, sum(amount) AS amount FROM demo5_src GROUP BY key"
run "INSERT INTO demo5_src VALUES ('a', 10)"
run "INSERT INTO demo5_src VALUES ('a', 20)"
echo -n "sum(amount) before merge (expect 30, correct by accident): "; run "SELECT sum(amount) FROM demo5_agg"; echo
run "OPTIMIZE TABLE demo5_agg FINAL"
echo -n "sum(amount) after OPTIMIZE FINAL (expect 10 - silently wrong): "; run "SELECT sum(amount) FROM demo5_agg"; echo

section "cleanup"
for t in demo1_mv demo1_tgt demo1_src demo2_mv demo2_enriched demo2_orders demo2_customers \
         demo3_mv demo3_tgt demo3_src demo4_mv demo4_tgt demo4_src demo5_mv demo5_agg demo5_src; do
  run "DROP TABLE IF EXISTS $t" >/dev/null
done
echo "done."
