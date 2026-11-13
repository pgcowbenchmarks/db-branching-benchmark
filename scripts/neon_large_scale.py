#!/usr/bin/env python3
"""Neon copy-on-write branch benchmark at LARGE scale (tens–hundreds of GB).

Seeds a per-scale parent branch *server-side* (see branchbench.datagen_sql) so no
data crosses the client uplink, then measures per-cycle CoW branch-create — the
metadata operation whose cost the paper predicts is flat in data size. At 100 GB+
this is the datapoint that matters: branch-create should still be ~sub-second and
storage-at-creation ~0, no matter how large the parent is.

    export NEON_API_KEY=napi_...
    export NEON_PROJECT_ID=...            # or pass --project-id
    python scripts/neon_large_scale.py --target-gb 128 --runs 5

Writes results/neon_large_scale_<N>gb.json. Use --keep to leave the parent branch in
place (it bills storage); by default the parent is deleted after measuring.
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

import psycopg
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from branchbench.datagen_sql import generate_server_side, db_size_bytes  # noqa: E402

API = "https://console.neon.tech/api/v2"


class Neon:
    def __init__(self, key, project_id):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"})
        self.pid = project_id

    def req(self, m, p, **kw):
        r = self.s.request(m, f"{API}{p}", timeout=180, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Neon {m} {p} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.text else {}

    def default_branch(self):
        bs = self.req("GET", f"/projects/{self.pid}/branches")["branches"]
        return next(b for b in bs if b.get("default")), bs

    def branch_named(self, name):
        _, bs = self.default_branch()
        return next((b for b in bs if b.get("name") == name), None)

    def create_branch(self, parent_id, name):
        d = self.req("POST", f"/projects/{self.pid}/branches",
                     json={"branch": {"parent_id": parent_id, "name": name},
                           "endpoints": [{"type": "read_write"}]})
        uris = d.get("connection_uris") or []
        return d["branch"]["id"], (uris[0]["connection_uri"] if uris else None)

    def connection_uri(self, branch_id, role="neondb_owner", db="neondb"):
        return self.req("GET", f"/projects/{self.pid}/connection_uri",
                        params={"branch_id": branch_id, "database_name": db,
                                "role_name": role}).get("uri")

    def branch_logical_size(self, branch_id):
        b = self.req("GET", f"/projects/{self.pid}/branches/{branch_id}")["branch"]
        v = b.get("logical_size")
        return int(v) if v is not None else None

    def delete_branch(self, branch_id):
        try:
            self.req("DELETE", f"/projects/{self.pid}/branches/{branch_id}")
        except Exception as e:
            print("  (branch delete failed:", e, ")")


def connect(uri, direct=True):
    """Connect to a branch. direct=True uses the plain endpoint (needed for long
    DDL/generation sessions); the pooler is transaction-mode and not for that."""
    u = urllib.parse.urlparse(uri)
    host = u.hostname
    if not direct and "-pooler." not in host:
        head, _, tail = host.partition(".")
        host = f"{head}-pooler.{tail}"
    return psycopg.connect(host=host, port=u.port or 5432,
                           user=urllib.parse.unquote(u.username or ""),
                           password=urllib.parse.unquote(u.password or ""),
                           dbname=(u.path.lstrip("/") or "neondb"), sslmode="require",
                           autocommit=True, connect_timeout=40,
                           keepalives=1, keepalives_idle=10, keepalives_interval=5,
                           keepalives_count=3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-gb", type=float, default=128)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--project-id", default=os.environ.get("NEON_PROJECT_ID"))
    ap.add_argument("--chunk-orders", type=int, default=20_000_000)
    ap.add_argument("--keep", action="store_true", help="keep the parent branch (it bills storage)")
    args = ap.parse_args()

    key = os.environ.get("NEON_API_KEY")
    if not key or not args.project_id:
        sys.exit("set NEON_API_KEY and NEON_PROJECT_ID (or --project-id)")
    n = Neon(key, args.project_id)

    # Target the INSERT loop a bit under the requested size; indexes add the rest.
    scale = f"{int(args.target_gb)}gb"
    parent_name = f"parent-{scale}"
    insert_target = int(args.target_gb * 1e9 * 0.82)  # ~18% headroom for indexes

    default, _ = n.default_branch()
    parent = n.branch_named(parent_name)
    if parent:
        pid_branch = parent["id"]
        uri = n.connection_uri(pid_branch)
        print(f"reusing {parent_name} ({pid_branch})", flush=True)
    else:
        pid_branch, uri = n.create_branch(default["id"], parent_name)
        print(f"created {parent_name} ({pid_branch})", flush=True)

    conn = connect(uri, direct=True)
    have = db_size_bytes(conn)
    if have < insert_target:
        print(f"seeding {parent_name} server-side to ~{args.target_gb} GB "
              f"(insert target {insert_target/1e9:.0f} GB) …", flush=True)
        stats = generate_server_side(conn, insert_target, chunk_orders=args.chunk_orders)
    else:
        stats = {"note": "parent already at target", "final_size_bytes": have}
    conn.close()
    parent_logical = n.branch_logical_size(pid_branch)
    print(f"parent logical size: {parent_logical/1e9:.1f} GB", flush=True)

    # ---- measure CoW branch-create (cold + N) ----
    print(f"\nmeasuring branch-create x{args.runs} (+cold) …", flush=True)
    creates, storage0 = [], None
    for i in range(args.runs + 1):
        name = f"cyc-{uuid.uuid4().hex[:8]}"
        t0 = time.perf_counter()
        bid, curi = n.create_branch(pid_branch, name)
        bc = time.perf_counter() - t0
        # first-query readiness + storage-at-creation
        try:
            c = connect(curi or n.connection_uri(bid), direct=False)
            c.execute("SELECT 1").fetchone()
            c.close()
        except Exception as e:
            print("  (connect warn:", str(e)[:80], ")")
        if storage0 is None:
            storage0 = n.branch_logical_size(bid)
        n.delete_branch(bid)
        tag = "(cold)" if i == 0 else "      "
        print(f"  run {i} {tag} branch_create={bc:.3f}s", flush=True)
        if i > 0:
            creates.append(bc)

    result = {
        "scale": scale,
        "target_gb": args.target_gb,
        "parent_logical_bytes": parent_logical,
        "seed_stats": stats,
        "branch_create_median_s": statistics.median(creates),
        "branch_create_min_s": min(creates),
        "branch_create_max_s": max(creates),
        "branch_create_runs_s": creates,
        "storage_at_creation_bytes": storage0,
    }
    out = Path(__file__).resolve().parent.parent / "results" / f"neon_large_scale_{args.target_gb:g}gb.json"
    out.write_text(json.dumps(result, indent=2, default=str))
    print(f"\nwrote {out}")
    print(f">> {scale}: branch_create median={result['branch_create_median_s']:.3f}s "
          f"range={result['branch_create_min_s']:.3f}-{result['branch_create_max_s']:.3f}s  storage@create={storage0} bytes  "
          f"parent={parent_logical/1e9:.0f}GB", flush=True)

    if not args.keep:
        print(f"deleting parent {parent_name} to stop storage billing …", flush=True)
        n.delete_branch(pid_branch)
    else:
        print(f"keeping parent {parent_name} (bills storage until deleted).", flush=True)


if __name__ == "__main__":
    main()
