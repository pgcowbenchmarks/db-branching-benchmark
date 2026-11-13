#!/usr/bin/env python3
"""Aurora PostgreSQL fast-clone benchmark at chosen scales (server-side seeded).

Aurora "fast database cloning" is copy-on-write at the shared storage layer: a
clone shares the source cluster's pages and diverges only on write, so clone
*storage* is independent of dataset size. Unlike a serverless CoW branch
(Neon), the clone's *compute* is a full Aurora DB instance that must
boot, so provisioning wall-clock includes instance startup (minutes, not
sub-seconds). Measuring this is the point: it separates "CoW storage economics"
from "serverless instant compute."

Per requested size: seed the Aurora SOURCE cluster's parent DB server-side (no
client uplink), then measure N clone cycles:
  restore_db_cluster_to_point_in_time(RestoreType='copy-on-write')
  + create_db_instance + wait available + connect + drop clone.

    python scripts/aurora_large_scale_clone.py --sizes 2,8,32 --runs 3
    python scripts/aurora_large_scale_clone.py --sizes 256 --runs 2

Reads engines.aurora_postgres from config/cloud.yaml (host/user/password + aws.*).
Needs LIVE AWS credentials for the clone API calls (seeding uses the DB password).
Writes results/aurora_clone_scale.json (accumulates per size).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

import boto3
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from branchbench.drivers import _pg  # noqa: E402
from branchbench.datagen_sql import generate_server_side, db_size_bytes  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def chunk_for(b):
    # ~150k orders: each server-side INSERT...SELECT chunk completes in a few seconds
    # so the (idle-during-generation) laptop connection commits it inside the window
    # between link drops. Large chunks (20M) leave the socket idle for minutes and the
    # flaky link drops them every time, stalling the seed. Resumable via max(order_id).
    return 150_000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="2,8,32")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--parent-db", default="branchbench_parent")
    args = ap.parse_args()
    sizes = [int(x) for x in args.sizes.split(",")]

    e = yaml.safe_load(open(ROOT / "config" / "cloud.yaml"))["engines"]["aurora_postgres"]
    aws = e.get("aws", {})
    region = aws.get("region", "us-west-2")
    src_cluster = aws["source_cluster_id"]
    instance_class = aws.get("instance_class", "db.r6g.large")
    rds = boto3.Session(region_name=region).client("rds")

    def conn(host=None, db="postgres"):
        return _pg.connect(host or e["host"], e.get("port", 5432), e["user"], e["password"],
                           db, sslmode=e.get("sslmode", "require"), retries=30, retry_delay=3.0)

    def source_meta():
        return rds.describe_db_clusters(DBClusterIdentifier=src_cluster)["DBClusters"][0]

    def do_clone(clone_id):
        src = source_meta()
        rds.restore_db_cluster_to_point_in_time(
            DBClusterIdentifier=clone_id, SourceDBClusterIdentifier=src_cluster,
            RestoreType="copy-on-write", UseLatestRestorableTime=True,
            VpcSecurityGroupIds=[g["VpcSecurityGroupId"] for g in src["VpcSecurityGroups"]],
            DBSubnetGroupName=src["DBSubnetGroup"],
            Tags=[{"Key": "project", "Value": "db-branching-benchmark"}])
        rds.create_db_instance(
            DBInstanceIdentifier=f"{clone_id}-1", DBClusterIdentifier=clone_id,
            Engine=src["Engine"], DBInstanceClass=instance_class, PubliclyAccessible=True)
        rds.get_waiter("db_instance_available").wait(
            DBInstanceIdentifier=f"{clone_id}-1", WaiterConfig={"Delay": 15, "MaxAttempts": 120})
        return rds.describe_db_clusters(DBClusterIdentifier=clone_id)["DBClusters"][0]["Endpoint"]

    def drop_clone(clone_id):
        for fn, kw in ((rds.delete_db_instance, dict(DBInstanceIdentifier=f"{clone_id}-1", SkipFinalSnapshot=True)),
                       (rds.delete_db_cluster, dict(DBClusterIdentifier=clone_id, SkipFinalSnapshot=True))):
            try:
                fn(**kw)
            except Exception as ex:
                print(f"  (drop warn: {str(ex)[:80]})", flush=True)

    out = ROOT / "results" / "aurora_clone_scale.json"
    acc = json.loads(out.read_text()) if out.exists() else {}

    for gb in sizes:
        insert_target = int(gb * 1e9 * 0.82)
        # ---- seed the source cluster's parent DB to this size (RESUME-SAFE) ----
        # A multi-hour large seed on one connection will drop; generate_server_side
        # resumes from max(order_id), so we (a) never drop a partially-seeded parent
        # and (b) wrap the seed in a reconnect-and-resume loop. Only when the parent
        # is BELOW target for THIS size do we (re)seed; a smaller leftover parent from
        # a previous size is dropped once, then grown.
        a = conn()
        exists = a.execute("SELECT 1 FROM pg_database WHERE datname=%s", (args.parent_db,)).fetchone()
        cur = 0
        if exists:
            pc = conn(db=args.parent_db)
            cur = db_size_bytes(pc)
            pc.close()
        # if a leftover parent is far below this target AND has data from a smaller
        # size run, keep it and just grow it (max(order_id) resume handles the shape).
        if not exists:
            a.execute(f'CREATE DATABASE "{args.parent_db}"')
        a.close()
        print(f"[aurora {gb}GB] seeding source server-side (resume-safe; "
              f"start {cur/1e9:.1f} GB) …", flush=True)
        while True:
            pc = conn(db=args.parent_db)
            try:
                if db_size_bytes(pc) >= insert_target:
                    pc.close(); break
                generate_server_side(pc, insert_target, chunk_orders=chunk_for(insert_target))
                pc.close(); break
            except Exception as e:
                print(f"  seed connection dropped ({str(e)[:70]}); reconnecting + resuming …", flush=True)
                try:
                    pc.close()
                except Exception:
                    pass
                time.sleep(5)
        pc = conn(db=args.parent_db)
        parent_bytes = db_size_bytes(pc)
        pc.close()
        print(f"[aurora {gb}GB] parent seeded: {parent_bytes/1e9:.1f} GB", flush=True)
        # Aurora needs a moment for the write to be restorable to latest time.
        time.sleep(30)

        times = []
        for i in range(args.runs + 1):
            clone_id = f"bbench-clone-{uuid.uuid4().hex[:8]}"
            t0 = time.perf_counter()
            host = do_clone(clone_id)
            c = conn(host=host, db=args.parent_db)
            c.execute("SELECT 1").fetchone()   # ready to run the first query
            c.close()
            dt = time.perf_counter() - t0
            drop_clone(clone_id)
            print(f"[aurora {gb}GB] clone run {i}{' (cold)' if i == 0 else ''}: {dt:.1f}s", flush=True)
            if i > 0:
                times.append(dt)

        acc[str(gb)] = {"parent_bytes": parent_bytes,
                        "clone_provision_median_s": statistics.median(times),
                        "clone_provision_runs_s": times, "instance_class": instance_class}
        out.write_text(json.dumps(acc, indent=2))
        print(f"[aurora {gb}GB] >> clone median {statistics.median(times):.1f}s "
              f"(parent {parent_bytes/1e9:.1f}GB, storage is CoW/flat)", flush=True)
    print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    main()
