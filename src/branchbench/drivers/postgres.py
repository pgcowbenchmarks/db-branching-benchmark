"""PostgreSQL provisioning strategies 2-6 (the primary-engine baselines).

All strategies seed from one shared, production-shaped parent database per scale
held in a long-lived parent container, so only the provisioning MECHANISM varies
across a shared data-size axis (benchmark methodology).

  2 fresh_fixture         drop / create / seed each cycle
  3 txn_rollback          one shared instance, BEGIN ... ROLLBACK
  4 ephemeral_container    throwaway container per cycle, seeded
  5 full_restore          fresh DB, pg_restore a full copy of the parent
  6 copy_based_branch      separate instance per branch + full data copy
                           (Supabase-style clone; measured locally as
                           container + pg_restore, labeled 'copy-based')
"""
from __future__ import annotations

import uuid
from pathlib import Path

from . import _docker, _pg
from .. import schema
from .base import Element, ProvisionedEnv, ProvisioningStrategy, READ_QUERIES

PARENT_CONTAINER = "bb-pg-parent"
ARCHIVE_MOUNT = "/archive"


def _host_archive_dir(data_dir: Path) -> Path:
    # Scale-INDEPENDENT archive dir: the parent container's /archive bind-mount
    # is fixed at first start, so it must not depend on which scale started
    # first. data_dir is results/data/<scale>; its parent (results/data) is
    # shared across scales. Absolute path required for Docker bind mounts.
    d = (data_dir.parent / "pg_archive").resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parent_db(scale: str) -> str:
    return f"branchbench_parent_{scale}"


def ensure_parent_container(engine_cfg: dict, data_dir: Path) -> None:
    """Start the long-lived parent container (with archive mount) if absent."""
    if _docker.exists(PARENT_CONTAINER):
        return
    host_archive = _host_archive_dir(data_dir)
    _docker.run_postgres(
        PARENT_CONTAINER, engine_cfg["port"], engine_cfg["container_image"],
        password=engine_cfg["password"], db=engine_cfg["admin_db"],
        user=engine_cfg["user"],
        volumes=[(str(host_archive), ARCHIVE_MOUNT)],
    )


def ensure_parent_loaded(engine_cfg: dict, scale: str, data_files: dict, data_dir: Path) -> None:
    """Idempotently load the scale's parent DB with the generated data."""
    admin = _pg.connect(engine_cfg["host"], engine_cfg["port"], engine_cfg["user"],
                        engine_cfg["password"], engine_cfg["admin_db"], retries=30)
    dbname = _parent_db(scale)
    row = admin.execute(
        "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
    if not row:
        _pg.create_database(admin, dbname)
    admin.close()
    conn = _pg.connect(engine_cfg["host"], engine_cfg["port"], engine_cfg["user"],
                       engine_cfg["password"], dbname)
    loaded = conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name='orders'"
    ).fetchone()[0]
    if not loaded:
        _pg.apply_schema(conn, "postgres", with_indexes=False)
        _pg.copy_load(conn, data_files, with_indexes=True, engine="postgres")
    conn.close()


def ensure_parent_archive(engine_cfg: dict, scale: str) -> str:
    """pg_dump the parent DB to a custom-format archive under the mount (once).

    Returns the in-container archive path."""
    in_container = f"{ARCHIVE_MOUNT}/parent_{scale}.dump"
    # Skip if already present.
    ls = _docker.exec_(PARENT_CONTAINER, ["sh", "-c", f"test -f {in_container} && echo yes || echo no"], check=False)
    if "yes" in ls:
        return in_container
    _docker.exec_(PARENT_CONTAINER, [
        "pg_dump", "-U", engine_cfg["user"], "-d", _parent_db(scale),
        "-Fc", "-f", in_container,
    ])
    return in_container


class _PgBase(ProvisioningStrategy):
    engine = "postgres"

    def _admin(self):
        e = self.engine_cfg
        return _pg.connect(e["host"], e["port"], e["user"], e["password"], e["admin_db"])

    def _conn(self, dbname):
        e = self.engine_cfg
        return _pg.connect(e["host"], e["port"], e["user"], e["password"], dbname)

    def setup_scale(self, scale_name, scale_cfg, data_dir, data_files):
        super().setup_scale(scale_name, scale_cfg, data_dir, data_files)
        ensure_parent_container(self.engine_cfg, data_dir)
        ensure_parent_loaded(self.engine_cfg, scale_name, data_files, data_dir)


