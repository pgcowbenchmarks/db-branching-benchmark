#!/usr/bin/env python3
"""RDS PostgreSQL: fresh_fixture, txn_rollback, and full_restore across sizes.

Rounds out the 1→128 GB multi-strategy curve alongside the TEMPLATE copy
(copy-based branch) and Neon CoW branch already measured. Per size:

  * fresh_fixture — DROP + CREATE DATABASE + regenerate data in-engine + index.
                    Cost = generation + index build; rises with size.
  * txn_rollback  — BEGIN; representative write; ROLLBACK on the seeded parent.
                    Shared instance; flat/near-zero, but no isolation, no DDL.
  * full_restore  — pg_dump (custom, parallel) then pg_restore into a fresh DB.
                    The classic backup/restore path; rises with size AND needs a
                    local dump artifact, so it is disk-bounded on the client.

Server-side generation (no uplink); full_restore uses local pg_dump/pg_restore
against the RDS endpoint. DB-password only. Writes results/sweep_rds_all.json.
Each size records failures (e.g. dump won't fit local disk) as results, not crashes.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from branchbench.drivers import _pg  # noqa: E402
from branchbench.datagen_sql import generate_server_side, db_size_bytes  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PG_BIN = "/opt/homebrew/opt/libpq/bin"


def chunk_for(b):
    return max(200_000, min(20_000_000, int(b / 520 / 4)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="1,8,32,64")
    ap.add_argument("--rollback-runs", type=int, default=10)
    ap.add_argument("--restore-max-gb", type=float, default=64,
                    help="skip full_restore above this (local dump disk limit)")
    args = ap.parse_args()
    sizes = [int(x) for x in args.sizes.split(",")]
    e = yaml.safe_load(open(ROOT / "config" / "cloud.yaml"))["engines"]["rds_postgres"]

    def adm(db="postgres"):
        return _pg.connect(e["host"], e.get("port", 5432), e["user"], e["password"],
                           db, sslmode=e.get("sslmode", "require"), retries=10, retry_delay=3.0)

    env = {**os.environ, "PGPASSWORD": e["password"], "PGSSLMODE": "require"}
    dump_dir = Path("/tmp/bb_dumps")
    dump_dir.mkdir(exist_ok=True)
    out = ROOT / "results" / "sweep_rds_all.json"
    acc = json.loads(out.read_text()) if out.exists() else {}

    for gb in sizes:
        rec = acc.get(str(gb), {})
        parent = f"bench_all_{gb}g"
        insert_target = int(gb * 1e9 * 0.82)

        # ---- fresh_fixture: DROP + CREATE + regenerate + index ----
        a = adm()
        if a.execute("SELECT 1 FROM pg_database WHERE datname=%s", (parent,)).fetchone():
            a.execute(f'DROP DATABASE "{parent}" WITH (FORCE)')
        t0 = time.perf_counter()
        a.execute(f'CREATE DATABASE "{parent}"')
        a.close()
        pc = adm(parent)
        generate_server_side(pc, insert_target, chunk_orders=chunk_for(insert_target))
        rec["fresh_fixture_s"] = time.perf_counter() - t0
        rec["parent_bytes"] = db_size_bytes(pc)
        print(f"[{gb}GB] fresh_fixture={rec['fresh_fixture_s']:.1f}s", flush=True)

        # ---- txn_rollback ----
        rb = []
        for _ in range(args.rollback_runs):
            c = _pg.connect(e["host"], e.get("port", 5432), e["user"], e["password"],
                            parent, sslmode=e.get("sslmode", "require"), autocommit=False)
            t = time.perf_counter()
            cur = c.cursor()
            cur.execute("UPDATE orders SET status='refunded' WHERE order_id < 1000")
            cur.execute("INSERT INTO events(event_id,account_id,event_type,dedupe_key,payload,occurred_at) "
                        "SELECT 9000000000+g,1,'t','RB',NULL,now() FROM generate_series(1,500) g")
            c.rollback()
            rb.append(time.perf_counter() - t)
            c.close()
        rec["txn_rollback_median_s"] = statistics.median(rb)
        pc.close()
        print(f"[{gb}GB] txn_rollback_median={statistics.median(rb)*1000:.1f}ms", flush=True)

        # ---- full_restore: pg_dump -> pg_restore (disk-bounded) ----
        if gb <= args.restore_max_gb and shutil.disk_usage("/tmp").free > gb * 1e9 * 1.5:
          phase = "dump"
          try:
            ddir = dump_dir / f"{parent}.dir"  # directory format supports -j (parallel)
            shutil.rmtree(ddir, ignore_errors=True)
            t0 = time.perf_counter()
            try:
                subprocess.run([f"{PG_BIN}/pg_dump", "-h", e["host"], "-U", e["user"],
                                "-d", parent, "-Fd", "-j", "4", "-f", str(ddir)],
                               env=env, check=True)
            except subprocess.CalledProcessError:
                # time spent before a failure is a metric in its own right -- keep it
                rec["full_restore_time_to_failure_s"] = time.perf_counter() - t0
                raise
            dump_s = time.perf_counter() - t0
            phase = "restore"
            restored = f"bb_rst_{uuid.uuid4().hex[:8]}"
            a = adm(); a.execute(f'CREATE DATABASE "{restored}"'); a.close()
            t0 = time.perf_counter()
            r = subprocess.run([f"{PG_BIN}/pg_restore", "-h", e["host"], "-U", e["user"],
                                "-d", restored, "-j", "4", "--no-owner", str(ddir)],
                               env=env, check=False, capture_output=True, text=True)
            restore_s = time.perf_counter() - t0
            sys.stderr.write(r.stderr)
            # Non-zero exit alone is not failure (an older server rejects a newer client's
            # SET transaction_timeout); a dead worker or dropped link is, and must never be
            # recorded as a completed restore.
            fatal = [m for m in ("worker process died", "could not receive data", "SSL SYSCALL")
                     if m in r.stderr]
            if fatal:
                rec["full_restore_dump_s"] = dump_s
                rec["full_restore_restore_until_failure_s"] = restore_s
                rec["full_restore_time_to_failure_s"] = dump_s + restore_s
                raise RuntimeError(f"pg_restore died after {restore_s:.1f}s ({fatal[0]})")
            phase = "cleanup (restore completed)"
            dump_bytes = sum(f.stat().st_size for f in ddir.rglob("*") if f.is_file())
            rec["full_restore_dump_s"] = dump_s
            rec["full_restore_restore_s"] = restore_s
            rec["full_restore_total_s"] = dump_s + restore_s
            rec["dump_bytes"] = dump_bytes
            a = adm(); a.execute(f'DROP DATABASE "{restored}" WITH (FORCE)'); a.close()
            shutil.rmtree(ddir, ignore_errors=True)
            print(f"[{gb}GB] full_restore dump={dump_s:.1f}s restore={restore_s:.1f}s "
                  f"total={dump_s+restore_s:.1f}s", flush=True)
          except Exception as ex:
            rec["full_restore_status"] = f"error during {phase}: {str(ex)[:100]}"
            ttf = rec.get("full_restore_time_to_failure_s")
            print(f"[{gb}GB] full_restore ERROR during {phase}"
                  + (f" after {ttf:.1f}s" if ttf else "") + f": {str(ex)[:100]}", flush=True)
        else:
            rec["full_restore_status"] = "skip (local dump disk limit)"
            print(f"[{gb}GB] full_restore SKIP (disk/limit)", flush=True)

        a = adm(); a.execute(f'DROP DATABASE "{parent}" WITH (FORCE)'); a.close()
        acc[str(gb)] = rec
        out.write_text(json.dumps(acc, indent=2))
    print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    main()
