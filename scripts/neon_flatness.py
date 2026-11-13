#!/usr/bin/env python3
"""Neon copy-on-write branch-create FLATNESS sweep — one parent, one session.

Why this script exists (methodology fix): the earlier per-size sweep created a
*fresh* parent for each size, seeded it, measured, and deleted it — so every
size was measured in a *different* session with its own network-RTT baseline,
and the 128 GB point came from a separate script entirely. For a sub-second
metadata operation whose true run-to-run spread (~0.4-0.57 s) is dominated by
client<->API round-trip latency, cross-session baselines make the flat line look
like it wiggles ("up at 64 GB, down at 128 GB") when the underlying operation is
size-independent.

This sweep removes that confound:
  * ONE parent branch, GROWN in place through ascending sizes (generate_server_side
    resumes from max(order_id) — no reseed), so the same parent/endpoint/session
    baseline is shared across every size.
  * N trials PER size (default 12), reported as mean / median / stddev / min / max,
    so the figure can show error bars and the reader can see the variation is noise,
    not a size trend.
  * Pure branch-create only (create child branch + delete it); we do NOT connect to
    each transient child (that would fold in serverless cold-start, a separate
    compute artifact already measured elsewhere).

    export NEON_API_KEY=... NEON_PROJECT_ID=...
    python scripts/neon_flatness.py --sizes 0.02,1,2,8,32,64,128 --runs 12

Writes results/neon_flatness.json (accumulates per size, incremental).
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

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from branchbench.drivers import _pg  # noqa: E402
from branchbench.datagen_sql import generate_server_side, db_size_bytes  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
API = "https://console.neon.tech/api/v2"


def chunk_for(insert_target_bytes: int) -> int:
    # VERY small chunks (~150k orders, commit in ~3-5s). The laptop<->Neon link stays
    # up when idle but drops DURING a heavy INSERT, so a chunk must commit inside the
    # few-second window between drops. 1.5M-order chunks (~25s) were long enough to be
    # hit every time and the seed stalled; ~150k commits fast enough to punch through.
    return max(50_000, 150_000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="0.02,1,2,8,32,64,128")
    ap.add_argument("--runs", type=int, default=12)
    ap.add_argument("--parent-name", default="flatness-parent")
    args = ap.parse_args()
    sizes = [float(x) for x in args.sizes.split(",")]

    key, pid = os.environ["NEON_API_KEY"], os.environ["NEON_PROJECT_ID"]
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def req(m, p, **kw):
        # Neon rate-limits concurrent branch operations (423) and occasionally 429/5xx;
        # back off and retry so a busy scheduler never loses a datapoint.
        delay = 3.0
        for attempt in range(12):
            r = requests.request(m, f"{API}{p}", headers=h, timeout=300, **kw)
            if r.status_code in (423, 429) or r.status_code >= 500:
                time.sleep(delay); delay = min(delay * 1.6, 30.0); continue
            if r.status_code >= 400:
                raise RuntimeError(f"Neon {m} {p} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.text else {}
        raise RuntimeError(f"Neon {m} {p}: still throttled after retries")

    def wait_ops(timeout=180):
        """Block until the project has no running/scheduling operations, so the next
        timed branch-create runs against a quiescent scheduler (clean measurement)."""
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            ops = req("GET", f"/projects/{pid}/operations").get("operations", [])
            if not any(o["status"] in ("running", "scheduling") for o in ops[:20]):
                return
            time.sleep(1.5)

    def connect(uri):
        u = urllib.parse.urlparse(uri)
        return _pg.connect(u.hostname, u.port or 5432,
                           urllib.parse.unquote(u.username or ""),
                           urllib.parse.unquote(u.password or ""),
                           u.path.lstrip("/") or "neondb", sslmode="require",
                           retries=20, retry_delay=2.0)

    default = next(b for b in req("GET", f"/projects/{pid}/branches")["branches"] if b.get("default"))

    # ---- one permanent parent branch, grown in place through the sizes ----
    existing = {b["name"]: b for b in req("GET", f"/projects/{pid}/branches")["branches"]}
    if args.parent_name in existing:
        parent = existing[args.parent_name]
        pbid = parent["id"]
        # need a RW endpoint + connection uri; recover or create
        eps = req("GET", f"/projects/{pid}/branches/{pbid}/endpoints").get("endpoints", [])
        if not eps:
            req("POST", f"/projects/{pid}/endpoints",
                json={"endpoint": {"branch_id": pbid, "type": "read_write"}})
        role = parent.get("role") or "neondb_owner"
        # build a connection uri via the role password reveal
        rp = req("GET", f"/projects/{pid}/branches/{pbid}/roles/neondb_owner/reveal_password")
        ep = req("GET", f"/projects/{pid}/branches/{pbid}/endpoints")["endpoints"][0]
        puri = (f"postgresql://neondb_owner:{rp['password']}@{ep['host']}/neondb"
                f"?sslmode=require")
    else:
        d = req("POST", f"/projects/{pid}/branches",
                json={"branch": {"parent_id": default["id"], "name": args.parent_name},
                      "endpoints": [{"type": "read_write"}]})
        pbid, puri = d["branch"]["id"], d["connection_uris"][0]["connection_uri"]
    print(f"parent branch {args.parent_name} id={pbid}", flush=True)

    out = ROOT / "results" / "neon_flatness.json"
    acc = json.loads(out.read_text()) if out.exists() else {}
    acc["_meta"] = {"engine": "neon", "op": "copy-on-write branch-create (API, single session)",
                    "note": "one parent grown in place; N trials per size; same session baseline"}

    for gb in sizes:
        insert_target = int(gb * 1e9 * 0.82)
        # grow the shared parent to this size (resume-safe)
        print(f"[neon {gb}GB] growing parent (resume-safe) …", flush=True)
        while True:
            conn = connect(puri)
            try:
                if db_size_bytes(conn) >= insert_target:
                    conn.close(); break
                generate_server_side(conn, insert_target, chunk_orders=chunk_for(insert_target))
                conn.close(); break
            except Exception as e:
                print(f"  seed dropped ({str(e)[:70]}); reconnecting …", flush=True)
                try: conn.close()
                except Exception: pass
                time.sleep(5)
        parent_bytes = req("GET", f"/projects/{pid}/branches/{pbid}")["branch"].get("logical_size")

        # ---- N branch-create trials (pure metadata op) + cold, same session ----
        times = []
        for i in range(args.runs + 1):
            name = f"cyc-{uuid.uuid4().hex[:8]}"
            wait_ops()                       # quiescent scheduler → clean measurement
            t0 = time.perf_counter()
            cb = req("POST", f"/projects/{pid}/branches",
                     json={"branch": {"parent_id": pbid, "name": name}})  # no endpoint: pure CoW create
            dt = time.perf_counter() - t0
            req("DELETE", f"/projects/{pid}/branches/{cb['branch']['id']}")
            if i > 0:
                times.append(dt)
            if i == 0 or i == args.runs:
                print(f"[neon {gb}GB] run {i}{' (cold)' if i==0 else ''}: {dt:.3f}s", flush=True)

        acc[str(gb)] = {
            "parent_bytes": parent_bytes,
            "n": len(times),
            "branch_create_mean_s": statistics.mean(times),
            "branch_create_median_s": statistics.median(times),
            "branch_create_stdev_s": statistics.pstdev(times) if len(times) > 1 else 0.0,
            "branch_create_min_s": min(times),
            "branch_create_max_s": max(times),
            "runs_s": times,
        }
        out.write_text(json.dumps(acc, indent=2))
        s = acc[str(gb)]
        print(f"[neon {gb}GB] >> mean {s['branch_create_mean_s']:.3f}s "
              f"median {s['branch_create_median_s']:.3f}s "
              f"±{s['branch_create_stdev_s']:.3f} (parent {(parent_bytes or 0)/1e9:.2f}GB, n={s['n']})",
              flush=True)

    # reclaim storage
    try:
        req("DELETE", f"/projects/{pid}/branches/{pbid}")
        print(f"deleted parent {args.parent_name}", flush=True)
    except Exception as e:
        print(f"(parent delete warn: {str(e)[:80]})", flush=True)
    print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    main()
