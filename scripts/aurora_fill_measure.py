#!/usr/bin/env python3
"""Fill in the missing Aurora CoW-clone datapoints (1 / 64 / 128 GB) on a TEMP
cluster, so the Aurora line has real measured points there instead of interpolation.

Two subcommands, split so seeding (DB-password auth, survives AWS-cred expiry, runs
for hours) and cloning (AWS API, needs live creds, ~7 min each) can be run in
separate cred windows:

  seed  --size N     grow the temp parent to ~N GB in place (server-side generate,
                     resume-safe reconnect loop). No AWS creds needed.
  clone --size N     clone the temp cluster N_RUNS+1 times (cold + measured), record
                     the provisioning median to results/aurora_fill.json. Needs creds.

The parent can only grow (Aurora clone = whole cluster), so measure smallest→largest:
1 GB, then 64 GB, then 128 GB.
"""
from __future__ import annotations
import argparse, json, os, statistics, sys, time, uuid
from pathlib import Path
import boto3, yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from branchbench.drivers import _pg                                   # noqa: E402
from branchbench.datagen_sql import generate_server_side, db_size_bytes  # noqa: E402

# The temp Aurora cluster to grow + clone. HOST is its writer endpoint:
#   aws rds describe-db-clusters --db-cluster-identifier <id> --query "DBClusters[0].Endpoint"
CLUSTER = os.environ.get("AURORA_FILL_CLUSTER", "bbench-aurora-fill")
HOST = os.environ.get("AURORA_FILL_HOST", "")
PARENT = "branchbench_parent"
OUT = ROOT / "results" / "aurora_fill.json"

E = yaml.safe_load(open(ROOT / "config" / "cloud.yaml"))["engines"]["aurora_postgres"]
REGION = E["aws"]["region"]
ICLASS = E["aws"].get("instance_class", "db.r6g.large")


def conn(host=None, db="postgres"):
    if not (host or HOST):
        sys.exit("set AURORA_FILL_HOST to the temp cluster's writer endpoint")
    return _pg.connect(host or HOST, 5432, E["user"], E["password"], db,
                       sslmode="require", retries=30, retry_delay=3.0)


def cmd_seed(args):
    target = int(args.size * 1e9 * 0.82)   # indexes add the remainder → ~size GB on disk
    a = conn()
    if not a.execute("SELECT 1 FROM pg_database WHERE datname=%s", (PARENT,)).fetchone():
        a.execute(f'CREATE DATABASE "{PARENT}"'); print(f"created {PARENT}", flush=True)
    a.close()
    print(f"[seed] target ~{args.size} GB (insert {target/1e9:.1f} GB) — resume-safe", flush=True)
    while True:
        pc = conn(db=PARENT)
        try:
            cur = db_size_bytes(pc)
            if cur >= target:
                print(f"[seed] already at {cur/1e9:.1f} GB ≥ target; done", flush=True)
                pc.close(); break
            generate_server_side(pc, target, chunk_orders=150_000)
            pc.close(); break
        except Exception as e:
            print(f"  seed drop ({str(e)[:70]}); reconnect+resume", flush=True)
            try: pc.close()
            except Exception: pass
            time.sleep(5)
    pc = conn(db=PARENT); fin = db_size_bytes(pc); pc.close()
    print(f"[seed] parent now {fin/1e9:.1f} GB", flush=True)


def cmd_clone(args):
    rds = boto3.Session(region_name=REGION).client("rds")
    src = rds.describe_db_clusters(DBClusterIdentifier=CLUSTER)["DBClusters"][0]
    sgs = [g["VpcSecurityGroupId"] for g in src["VpcSecurityGroups"]]
    subnet = src["DBSubnetGroup"]; engine = src["Engine"]
    pc = conn(db=PARENT); parent_bytes = db_size_bytes(pc); pc.close()
    print(f"[clone] parent {parent_bytes/1e9:.1f} GB; {args.runs} measured + 1 cold", flush=True)

    def do_clone(cid):
        rds.restore_db_cluster_to_point_in_time(
            DBClusterIdentifier=cid, SourceDBClusterIdentifier=CLUSTER,
            RestoreType="copy-on-write", UseLatestRestorableTime=True,
            VpcSecurityGroupIds=sgs, DBSubnetGroupName=subnet,
            Tags=[{"Key": "project", "Value": "db-branching-benchmark"}])
        rds.create_db_instance(DBInstanceIdentifier=f"{cid}-1", DBClusterIdentifier=cid,
                               Engine=engine, DBInstanceClass=ICLASS, PubliclyAccessible=True)
        rds.get_waiter("db_instance_available").wait(
            DBInstanceIdentifier=f"{cid}-1", WaiterConfig={"Delay": 15, "MaxAttempts": 160})
        return rds.describe_db_clusters(DBClusterIdentifier=cid)["DBClusters"][0]["Endpoint"]

    def drop_clone(cid):
        for fn, kw in ((rds.delete_db_instance, dict(DBInstanceIdentifier=f"{cid}-1", SkipFinalSnapshot=True)),
                       (rds.delete_db_cluster, dict(DBClusterIdentifier=cid, SkipFinalSnapshot=True))):
            try: fn(**kw)
            except Exception as ex: print(f"  (drop warn: {str(ex)[:70]})", flush=True)

    times = []
    for i in range(args.runs + 1):
        cid = f"bbench-fill-clone-{uuid.uuid4().hex[:8]}"
        t0 = time.perf_counter()
        host = do_clone(cid)
        c = conn(host=host, db=PARENT); c.execute("SELECT 1").fetchone(); c.close()
        dt = time.perf_counter() - t0
        drop_clone(cid)
        print(f"[clone {args.size}GB] run {i}{' (cold)' if i == 0 else ''}: {dt:.1f}s", flush=True)
        if i > 0: times.append(dt)

    acc = json.loads(OUT.read_text()) if OUT.exists() else {}
    acc[str(args.size)] = {"parent_bytes": parent_bytes, "parent_gb": round(parent_bytes/1e9, 1),
                           "clone_provision_median_s": statistics.median(times),
                           "clone_provision_runs_s": times, "instance_class": ICLASS}
    OUT.write_text(json.dumps(acc, indent=2))
    print(f"[clone {args.size}GB] >> median {statistics.median(times):.1f}s "
          f"(parent {parent_bytes/1e9:.1f} GB) -> wrote {OUT.name}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="do", required=True)
    s = sub.add_parser("seed"); s.add_argument("--size", type=float, required=True); s.set_defaults(f=cmd_seed)
    c = sub.add_parser("clone"); c.add_argument("--size", type=float, required=True)
    c.add_argument("--runs", type=int, default=2); c.set_defaults(f=cmd_clone)
    a = ap.parse_args(); a.f(a)
