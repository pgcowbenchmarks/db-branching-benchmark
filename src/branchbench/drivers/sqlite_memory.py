"""Strategy 1: in-memory fake (SQLite :memory:) -- the low-fidelity floor.

Per the benchmark methodology it is seeded with the SAME production-shaped dataset at
each scale (not a minimal fixture). At ~100 GB an in-memory engine cannot hold
the data; that failure (OOM/MemoryError) is the recorded result, not an
exemption from the data-size axis.
"""
from __future__ import annotations

import sqlite3

from . import base
from .. import datagen, schema
from .base import Element, ProvisionedEnv, ProvisioningStrategy


class InMemoryFake(ProvisioningStrategy):
    num, name, engine = 1, "in_memory_fake", "sqlite"
    elements = Element("yes(no real DB)", False, False, "n/a")
    limits = ("Wrong dialect/stored logic (not the real engine); cannot hold "
              "production-scale data; catches no data-dependent defect.")
    may_fail_at_large_scale = True

    def _build(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(schema.ddl("sqlite"))
        for table in schema.TABLES:
            ncols = len(next(datagen.sqlite_rows(self.data_files[table])))
            placeholders = ",".join("?" * ncols)
            conn.executemany(
                f"INSERT INTO {table} VALUES ({placeholders})",
                datagen.sqlite_rows(self.data_files[table]),
            )
        conn.commit()
        return conn

    def provision(self) -> ProvisionedEnv:
        conn = self._build()
        return ProvisionedEnv("sqlite_mem", conn_factory=lambda: conn, meta={"conn": conn})

    def reset(self, env):
        # Discard and rebuild a fresh in-memory database.
        try:
            env.meta["conn"].close()
        except Exception:
            pass
        env.meta["conn"] = self._build()

    def teardown(self, env):
        try:
            env.meta["conn"].close()
        except Exception:
            pass

    def storage_bytes(self, env, after_write=False):
        # In-memory: report resident page bytes as a proxy for its footprint.
        try:
            c = env.meta["conn"]
            page_count = c.execute("PRAGMA page_count").fetchone()[0]
            page_size = c.execute("PRAGMA page_size").fetchone()[0]
            return int(page_count) * int(page_size)
        except Exception:
            return None
