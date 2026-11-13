#!/usr/bin/env python3
"""Robust fresh-fixture measurement at 256 GB (the one point the sweep dropped).

rds_all_strategies used 20M-row chunks, leaving the client socket idle for minutes
during the ~40-min 256 GB generate — long enough for a network idle-timeout to drop
it. Here we use small 200k-row chunks so the socket has request/response traffic
every couple of seconds (keepalives are already on in _pg.connect), and time the
same fresh-fixture cycle: DROP + CREATE DATABASE + regenerate + index. Writes the
result into results/sweep_rds_all.json under "256".fresh_fixture_s.
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from branchbench.drivers import _pg  # noqa: E402
from branchbench.datagen_sql import generate_server_side, db_size_bytes  # noqa: E402

GB = 256
E = yaml.safe_load(open(ROOT / "config" / "cloud.yaml"))["engines"]["rds_postgres"]
PARENT = f"bench_all_{GB}g"
insert_target = int(GB * 1e9 * 0.82)


def adm(db="postgres"):
    return _pg.connect(E["host"], E.get("port", 5432), E["user"], E["password"], db,
                       sslmode=E.get("sslmode", "require"), retries=10, retry_delay=3.0)


def main():
    a = adm()
    if a.execute("SELECT 1 FROM pg_database WHERE datname=%s", (PARENT,)).fetchone():
        print(f"dropping stale {PARENT} …", flush=True)
        a.execute(f'DROP DATABASE "{PARENT}" WITH (FORCE)')
    t0 = time.perf_counter()
    a.execute(f'CREATE DATABASE "{PARENT}"')
    a.close()
    print(f"[256GB] fresh-fixture: generating (small 200k chunks) → {insert_target/1e9:.0f} GB target", flush=True)
    pc = adm(PARENT)
    generate_server_side(pc, insert_target, chunk_orders=200_000)
    elapsed = time.perf_counter() - t0
    size = db_size_bytes(pc)
    pc.close()
    print(f"[256GB] fresh_fixture={elapsed:.1f}s  final size {size/1e9:.1f} GB", flush=True)

    out = ROOT / "results" / "sweep_rds_all.json"
    acc = json.loads(out.read_text()) if out.exists() else {}
    rec = acc.get("256", {})
    rec["fresh_fixture_s"] = elapsed
    rec["parent_bytes"] = size
    acc["256"] = rec
    out.write_text(json.dumps(acc, indent=2))
    print(f"wrote {out}  (256 fresh_fixture_s={elapsed:.1f})", flush=True)
    print("FRESH256_DONE")


if __name__ == "__main__":
    main()
