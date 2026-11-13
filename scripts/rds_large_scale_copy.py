#!/usr/bin/env python3
"""RDS PostgreSQL COPY-provisioning benchmark at LARGE scale (100 GB+).

The head-to-head partner for scripts/neon_large_scale.py. Seeds a parent database
server-side (branchbench.datagen_sql — no client uplink bottleneck), then measures
per-cycle *copy* provisioning at that scale with `CREATE DATABASE <copy> TEMPLATE
<parent>` — Postgres physically duplicates every page of the parent, so the
provisioning time scales with data size. This is the honest in-engine equivalent of
the fresh-fixture strategy at a scale where loading from external files is
impractical: it still hands each cycle a full, independent copy of the data.

Contrast with the CoW branch (neon_large_scale.py): the branch shares the parent's
pages (metadata op, flat in size), the TEMPLATE copy moves all the bytes (rises).

    python scripts/rds_large_scale_copy.py --target-gb 128 --runs 3
        # reads engines.rds_postgres from config/cloud.yaml (gen_config.py),
        # writes results/rds_large_scale_copy_<N>gb.json

Needs the DB password only (config/cloud.yaml) — no live AWS credentials, so it
survives STS-token expiry during a multi-hour run.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from branchbench.drivers import _pg  # noqa: E402
from branchbench.datagen_sql import generate_server_side, db_size_bytes  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def admin_conn(e, dbname="postgres"):
    return _pg.connect(e["host"], e.get("port", 5432), e["user"], e["password"],
                       dbname, sslmode=e.get("sslmode", "require"),
                       retries=10, retry_delay=3.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-gb", type=float, default=128)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--config", default=str(ROOT / "config" / "cloud.yaml"))
    ap.add_argument("--parent-db", default="bench_parent_lg")
    ap.add_argument("--chunk-orders", type=int, default=20_000_000)
    args = ap.parse_args()

    e = yaml.safe_load(open(args.config))["engines"]["rds_postgres"]
    scale = f"{int(args.target_gb)}gb"
    insert_target = int(args.target_gb * 1e9 * 0.82)  # indexes add the rest

    # ---- seed the parent database server-side ----
    adm = admin_conn(e)
    exists = adm.execute("SELECT 1 FROM pg_database WHERE datname=%s", (args.parent_db,)).fetchone()
    if not exists:
        adm.execute(f'CREATE DATABASE "{args.parent_db}"')
        print(f"created parent db {args.parent_db}", flush=True)
    adm.close()

    pconn = admin_conn(e, args.parent_db)
    have = db_size_bytes(pconn)
    if have < insert_target:
        print(f"seeding {args.parent_db} server-side to ~{args.target_gb} GB "
              f"(insert target {insert_target/1e9:.0f} GB) …", flush=True)
        seed_stats = generate_server_side(pconn, insert_target, chunk_orders=args.chunk_orders)
    else:
        seed_stats = {"note": "already seeded", "final_size_bytes": have}
    parent_bytes = db_size_bytes(pconn)
    pconn.close()
    print(f"parent size: {parent_bytes/1e9:.1f} GB", flush=True)

    out = ROOT / "results" / f"rds_large_scale_copy_{args.target_gb:g}gb.json"

    def flush_result(times, cold):
        """Persist after every completed copy so a mid-run connection drop
        (a real hazard on a laptop link during a ~36-min single statement)
        never loses an already-measured datapoint."""
        r = {
            "scale": scale, "target_gb": args.target_gb,
            "engine": "rds_postgres", "mechanism": "CREATE DATABASE TEMPLATE (in-engine physical copy)",
            "parent_bytes": parent_bytes, "seed_stats": seed_stats,
            "cold_run_s": cold,
            "copy_provision_runs_s": times,
        }
        if times:
            r["copy_provision_median_s"] = statistics.median(times)
            r["copy_provision_min_s"] = min(times)
            r["copy_provision_max_s"] = max(times)
        out.write_text(json.dumps(r, indent=2, default=str))
        return r

    # ---- measure CREATE DATABASE … TEMPLATE copy provisioning (cold + N) ----
    print(f"\nmeasuring CREATE DATABASE TEMPLATE copy x{args.runs} (+cold) …", flush=True)
    times, cold = [], None
    for i in range(args.runs + 1):
        copy = f"bb_copy_{uuid.uuid4().hex[:8]}"
        try:
            adm = admin_conn(e)
            # TEMPLATE copy requires no active sessions on the template; admin is on 'postgres'.
            t0 = time.perf_counter()
            adm.execute(f'CREATE DATABASE "{copy}" TEMPLATE "{args.parent_db}"')
            dt = time.perf_counter() - t0
            # ready to run the first query on the fresh copy
            c = admin_conn(e, copy)
            c.execute("SELECT count(*) FROM orders").fetchone()
            c.close()
            adm.execute(f'DROP DATABASE "{copy}" WITH (FORCE)')
            adm.close()
        except Exception as ex:
            print(f"  run {i} FAILED (connection drop): {str(ex)[:90]}; "
                  f"cleaning any partial copy and continuing …", flush=True)
            try:
                a2 = admin_conn(e)
                a2.execute(f'DROP DATABASE IF EXISTS "{copy}" WITH (FORCE)')
                a2.close()
            except Exception:
                pass
            continue
        tag = "(cold)" if i == 0 else "      "
        print(f"  run {i} {tag} create_template_copy={dt:8.3f}s", flush=True)
        if i == 0:
            cold = dt
        else:
            times.append(dt)
        flush_result(times, cold)          # persist after every completed copy

    result = flush_result(times, cold)
    print(f"\nwrote {out}")
    med = result.get("copy_provision_median_s")
    print(f">> {scale}: copy(TEMPLATE) median={med if med is None else f'{med:.1f}'}s "
          f"cold={cold if cold is None else f'{cold:.1f}'}s  parent={parent_bytes/1e9:.0f}GB", flush=True)
    print(f"NOTE: parent db {args.parent_db} left in place; drop it or terraform destroy to reclaim.",
          flush=True)


if __name__ == "__main__":
    main()
