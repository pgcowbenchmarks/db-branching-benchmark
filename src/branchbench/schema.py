"""Production-shaped OLTP schema, dialect-aware for Postgres and SQLite.

Four related tables give the join fan-out, skew, null, and dirtiness properties
the benchmark methodology require ("real volumes, distributions, and skew (power-law
hot keys), real null patterns, duplicate keys, and wide values"). The same
schema also lets the harness later demonstrate the Table III fidelity defects.

  accounts     -- dimension; a few HOT accounts dominate (power law)
  orders       -- fact; FK -> accounts; carries real nulls (note) + wide values
  order_items  -- fan-out under orders (many:1); the join-fan-out surface
  events       -- append log; FK -> accounts; duplicate/idempotency keys
"""
from __future__ import annotations

# Logical table order respects FK dependencies for load + drop.
TABLES = ["accounts", "orders", "order_items", "events"]

_POSTGRES_DDL = """
CREATE TABLE accounts (
    account_id   BIGINT PRIMARY KEY,
    name         TEXT NOT NULL,
    region       TEXT NOT NULL,
    tier         TEXT NOT NULL,
    email        TEXT,                 -- real nulls: some accounts have none
    legacy_code  TEXT,                 -- mostly null legacy encoding
    created_at   TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL
);
CREATE TABLE orders (
    order_id     BIGINT PRIMARY KEY,
    account_id   BIGINT NOT NULL REFERENCES accounts(account_id),
    amount_cents BIGINT NOT NULL,
    currency     TEXT NOT NULL,
    status       TEXT NOT NULL,
    note         TEXT,                 -- frequently null; wide free text when present
    created_at   TIMESTAMPTZ NOT NULL
);
CREATE TABLE order_items (
    item_id      BIGINT PRIMARY KEY,
    order_id     BIGINT NOT NULL REFERENCES orders(order_id),
    sku          TEXT NOT NULL,
    qty          INT NOT NULL,
    unit_price_cents BIGINT NOT NULL
);
CREATE TABLE events (
    event_id     BIGINT PRIMARY KEY,
    account_id   BIGINT NOT NULL REFERENCES accounts(account_id),
    event_type   TEXT NOT NULL,
    dedupe_key   TEXT NOT NULL,        -- duplicate keys occur in real logs
    payload      TEXT,
    occurred_at  TIMESTAMPTZ NOT NULL
);
"""

_POSTGRES_INDEXES = """
CREATE INDEX idx_orders_account ON orders(account_id);
CREATE INDEX idx_orders_created ON orders(created_at);
CREATE INDEX idx_items_order ON order_items(order_id);
CREATE INDEX idx_events_account ON events(account_id);
"""

# SQLite: no TIMESTAMPTZ / no enforced FK by default; keep it minimal since the
# in-memory fake exists only as the lower bound.
_SQLITE_DDL = """
CREATE TABLE accounts (
    account_id INTEGER PRIMARY KEY, name TEXT NOT NULL, region TEXT NOT NULL,
    tier TEXT NOT NULL, email TEXT, legacy_code TEXT, created_at TEXT NOT NULL,
    status TEXT NOT NULL);
CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL, status TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL);
CREATE TABLE order_items (
    item_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, sku TEXT NOT NULL,
    qty INTEGER NOT NULL, unit_price_cents INTEGER NOT NULL);
CREATE TABLE events (
    event_id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, event_type TEXT NOT NULL,
    dedupe_key TEXT NOT NULL, payload TEXT, occurred_at TEXT NOT NULL);
"""


def ddl(engine: str) -> str:
    """Return CREATE TABLE statements for the given engine (no indexes)."""
    return {"postgres": _POSTGRES_DDL, "sqlite": _SQLITE_DDL}[engine]


def index_ddl(engine: str) -> str:
    """Return secondary-index statements (run after bulk load for speed)."""
    return {"postgres": _POSTGRES_INDEXES, "sqlite": ""}[engine]


def drop_all(engine: str) -> str:
    """Return statements dropping all tables (reverse FK order)."""
    cascade = " CASCADE" if engine == "postgres" else ""
    return "\n".join(f"DROP TABLE IF EXISTS {t}{cascade};" for t in reversed(TABLES))
