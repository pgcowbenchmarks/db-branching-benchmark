"""Server-side (in-engine) data generator for the large scale (tens–hundreds of GB).

The file-based generator (`datagen.py`) writes TSVs on the client and COPY-loads
them — fine up to a few GB, but at 100 GB+ the local disk and the client→cloud
uplink both become the bottleneck (a laptop can neither hold nor push that much
data in a reasonable time). This generator instead builds the data *inside* the
target Postgres with `INSERT … SELECT generate_series(...)`, so generation runs
on the database's own (in-region) compute and nothing crosses the client link.

It reproduces the same production-shaped properties as `datagen.py`:
  * power-law hot accounts   — account_id = 1 + (n_acct * random()^3)::bigint % n_acct
  * real null patterns       — email ~15% null, order.note ~70% null, payload ~20% null
  * duplicate keys           — events draw dedupe_key from a small pool (n_orders/20)
  * wide values              — ~3% of orders carry a long repeated "promo " note
  * join fan-out             — ~2.5 order_items per order, ~1.35 events per order

Deviations from datagen.py, for bulk-generation speed at scale (documented so the
result is honest): tables are created WITHOUT foreign-key constraints (per-row FK
checks against hundreds of millions of parents would dominate runtime; the data
*shape* — skew, nulls, dups, width — is what "production-shaped" means here), and
generation is chunked with a live size poll so it stops at the requested target.
Secondary indexes (matching schema.index_ddl) are built after load, so the parent
matches the indexed shape the CoW branch is cut from.
"""
from __future__ import annotations

import time

# Same value vocabularies as datagen.py (kept in sync intentionally).
REGIONS = ["us-east", "us-west", "eu-central", "eu-west", "ap-south", "ap-southeast"]
TIERS = ["free", "standard", "premium", "enterprise"]
ACCT_STATUS = ["active", "active", "active", "suspended", "closed"]
ORDER_STATUS = ["paid", "paid", "paid", "pending", "refunded", "failed"]
CURRENCIES = ["USD", "USD", "USD", "EUR", "GBP", "JPY", "INR"]
EVENT_TYPES = ["login", "view", "purchase", "refund", "support", "settings_change"]
SKEW_EXP = 3.0
_EPOCH = 1_700_000_000

# Tables WITHOUT FK constraints (bulk-load speed); columns identical to schema.py.
_DDL_NO_FK = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id BIGINT PRIMARY KEY, name TEXT NOT NULL, region TEXT NOT NULL,
    tier TEXT NOT NULL, email TEXT, legacy_code TEXT,
    created_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
    order_id BIGINT PRIMARY KEY, account_id BIGINT NOT NULL, amount_cents BIGINT NOT NULL,
    currency TEXT NOT NULL, status TEXT NOT NULL, note TEXT, created_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS order_items (
    item_id BIGINT PRIMARY KEY, order_id BIGINT NOT NULL, sku TEXT NOT NULL,
    qty INT NOT NULL, unit_price_cents BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    event_id BIGINT PRIMARY KEY, account_id BIGINT NOT NULL, event_type TEXT NOT NULL,
    dedupe_key TEXT NOT NULL, payload TEXT, occurred_at TIMESTAMPTZ NOT NULL);
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_orders_account ON orders(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_account ON events(account_id)",
]


def _sql_array(vals):
    inner = ",".join("'" + v.replace("'", "''") + "'" for v in vals)
    return "ARRAY[" + inner + "]"


def _pg_arr(vals):
    return f"({_sql_array(vals)})[1 + (floor(random()*{len(vals)}))::int]"


def db_size_bytes(conn) -> int:
    return conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]