class PostgresFreshFixture(_PgBase):
    num, name = 2, "pg_fresh_fixture"
    elements = Element("partial", True, False, False)
    limits = ("Synthetic/hand-built seed -> misses skew, real nulls, and "
              "referential-integrity-at-volume failures; rebuild cost grows with seed size.")

    def _build(self, dbname):
        admin = self._admin()
        _pg.create_database(admin, dbname)
        admin.close()
        conn = self._conn(dbname)
        _pg.apply_schema(conn, "postgres", with_indexes=False)
        _pg.copy_load(conn, self.data_files, with_indexes=True, engine="postgres")
        conn.close()

    def provision(self) -> ProvisionedEnv:
        dbname = f"bb_fresh_{uuid.uuid4().hex[:10]}"
        self._build(dbname)
        return ProvisionedEnv(dbname, conn_factory=lambda: self._conn(dbname),
                              meta={"dbname": dbname})

    def reset(self, env):
        # Fresh fixture reset = drop / recreate / seed (explicit definition).
        self._build(env.meta["dbname"])

    def teardown(self, env):
        admin = self._admin()
        _pg.drop_database(admin, env.meta["dbname"])
        admin.close()

    def storage_bytes(self, env, after_write=False):
        conn = self._conn(env.meta["dbname"])
        b = _pg.database_size_bytes(conn, env.meta["dbname"])
        conn.close()
        return b


class PostgresTxnRollback(_PgBase):
    num, name = 3, "pg_txn_rollback"
    elements = Element("partial", True, False, "partial")
    limits = ("Can't reset DDL/schema changes, run destructive tests, or isolate "
              "concurrent developers on the shared instance; data is still synthetic.")

    def setup_scale(self, scale_name, scale_cfg, data_dir, data_files):
        super().setup_scale(scale_name, scale_cfg, data_dir, data_files)
        # Shared fixture: one persistent instance, loaded once (== parent DB).
        self._shared_db = _parent_db(scale_name)
        self._persistent = self._conn(self._shared_db)
        self._persistent.autocommit = False

    def provision(self) -> ProvisionedEnv:
        # Nothing rebuilt: begin a transaction on the shared instance.
        self._persistent.execute("BEGIN")
        return ProvisionedEnv("shared_txn", conn_factory=lambda: self._persistent,
                              meta={"shared": True})

    def reset(self, env):
        self._persistent.execute("ROLLBACK")

    def storage_bytes(self, env, after_write=False):
        conn = self._conn(self._shared_db)
        b = _pg.database_size_bytes(conn, self._shared_db)
        conn.close()
        return b  # NOTE: shared across all cycles/devs, not per-env

    def teardown_scale(self):
        try:
            self._persistent.close()
        except Exception:
            pass


class PostgresEphemeralContainer(_PgBase):
    num, name = 4, "pg_ephemeral_container"
    elements = Element("partial", True, False, False)
    limits = ("Empty/synthetic seed (not production-shaped); container startup is "
              "seconds-scale (not inner-loop-fast); cost grows with image + seed.")

    def setup_scale(self, scale_name, scale_cfg, data_dir, data_files):
        # Needs data + parent-container archive availability, but seeds fresh.
        super().setup_scale(scale_name, scale_cfg, data_dir, data_files)

    def provision(self) -> ProvisionedEnv:
        e = self.engine_cfg
        name = f"{e['container_name_prefix']}-eph-{uuid.uuid4().hex[:8]}"
        port = _docker.free_port()
        _docker.run_postgres(name, port, e["container_image"], password=e["password"],
                             db="bench", user=e["user"])
        conn = _pg.connect(e["host"], port, e["user"], e["password"], "bench", retries=30)
        _pg.apply_schema(conn, "postgres", with_indexes=False)
        _pg.copy_load(conn, self.data_files, with_indexes=True, engine="postgres")
        conn.close()
        return ProvisionedEnv(
            name, conn_factory=lambda: _pg.connect(e["host"], port, e["user"], e["password"], "bench"),
            meta={"container": name, "port": port})

    def reset(self, env):
        # Ephemeral container reset = dispose; next cycle starts fresh.
        _docker.stop_rm(env.meta["container"])

    def teardown(self, env):
        _docker.stop_rm(env.meta["container"])

    def storage_bytes(self, env, after_write=False):
        return _docker.container_disk_bytes(env.meta["container"])


