#!/usr/bin/env python3
"""Sweep per-cycle provisioning cost across a range of parent sizes (1 GB … 128 GB).

Fills in the enterprise-scale curve with multiple points per line so the
flat-CoW / rising-copy contrast is a sampled trend, not two endpoints. Data is
generated server-side (branchbench.datagen_sql) at each size — no client uplink.

  --engine rds   : RDS PostgreSQL full copy via CREATE DATABASE … TEMPLATE
                   (reads engines.rds_postgres from config/cloud.yaml; DB-password
                   auth, no AWS token needed). Writes results/sweep_rds.json.
  --engine neon  : Neon copy-on-write branch-create (NEON_API_KEY/PROJECT_ID).
                   Writes results/sweep_neon.json.

    python scripts/large_scale_sweep.py --engine rds  --sizes 1,8,32,64 --runs 3
    python scripts/large_scale_sweep.py --engine neon --sizes 1,8,32,64 --runs 5

Each size's parent is created, measured, then DELETED (so storage stays bounded
and Neon storage billing stops). Results accumulate incrementally per size.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from branchbench.drivers import _pg  # noqa: E402
from branchbench.datagen_sql import generate_server_side, db_size_bytes  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def chunk_for(insert_target_bytes: int) -> int:
    """~4-6 chunks per size for progress granularity without overshoot."""
    orders = insert_target_bytes / 520  # ~520 B/order (table-only)
    return max(200_000, min(20_000_000, int(orders / 4)))


# ---------------- RDS: CREATE DATABASE … TEMPLATE (full physical copy) --------
def sweep_rds(sizes, runs):
    e = yaml.safe_load(open(ROOT / "config" / "cloud.yaml"))["engines"]["rds_postgres"]

    def adm(db="postgres"):
        return _pg.connect(e["host"], e.get("port", 5432), e["user"], e["password"],
                           db, sslmode=e.get("sslmode", "require"), retries=10, retry_delay=3.0)

    out = ROOT / "results" / "sweep_rds.json"
    acc = json.loads(out.read_text()) if out.exists() else {}
    for gb in sizes:
        parent = f"bench_sweep_{gb}g"
        insert_target = int(gb * 1e9 * 0.82)
        c = adm()
        if not c.execute("SELECT 1 FROM pg_database WHERE datname=%s", (parent,)).fetchone():
            c.execute(f'CREATE DATABASE "{parent}"')
        c.close()
        pc = adm(parent)
        if db_size_bytes(pc) < insert_target:
            print(f"[rds {gb}GB] seeding …", flush=True)
            generate_server_side(pc, insert_target, chunk_orders=chunk_for(insert_target))
        parent_bytes = db_size_bytes(pc)
        pc.close()
        times = []
        for i in range(runs + 1):
            copy = f"bb_cp_{uuid.uuid4().hex[:8]}"
            a = adm()
            t0 = time.perf_counter()
            a.execute(f'CREATE DATABASE "{copy}" TEMPLATE "{parent}"')
            dt = time.perf_counter() - t0
            a.execute(f'DROP DATABASE "{copy}" WITH (FORCE)')
            a.close()
            print(f"[rds {gb}GB] copy run {i}{' (cold)' if i == 0 else ''}: {dt:.2f}s", flush=True)
            if i > 0:
                times.append(dt)
        # drop the parent to reclaim space
        a = adm()
        a.execute(f'DROP DATABASE "{parent}" WITH (FORCE)')
        a.close()
        acc[str(gb)] = {"parent_bytes": parent_bytes, "copy_median_s": statistics.median(times),
                        "runs_s": times}
        out.write_text(json.dumps(acc, indent=2))
        print(f"[rds {gb}GB] >> copy median {statistics.median(times):.1f}s "
              f"(parent {parent_bytes/1e9:.1f}GB)", flush=True)
    print(f"wrote {out}", flush=True)


# ---------------- Neon: copy-on-write branch-create ---------------------------
def sweep_neon(sizes, runs):
    import requests
    API = "https://console.neon.tech/api/v2"
    key, pid = os.environ["NEON_API_KEY"], os.environ["NEON_PROJECT_ID"]
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def req(m, p, **kw):
        r = requests.request(m, f"{API}{p}", headers=h, timeout=180, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Neon {m} {p} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}

    def pooled(host):
        head, _, tail = host.partition(".")
        return f"{head}-pooler.{tail}" if tail and "-pooler." not in host else host

    def connect(uri, direct):
        u = urllib.parse.urlparse(uri)
        host = u.hostname if direct else pooled(u.hostname)
        return _pg.connect(host, u.port or 5432, urllib.parse.unquote(u.username or ""),
                           urllib.parse.unquote(u.password or ""),
                           u.path.lstrip("/") or "neondb", sslmode="require",
                           retries=20, retry_delay=2.0)

    default = next(b for b in req("GET", f"/projects/{pid}/branches")["branches"] if b.get("default"))
    out = ROOT / "results" / "sweep_neon.json"
    acc = json.loads(out.read_text()) if out.exists() else {}
    for gb in sizes:
        insert_target = int(gb * 1e9 * 0.82)
        d = req("POST", f"/projects/{pid}/branches",
                json={"branch": {"parent_id": default["id"], "name": f"sweep-{gb}g"},
                      "endpoints": [{"type": "read_write"}]})
        pbid, puri = d["branch"]["id"], d["connection_uris"][0]["connection_uri"]
        print(f"[neon {gb}GB] seeding …", flush=True)
        conn = connect(puri, direct=True)
        generate_server_side(conn, insert_target, chunk_orders=chunk_for(insert_target))
        conn.close()
        parent_bytes = req("GET", f"/projects/{pid}/branches/{pbid}")["branch"].get("logical_size")
        times = []
        for i in range(runs + 1):
            t0 = time.perf_counter()
            cb = req("POST", f"/projects/{pid}/branches",
                     json={"branch": {"parent_id": pbid, "name": f"cyc-{uuid.uuid4().hex[:8]}"},
                           "endpoints": [{"type": "read_write"}]})
            dt = time.perf_counter() - t0
            try:
                c = connect(cb["connection_uris"][0]["connection_uri"], direct=False)
                c.execute("SELECT 1").fetchone(); c.close()
            except Exception:
                pass
            req("DELETE", f"/projects/{pid}/branches/{cb['branch']['id']}")
            print(f"[neon {gb}GB] branch run {i}{' (cold)' if i == 0 else ''}: {dt:.3f}s", flush=True)
            if i > 0:
                times.append(dt)
        req("DELETE", f"/projects/{pid}/branches/{pbid}")  # reclaim storage
        acc[str(gb)] = {"parent_bytes": parent_bytes, "branch_create_median_s": statistics.median(times),
                        "runs_s": times}
        out.write_text(json.dumps(acc, indent=2))
        print(f"[neon {gb}GB] >> branch-create median {statistics.median(times):.3f}s "
              f"(parent {(parent_bytes or 0)/1e9:.1f}GB)", flush=True)
    print(f"wrote {out}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["rds", "neon"], required=True)
    ap.add_argument("--sizes", default="1,8,32,64")
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    sizes = [int(x) for x in args.sizes.split(",")]
    (sweep_rds if args.engine == "rds" else sweep_neon)(sizes, args.runs)


if __name__ == "__main__":
    main()
