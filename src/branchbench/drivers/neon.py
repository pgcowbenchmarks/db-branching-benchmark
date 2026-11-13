"""Copy-on-write branch provisioning on Neon (serverless Postgres).

Neon branches are copy-on-write: a branch shares the parent's storage and
diverges only on write, so branch creation is a near-constant-time metadata
operation independent of dataset size -- Neon serverless Postgres, the technology Databricks Lakebase is built on, on a
publicly available service (free tier + REST API), so anyone can reproduce this
datapoint anonymously.

Provisioning ("no environment -> ready to run the first query") =
    create_branch (CoW) with a read-write endpoint  +  connect.
Reset = discard + cut fresh (delete branch + create a new one).

One parent branch per scale (`parent-<scale>`) holds that scale's
production-shaped data; per-cycle measured branches are cut from it.

Auth: set NEON_API_KEY (https://console.neon.tech -> Account -> API keys).
Config (engines.neon): api_key_env, project_id (optional; auto-created if
omitted), region_id, pg_version.
"""
from __future__ import annotations

import os
import time
import urllib.parse
import uuid

import requests

from . import _pg
from .base import Element, ProvisionedEnv, ProvisioningStrategy, READ_QUERIES

API = "https://console.neon.tech/api/v2"


class NeonCowBranch(ProvisioningStrategy):
    num, name, engine = 7, "neon_cow_branch", "neon"
    elements = Element(True, True, "some(governed where DB provides)", True)
    limits = ("Production-shaped but ungoverned (Neon has no Unity-Catalog-style "
              "governance); CoW economics and a live real Postgres are fully present.")

    def __init__(self, cfg, engine_cfg):
        super().__init__(cfg, engine_cfg)
        self._s = None
        self._project_id = engine_cfg.get("project_id") or None
        self._default_branch_id = None
        self._parent_id = None

    # ---- REST plumbing ----
    def _session(self):
        if self._s is None:
            key = os.environ.get(self.engine_cfg.get("api_key_env", "NEON_API_KEY"))
            if not key:
                raise RuntimeError(
                    "NEON_API_KEY not set (https://console.neon.tech -> API keys)")
            self._s = requests.Session()
            self._s.headers.update({"Authorization": f"Bearer {key}",
                                    "Content-Type": "application/json"})
        return self._s

    def _req(self, method, path, **kw):
        r = self._session().request(method, f"{API}{path}", timeout=120, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Neon {method} {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.text else {}

    def _ensure_project(self):
        if self._project_id:
            # verify it exists / find default branch
            data = self._req("GET", f"/projects/{self._project_id}")
        else:
            body = {"project": {
                "name": "db-branching-benchmark",
                "pg_version": int(self.engine_cfg.get("pg_version", 16)),
                "region_id": self.engine_cfg.get("region_id", "aws-us-east-2")}}
            data = self._req("POST", "/projects", json=body)
            self._project_id = data["project"]["id"]
            print(f"  neon: created project {self._project_id}")
        branches = self._req("GET", f"/projects/{self._project_id}/branches")["branches"]
        default = next((b for b in branches if b.get("default")), branches[0])
        self._default_branch_id = default["id"]

    def _create_branch(self, parent_id, name):
        """Create a CoW branch with a read-write endpoint; return (branch_id, uri)."""
        body = {"branch": {"parent_id": parent_id, "name": name},
                "endpoints": [{"type": "read_write"}]}
        data = self._req("POST", f"/projects/{self._project_id}/branches", json=body)
        bid = data["branch"]["id"]
        uris = data.get("connection_uris") or []
        uri = uris[0]["connection_uri"] if uris else None
        return bid, uri

    def _delete_branch(self, branch_id):
        try:
            self._req("DELETE", f"/projects/{self._project_id}/branches/{branch_id}")
        except Exception:
            pass

    def _branch_logical_size(self, branch_id):
        try:
            b = self._req("GET",
                          f"/projects/{self._project_id}/branches/{branch_id}")["branch"]
            v = b.get("logical_size")
            return int(v) if v is not None else None
        except Exception:
            return None

    @staticmethod
    def _pooled_host(host: str) -> str:
        """Rewrite a Neon endpoint host to its PgBouncer pooler form.

        Neon fronts every branch endpoint with a built-in PgBouncer pooler
        (transaction mode) reached at the `-pooler` host: connections are
        recycled instead of spinning up a fresh Postgres backend per connect,
        which is exactly what a per-cycle test-provisioning workload wants
        (https://neon.tech/docs/connect/connection-pooling). The direct host
        `ep-<id>.<region>.aws.neon.tech` becomes
        `ep-<id>-pooler.<region>.aws.neon.tech`.
        """
        if not host or "-pooler." in host:
            return host
        head, _, tail = host.partition(".")
        return f"{head}-pooler.{tail}" if tail else host

    def _connect_uri(self, uri, retries=30):
        u = urllib.parse.urlparse(uri)
        dbname = u.path.lstrip("/") or "neondb"
        host = u.hostname
        if self.engine_cfg.get("use_pooler", True):
            host = self._pooled_host(host)
        return _pg.connect(host=host, port=u.port or 5432,
                           user=urllib.parse.unquote(u.username or ""),
                           password=urllib.parse.unquote(u.password or ""),
                           dbname=dbname, sslmode="require",
                           retries=retries, retry_delay=2.0)

    # ---- lifecycle ----
    def setup_scale(self, scale_name, scale_cfg, data_dir, data_files):
        super().setup_scale(scale_name, scale_cfg, data_dir, data_files)
        self._ensure_project()
        parent_name = f"parent-{scale_name}"
        branches = self._req("GET", f"/projects/{self._project_id}/branches")["branches"]
        existing = next((b for b in branches if b.get("name") == parent_name), None)
        if existing:
            self._parent_id = existing["id"]
            eps = self._req("GET",
                            f"/projects/{self._project_id}/branches/{self._parent_id}/endpoints"
                            )["endpoints"]
            if not eps:
                self._req("POST", f"/projects/{self._project_id}/endpoints",
                          json={"endpoint": {"branch_id": self._parent_id,
                                             "type": "read_write"}})
            uri = self._req("GET",
                            f"/projects/{self._project_id}/connection_uri",
                            params={"branch_id": self._parent_id,
                                    "database_name": "neondb",
                                    "role_name": self.engine_cfg.get("role", "neondb_owner")}
                            ).get("uri")
        else:
            self._parent_id, uri = self._create_branch(self._default_branch_id, parent_name)
        conn = self._connect_uri(uri)
        loaded = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='orders'"
        ).fetchone()[0]
        if not loaded:
            _pg.apply_schema(conn, "postgres", with_indexes=False)
            _pg.copy_load(conn, data_files, with_indexes=True, engine="postgres")
        conn.close()

    def provision(self) -> ProvisionedEnv:
        bid = f"cyc-{uuid.uuid4().hex[:10]}"
        t0 = time.perf_counter()
        branch_id, uri = self._create_branch(self._parent_id, bid)
        branch_create_s = time.perf_counter() - t0
        conn = self._connect_uri(uri)
        conn.execute("SELECT 1").fetchone()  # ready to run the first query
        conn.close()
        return ProvisionedEnv(
            branch_id, conn_factory=lambda: self._connect_uri(uri, retries=5),
            meta={"branch_id": branch_id, "uri": uri, "branch_create_s": branch_create_s})

    def reset(self, env):
        self._delete_branch(env.meta["branch_id"])
        bid = f"cyc-{uuid.uuid4().hex[:10]}"
        branch_id, uri = self._create_branch(self._parent_id, bid)
        env.meta["branch_id"] = branch_id
        env.meta["uri"] = uri

    def teardown(self, env):
        self._delete_branch(env.meta["branch_id"])

    def storage_bytes(self, env, after_write=False):
        return self._branch_logical_size(env.meta["branch_id"])

    def representative_write(self, env):
        conn = env.conn_factory()
        _pg.representative_write(conn)
        conn.close()

    def steady_state_latencies(self, env, n):
        conn = env.conn_factory()
        lat = _pg.run_reads(conn, READ_QUERIES, n)
        conn.close()
        return lat