def generate_server_side(conn, target_bytes: int, *, chunk_orders: int = 10_000_000,
                         items_per_order: float = 2.5, events_per_order: float = 1.35,
                         orders_per_account: int = 50, log=print) -> dict:
    """Generate production-shaped data in `conn` until pg_database_size >= target_bytes.

    Chunks orders (and proportional items/events) so progress is visible and no
    single statement runs unbounded. Indexes are built at the end.
    """
    conn.execute("SET statement_timeout = 0")
    conn.execute("SET synchronous_commit = off")  # throwaway benchmark parent
    for stmt in _DDL_NO_FK.strip().split(";\n"):
        if stmt.strip():
            conn.execute(stmt)

    # Fixed account universe sized to the projected final order count.
    proj_orders = max(chunk_orders, int(target_bytes / 350))  # ~350 B/order incl. indexes
    n_accounts = max(100, proj_orders // orders_per_account)
    have = conn.execute("SELECT count(*) FROM accounts").fetchone()[0]
    if have < n_accounts:
        log(f"  accounts: generating {n_accounts:,} …", flush=True)
        conn.execute(f"""
            INSERT INTO accounts
            SELECT g,
                   'Account '||g||' '||(ARRAY['Corp','LLC','Inc','GmbH','KK'])[1+(floor(random()*5))::int],
                   {_pg_arr(REGIONS)}, {_pg_arr(TIERS)},
                   CASE WHEN random()<0.15 THEN NULL ELSE 'user'||g||'@ex-'||{_pg_arr(REGIONS)}||'.com' END,
                   CASE WHEN random()<0.15 THEN 'LGCY-'||lpad((floor(random()*10000))::int::text,4,'0') ELSE NULL END,
                   to_timestamp({_EPOCH} + floor(random()*720*86400)),
                   {_pg_arr(ACCT_STATUS)}
            FROM generate_series({have+1}, {n_accounts}) g
            ON CONFLICT DO NOTHING""")

    n_dedupe = max(50, proj_orders // 20)
    acct_expr = f"1 + (floor({n_accounts} * power(random(), {SKEW_EXP}))::bigint % {n_accounts})"

    start = time.time()
    oid0 = conn.execute("SELECT COALESCE(max(order_id),0) FROM orders").fetchone()[0]
    iid0 = conn.execute("SELECT COALESCE(max(item_id),0) FROM order_items").fetchone()[0]
    eid0 = conn.execute("SELECT COALESCE(max(event_id),0) FROM events").fetchone()[0]
    size = db_size_bytes(conn)
    log(f"  seeding to target {target_bytes/1e9:.0f} GB (start size {size/1e9:.2f} GB, "
        f"orders so far {oid0:,}) …", flush=True)

    while size < target_bytes:
        a, b = oid0 + 1, oid0 + chunk_orders
        conn.execute(f"""
            INSERT INTO orders
            SELECT g, {acct_expr}, 100 + (floor(random()*499900))::bigint,
                   {_pg_arr(CURRENCIES)}, {_pg_arr(ORDER_STATUS)},
                   CASE WHEN random()<0.30 THEN
                     CASE WHEN random()<0.10 THEN repeat('promo ', 1+(floor(random()*60))::int)
                          ELSE 'ref#'||(1000+(floor(random()*9000))::int) END
                     ELSE NULL END,
                   to_timestamp({_EPOCH} + floor(random()*720*86400))
            FROM generate_series({a}, {b}) g""")
        n_items = int(chunk_orders * items_per_order)
        conn.execute(f"""
            INSERT INTO order_items
            SELECT {iid0} + g, {a} + (floor(random()*{chunk_orders}))::bigint,
                   'SKU-'||lpad((floor(random()*2000))::int::text,5,'0'),
                   1+(floor(random()*20))::int, 50+(floor(random()*19950))::bigint
            FROM generate_series(1, {n_items}) g""")
        iid0 += n_items
        n_events = int(chunk_orders * events_per_order)
        conn.execute(f"""
            INSERT INTO events
            SELECT {eid0} + g, {acct_expr}, {_pg_arr(EVENT_TYPES)},
                   'DEDUP-'||lpad((floor(random()*{n_dedupe}))::int::text,6,'0'),
                   CASE WHEN random()<0.20 THEN NULL ELSE '{{"k":'||(floor(random()*1000))::int||'}}' END,
                   to_timestamp({_EPOCH} + floor(random()*720*86400))
            FROM generate_series(1, {n_events}) g""")
        eid0 += n_events
        oid0 = b
        size = db_size_bytes(conn)
        rate = oid0 / max(1e-9, time.time() - start)
        log(f"    orders={oid0:,} items~{iid0:,} events~{eid0:,}  size={size/1e9:.1f} GB "
            f"({rate/1e3:.0f}k orders/s)", flush=True)

    log("  building indexes …", flush=True)
    for ix in _INDEXES:
        t = time.time()
        conn.execute(ix)
        log(f"    {ix.split('idx_')[1].split(' ')[0]}  {time.time()-t:.0f}s", flush=True)
    conn.execute("ANALYZE")
    final = db_size_bytes(conn)
    log(f"  done. final size {final/1e9:.1f} GB, orders {oid0:,}", flush=True)
    return {"orders": oid0, "order_items": iid0, "events": eid0,
            "accounts": n_accounts, "final_size_bytes": final,
            "elapsed_s": time.time() - start}
