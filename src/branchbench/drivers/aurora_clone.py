"""Copy-on-write clone provisioning on Amazon Aurora PostgreSQL.

Aurora "fast database cloning" is copy-on-write at the shared storage layer: a
clone shares the source cluster's pages and diverges only on write, so clone
*storage* is independent of dataset size. Unlike a serverless CoW branch
(Neon), the clone's *compute* is a full Aurora DB instance that must
boot, so the provisioning wall-clock includes instance startup (minutes, not
sub-second). Measuring this side-by-side with Neon is the point: it
separates "CoW storage economics" from "serverless instant compute."

Provisioning = restore_db_cluster_to_point_in_time(RestoreType='copy-on-write')
    + create_db_instance in the clone cluster + wait available + connect.
Reset = delete the clone (instance + cluster) + clone fresh.

Config (engines.aurora_postgres): host/port/user/password of the SOURCE cluster
(loaded once with the scale's data), plus aws.{region, source_cluster_id,
instance_class}. Requires AWS credentials (boto3) with RDS permissions.
"""
from __future__ import annotations

import time
import uuid

from . import _pg
from .base import Element, ProvisionedEnv, ProvisioningStrategy, READ_QUERIES


class AuroraCowClone(ProvisioningStrategy):
    num, name, engine = 6, "aurora_cow_clone", "aurora_postgres"
    elements = Element(True, True, "some(governed where DB provides)", True)
    limits = ("CoW at the storage layer (flat storage), but compute is a full "
              "Aurora instance that must boot, so provisioning wall-clock is not "
              "sub-second like a serverless branch.")

    def __init__(self, cfg, engine_cfg):
        super().__init__(cfg, engine_cfg)
        self._aws = engine_cfg.get("aws", {})
        self._rds = None
        self._src_meta = None
        self.parent_db = engine_cfg.get("parent_db", "branchbench_parent")

    def _client(self):
        if self._rds is None:
            import boto3
            self._rds = boto3.Session(
                profile_name=self._aws.get("profile"),
                region_name=self._aws.get("region", "us-west-2")).client("rds")
        return self._rds

    def _source(self):
        if self._src_meta is None:
            cid = self._aws["source_cluster_id"]
            self._src_meta = self._client().describe_db_clusters(
                DBClusterIdentifier=cid)["DBClusters"][0]
        return self._src_meta

    def _admin_conn(self, host=None, dbname="postgres"):
        ec = self.engine_cfg
        return _pg.connect(host=host or ec["host"], port=ec.get("port", 5432),
                           user=ec["user"], password=ec["password"], dbname=dbname,
                           sslmode=ec.get("sslmode", "require"), retries=30, retry_delay=3.0)

    # ---- lifecycle ----
    def setup_scale(self, scale_name, scale_cfg, data_dir, data_files):
        super().setup_scale(scale_name, scale_cfg, data_dir, data_files)
        # Load the scale's production-shaped data into the source cluster once.
        admin = self._admin_conn()
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname=%s",
                               (self.parent_db,)).fetchone()
        if not exists:
            _pg.create_database(admin, self.parent_db)
        admin.close()
        conn = self._admin_conn(dbname=self.parent_db)
        loaded = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='orders'"
        ).fetchone()[0]
        if not loaded:
            _pg.apply_schema(conn, "postgres", with_indexes=False)
            _pg.copy_load(conn, data_files, with_indexes=True, engine="postgres")
        conn.close()

    def _clone(self, clone_id):
        src = self._source()
        rds = self._client()
        rds.restore_db_cluster_to_point_in_time(
            DBClusterIdentifier=clone_id,
            SourceDBClusterIdentifier=src["DBClusterIdentifier"],
            RestoreType="copy-on-write", UseLatestRestorableTime=True,
            VpcSecurityGroupIds=[g["VpcSecurityGroupId"] for g in src["VpcSecurityGroups"]],
            DBSubnetGroupName=src["DBSubnetGroup"],
            Tags=[{"Key": "project", "Value": "db-branching-benchmark"}])
        rds.create_db_instance(
            DBInstanceIdentifier=f"{clone_id}-1", DBClusterIdentifier=clone_id,
            Engine=src["Engine"],
            DBInstanceClass=self._aws.get("instance_class", "db.r6g.large"),
            PubliclyAccessible=True)
        rds.get_waiter("db_instance_available").wait(
            DBInstanceIdentifier=f"{clone_id}-1",
            WaiterConfig={"Delay": 15, "MaxAttempts": 80})
        ep = rds.describe_db_clusters(DBClusterIdentifier=clone_id
                                      )["DBClusters"][0]["Endpoint"]
        return ep

    def _drop_clone(self, clone_id):
        rds = self._client()
        for fn, kw in (
            (rds.delete_db_instance, dict(DBInstanceIdentifier=f"{clone_id}-1",
                                          SkipFinalSnapshot=True)),
            (rds.delete_db_cluster, dict(DBClusterIdentifier=clone_id,
                                         SkipFinalSnapshot=True))):
            try:
                fn(**kw)
            except Exception:
                pass

    def provision(self) -> ProvisionedEnv:
        clone_id = f"bbench-clone-{uuid.uuid4().hex[:8]}"
        t0 = time.perf_counter()
        host = self._clone(clone_id)
        conn = self._admin_conn(host=host, dbname=self.parent_db)
        conn.execute("SELECT 1").fetchone()  # ready to run the first query
        conn.close()
        prov_s = time.perf_counter() - t0
        return ProvisionedEnv(
            clone_id,
            conn_factory=lambda: self._admin_conn(host=host, dbname=self.parent_db),
            meta={"clone_id": clone_id, "host": host, "provision_s": prov_s})

    def reset(self, env):
        self._drop_clone(env.meta["clone_id"])
        new_id = f"bbench-clone-{uuid.uuid4().hex[:8]}"
        host = self._clone(new_id)
        env.meta["clone_id"] = new_id
        env.meta["host"] = host

    def teardown(self, env):
        self._drop_clone(env.meta["clone_id"])

    def steady_state_latencies(self, env, n):
        conn = env.conn_factory()
        lat = _pg.run_reads(conn, READ_QUERIES, n)
        conn.close()
        return lat
