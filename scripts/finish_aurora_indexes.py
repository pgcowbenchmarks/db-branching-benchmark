#!/usr/bin/env python3
"""Build the 4 indexes the interrupted pg_restore never finished on Aurora
branchbench_parent (order_items + events), so the Aurora parent matches the RDS
source dataset (bench_parent_lg, 243.8 GB, fully indexed) before the fast-clone.

Idempotent + drop-tolerant: checks pg_indexes before each build and skips ones
that already exist; on a dropped connection it reconnects and retries that index
(a non-CONCURRENT build rolls back on disconnect, so the retry just starts fresh).
Runs one build at a time with a big session maintenance_work_mem.
"""
import sys, time, yaml, psycopg
from pathlib import Path

CFG = yaml.safe_load(open(Path(__file__).resolve().parent.parent / "config/cloud.yaml"))
A = CFG["engines"]["aurora_postgres"]
DB = "branchbench_parent"

# smallest/fastest first so we get early progress, biggest (order_items) last
INDEXES = [
    ("events",      "events_pkey",       "CREATE UNIQUE INDEX events_pkey ON public.events USING btree (event_id)"),
    ("events",      "idx_events_account","CREATE INDEX idx_events_account ON public.events USING btree (account_id)"),
    ("order_items", "idx_items_order",   "CREATE INDEX idx_items_order ON public.order_items USING btree (order_id)"),
    ("order_items", "order_items_pkey",  "CREATE UNIQUE INDEX order_items_pkey ON public.order_items USING btree (item_id)"),
]

def connect():
    return psycopg.connect(
        host=A["host"], port=5432, user=A["user"], password=A["password"],
        dbname=DB, sslmode="require", autocommit=True, connect_timeout=30,
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5,
    )

def index_exists(cur, name):
    cur.execute("SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname=%s", (name,))
    return cur.fetchone() is not None

def main():
    for tbl, name, ddl in INDEXES:
        while True:
            try:
                conn = connect(); cur = conn.cursor()
                if index_exists(cur, name):
                    print(f"[skip] {name} already exists", flush=True); conn.close(); break
                cur.execute("SET maintenance_work_mem = '6GB'")
                cur.execute("SET max_parallel_maintenance_workers = 2")
                cur.execute("SET statement_timeout = 0")
                t0 = time.time()
                print(f"[build] {name} on {tbl} … (started {time.strftime('%H:%M:%S')})", flush=True)
                cur.execute(ddl)
                dt = time.time() - t0
                print(f"[done ] {name} in {dt/60:.1f} min", flush=True)
                conn.close(); break
            except Exception as e:
                print(f"  [retry] {name}: {str(e)[:90]}; reconnecting in 10s", flush=True)
                try: conn.close()
                except Exception: pass
                time.sleep(10)
    # final report
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT pg_size_pretty(pg_database_size(%s)), pg_database_size(%s)", (DB, DB))
    sz = cur.fetchone(); print(f"[final] {DB} = {sz[0]} ({sz[1]/1e9:.1f} GB)", flush=True)
    cur.execute("SELECT count(*) FROM pg_indexes WHERE schemaname='public'")
    print(f"[final] indexes = {cur.fetchone()[0]} (expect 8)", flush=True)
    conn.close()

if __name__ == "__main__":
    main()
