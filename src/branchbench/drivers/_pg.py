"""Postgres connection + bulk-load helpers (psycopg3)."""
from __future__ import annotations

import time

import psycopg

from .. import schema


def connect(host, port, user, password, dbname, autocommit=True, sslmode=None,
            hostaddr=None, retries=1, retry_delay=1.0, connect_timeout=30):
    # TCP keepalives + a connect timeout so a socket that goes stale (e.g. a
    # serverless compute suspends after a long idle seed) surfaces as an error
    # instead of blocking a read forever. Without these a post-seed idle
    # connection to Neon/Aurora can hang the whole run.
    kw = dict(host=host, port=port, user=user, password=password,
              dbname=dbname, autocommit=autocommit, connect_timeout=connect_timeout,
              keepalives=1, keepalives_idle=10, keepalives_interval=5,
              keepalives_count=3)
    if sslmode:
        kw["sslmode"] = sslmode
    if hostaddr:
        kw["hostaddr"] = hostaddr
    last = None
    for _ in range(max(1, retries)):
        try:
            return psycopg.connect(**kw)
        except Exception as e:  # scale-to-zero wake / container warmup
            last = e
            time.sleep(retry_delay)
    raise last


def create_database(admin_conn, dbname, template="template0"):
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    admin_conn.execute(f'CREATE DATABASE "{dbname}" TEMPLATE {template}')


def drop_database(admin_conn, dbname):
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


def apply_schema(conn, engine="postgres", with_indexes=False):
    conn.execute(schema.ddl(engine))
    if with_indexes:
        conn.execute(schema.index_ddl(engine))


def copy_load(conn, data_files: dict, with_indexes=True, engine="postgres"):
    """COPY-load all tables from generated TSVs (text format: tab, \\N null)."""
    for table in schema.TABLES:
        with conn.cursor() as cur, cur.copy(f'COPY {table} FROM STDIN') as cp:
            with open(data_files[table], "rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    cp.write(chunk)
    if with_indexes:
        conn.execute(schema.index_ddl(engine))
    conn.execute("ANALYZE")


def database_size_bytes(conn, dbname) -> int | None:
    try:
        row = conn.execute("SELECT pg_database_size(%s)", (dbname,)).fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


def representative_write(conn, rows=50_000):
    """Representative write to make CoW copy-on-write page divergence observable."""
    conn.execute("UPDATE accounts SET status = 'active' WHERE account_id <= 500")
    conn.execute(
        "INSERT INTO events (event_id, account_id, event_type, dedupe_key, payload, occurred_at) "
        "SELECT (SELECT COALESCE(MAX(event_id),0) FROM events) + g, 1, 'bench_write', "
        "'BENCHWRITE', '{\"w\":1}', now() FROM generate_series(1, %s) g", (rows,)
    )


def run_reads(conn, queries, n) -> list[float]:
    """Run `queries` round-robin n times, return per-query latencies (seconds)."""
    lat = []
    for i in range(n):
        q = queries[i % len(queries)]
        t0 = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute(q)
            cur.fetchall()
        lat.append(time.perf_counter() - t0)
    return lat