class PostgresFullRestore(_PgBase):
    num, name = 5, "pg_full_restore"
    elements = Element(False, True, True, False)
    limits = ("Fidelity is fine, economics aren't: provisioning time and storage "
              "scale with data size, so it gets pushed to integration/CI.")

    def setup_scale(self, scale_name, scale_cfg, data_dir, data_files):
        super().setup_scale(scale_name, scale_cfg, data_dir, data_files)
        self._archive = ensure_parent_archive(self.engine_cfg, scale_name)

    def _restore(self, dbname):
        admin = self._admin()
        _pg.create_database(admin, dbname)
        admin.close()
        _docker.exec_(PARENT_CONTAINER, [
            "pg_restore", "-U", self.engine_cfg["user"], "-d", dbname,
            "--no-owner", "-j", "2", self._archive,
        ])

    def provision(self) -> ProvisionedEnv:
        dbname = f"bb_restore_{uuid.uuid4().hex[:10]}"
        self._restore(dbname)
        return ProvisionedEnv(dbname, conn_factory=lambda: self._conn(dbname),
                              meta={"dbname": dbname})

    def reset(self, env):
        admin = self._admin()
        _pg.drop_database(admin, env.meta["dbname"])
        admin.close()

    def teardown(self, env):
        admin = self._admin()
        _pg.drop_database(admin, env.meta["dbname"])
        admin.close()

    def storage_bytes(self, env, after_write=False):
        conn = self._conn(env.meta["dbname"])
        b = _pg.database_size_bytes(conn, env.meta["dbname"])
        conn.close()
        return b

    def representative_write(self, env):
        conn = self._conn(env.meta["dbname"])
        _pg.representative_write(conn)
        conn.close()

    def steady_state_latencies(self, env, n):
        conn = self._conn(env.meta["dbname"])
        lat = _pg.run_reads(conn, READ_QUERIES, n)
        conn.close()
        return lat


class PostgresCopyBasedBranch(_PgBase):
    num, name = 6, "pg_copy_based_branch"
    elements = Element(False, True, True, False)
    limits = ("Reaching production-shaped data requires a full copy, so time and "
              "storage scale with size -- the same ceiling as full restore, under a "
              "'branch' label. (Measured locally as fresh instance + pg_restore; "
              "Supabase-style clone is the real-world referent.)")

    def setup_scale(self, scale_name, scale_cfg, data_dir, data_files):
        super().setup_scale(scale_name, scale_cfg, data_dir, data_files)
        self._archive_host = str(_host_archive_dir(data_dir) / f"parent_{scale_name}.dump")
        # Ensure the archive exists (written under the shared mount).
        ensure_parent_archive(self.engine_cfg, scale_name)

    def provision(self) -> ProvisionedEnv:
        e = self.engine_cfg
        name = f"{e['container_name_prefix']}-clone-{uuid.uuid4().hex[:8]}"
        port = _docker.free_port()
        # Separate instance per branch (mount the shared archive dir).
        _docker.run_postgres(name, port, e["container_image"], password=e["password"],
                             db="bench", user=e["user"])
        # Confirm the server is truly ready before the parallel restore (pg_isready
        # can pass before the instance accepts the -j workers).
        _pg.connect(e["host"], port, e["user"], e["password"], "bench", retries=30).close()
        # Copy the archive into the fresh instance, then restore (data copied in).
        import subprocess
        subprocess.run(["docker", "cp", self._archive_host, f"{name}:/tmp/parent.dump"], check=True)
        _docker.exec_(name, ["pg_restore", "-U", e["user"], "-d", "bench",
                             "--no-owner", "-j", "2", "/tmp/parent.dump"])
        return ProvisionedEnv(
            name, conn_factory=lambda: _pg.connect(e["host"], port, e["user"], e["password"], "bench"),
            meta={"container": name, "port": port})

    def reset(self, env):
        _docker.stop_rm(env.meta["container"])

    def teardown(self, env):
        _docker.stop_rm(env.meta["container"])

    def storage_bytes(self, env, after_write=False):
        return _docker.container_disk_bytes(env.meta["container"])

    def steady_state_latencies(self, env, n):
        conn = env.conn_factory()
        lat = _pg.run_reads(conn, READ_QUERIES, n)
        conn.close()
        return lat
