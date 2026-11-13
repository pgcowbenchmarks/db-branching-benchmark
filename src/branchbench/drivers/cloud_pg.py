"""Docker-free "fresh fixture" provisioning for a managed Postgres endpoint.

Serves RDS PostgreSQL and Aurora PostgreSQL (and any managed Postgres): each
cycle creates a brand-new database on the endpoint and COPY-loads the scale's
production-shaped data into it. This is the "copy" datapoint whose provisioning
time and storage rise with dataset size -- the contrast to CoW branching
(Neon branch / Aurora clone), on the SAME data-size axis.

No Docker, no local parent container: it drives the managed endpoint directly
via engine_cfg host/port/user/password (populated from Terraform outputs by
scripts/gen_config.py). `sslmode` defaults to `require` for managed endpoints.
"""
from __future__ import annotations

import uuid

from . import _pg
from .base import Element, ProvisionedEnv, ProvisioningStrategy, READ_QUERIES


class CloudPgFreshFixture(ProvisioningStrategy):
    num, name, engine = 2, "cloud_pg_fresh_fixture", "cloud_postgres"
    elements = Element(False, True, True, False)
    limits = ("Full-fidelity production-shaped data, but provisioning time and "
              "storage scale with data size (a fresh CREATE DATABASE + full load "
              "every cycle) -- the economics CoW branching removes.")

    def _admin(self, dbname=None):
        e = self.engine_cfg
        return _pg.connect(e["host"], e.get("port", 5432), e["user"], e["password"],
                           dbname or e.get("admin_db", "postgres"),
                           sslmode=e.get("sslmode", "require"), retries=30, retry_delay=3.0)

    def _build(self, dbname):
        admin = self._admin()
        _pg.create_database(admin, dbname)
        admin.close()
        conn = self._admin(dbname)
        _pg.apply_schema(conn, "postgres", with_indexes=False)
        _pg.copy_load(conn, self.data_files, with_indexes=True, engine="postgres")
        conn.close()

    def provision(self) -> ProvisionedEnv:
        dbname = f"bb_fresh_{uuid.uuid4().hex[:10]}"
        self._build(dbname)
        return ProvisionedEnv(dbname, conn_factory=lambda: self._admin(dbname),
                              meta={"dbname": dbname})

    def reset(self, env):
        self._build(env.meta["dbname"])  # drop / recreate / seed

    def teardown(self, env):
        admin = self._admin()
        _pg.drop_database(admin, env.meta["dbname"])
        admin.close()

    def storage_bytes(self, env, after_write=False):
        conn = self._admin(env.meta["dbname"])
        b = _pg.database_size_bytes(conn, env.meta["dbname"])
        conn.close()
        return b

    def representative_write(self, env):
        conn = self._admin(env.meta["dbname"])
        _pg.representative_write(conn)
        conn.close()

    def steady_state_latencies(self, env, n):
        conn = self._admin(env.meta["dbname"])
        lat = _pg.run_reads(conn, READ_QUERIES, n)
        conn.close()
        return lat
