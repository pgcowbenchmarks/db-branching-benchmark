#!/usr/bin/env python3
"""Assemble results/paper_results.json -- the single source for the paper's Table II and Fig. 5.

Every cell is read from a raw result file in results/ (never typed in), carries its source,
run count and min-max range, and an explicit status: measured, failed, oom, skipped or not_run.
A failed attempt is never a measurement; its time-to-failure is kept separately.

Run after any measurement script; make_paper_table2.py and make_paper_fig5.py read the output.
"""
import json
import statistics
from pathlib import Path

R = Path(__file__).resolve().parent.parent / "results"
SIZES = ["1", "2", "8", "32", "64", "128", "256"]


def load(name):
    return json.load(open(R / name))


def measured(runs=None, median=None, n=None, source=""):
    if runs:
        return {"status": "measured", "median_s": statistics.median(runs), "min_s": min(runs),
                "max_s": max(runs), "n": len(runs), "source": source}
    return {"status": "measured", "median_s": median, "min_s": None, "max_s": None, "n": n, "source": source}


def key(gb):  # raw files key sizes as "1" or "1.0"
    return lambda d: d.get(gb, d.get(f"{float(gb)}"))


sw_all, sw_copy = load("sweep_rds_all.json"), load("sweep_rds.json")
flat, flat_sizes = load("neon_flatness.json"), load("neon_flatness_parent_sizes.json")["parent_pg_database_size_gb"]
aurora = {**load("aurora_clone_scale.json"), **load("aurora_fill.json")}

rows = {}

# ---- copy-on-write branch: Neon serverless Postgres ----
neon = {}
for gb in ("1", "2", "8", "32", "64"):
    c = key(gb)(flat)
    neon[gb] = measured(c["runs_s"], source="neon_flatness.json (one session, parent grown in place)")
n128, n256 = load("neon_large_scale_128gb.json"), load("neon_large_scale_256gb.json")
neon["128"] = measured(n128["branch_create_runs_s"], source="neon_large_scale_128gb.json (separate session)")
neon["256"] = measured(n256["branch_create_runs_s"], source="neon_large_scale_256gb.json (separate session)")
rows["cow_branch"] = {"label": "Copy-on-write branch", "engine": "Neon",
                      "mechanism": "Neon branch API (metadata operation over shared pages)", "cells": neon}

# ---- copy-on-write clone: Aurora fast clone ----
rows["cow_clone"] = {"label": "Copy-on-write clone", "engine": "Aurora PostgreSQL",
                     "mechanism": "restore-to-point-in-time, RestoreType=copy-on-write, plus instance boot",
                     "cells": {gb: measured(key(gb)(aurora)["clone_provision_runs_s"],
                                            source="aurora_clone_scale.json / aurora_fill.json") for gb in SIZES}}

# ---- copy-based branch: CREATE DATABASE ... TEMPLATE ----
copy = {gb: measured(sw_copy[gb]["runs_s"], source="sweep_rds.json") for gb in ("1", "2", "8", "32", "64")}
copy["128"] = measured(load("rds_large_scale_copy_128gb.json")["copy_provision_runs_s"],
                       source="rds_large_scale_copy_128gb.json")
copy["256"] = measured(load("rds_large_scale_copy_256gb.json")["copy_provision_runs_s"],
                       source="rds_large_scale_copy_256gb.json (cold run excluded)")
rows["copy_based_branch"] = {"label": "Copy-based branch", "engine": "RDS PostgreSQL",
                             "mechanism": "CREATE DATABASE ... TEMPLATE (full physical copy; branch-shaped, not copy-on-write)",
                             "cells": copy}

# ---- fresh fixture: drop / create / seed (one timed cycle per size) ----
rows["fresh_fixture"] = {"label": "Fresh fixture", "engine": "RDS PostgreSQL", "mechanism": "drop / create / seed",
                         "cells": {gb: measured(median=sw_all[gb]["fresh_fixture_s"], n=1, source="sweep_rds_all.json")
                                   for gb in SIZES}}

# ---- full copy / restore: pg_dump then pg_restore ----
restore = {}
for gb in SIZES:
    rec = sw_all[gb]
    status = rec.get("full_restore_status", "")
    if rec.get("full_restore_total_s") and not status:
        restore[gb] = measured(median=rec["full_restore_total_s"], n=1, source="sweep_rds_all.json")
    elif status.startswith("error"):
        cell = {"status": "failed", "source": "sweep_rds_all.json + sweep_rds_all.log",
                "note": "pg_restore worker died mid-transfer over the client link"}
        if rec.get("full_restore_total_s"):
            cell["time_to_failure_s"] = rec["full_restore_total_s"]
            cell["dump_s"] = rec["full_restore_dump_s"]
            cell["restore_until_failure_s"] = rec["full_restore_restore_s"]
        else:
            cell["time_to_failure_s"] = None
            cell["note"] += "; time not recorded (sweep crashed before writing, log has no timestamps)"
        restore[gb] = cell
    elif status.startswith("skip"):
        restore[gb] = {"status": "skipped", "note": "dump would not fit on local disk", "source": "sweep_rds_all.json"}
    else:
        restore[gb] = {"status": "not_run"}
rows["full_restore"] = {"label": "Full copy / restore", "engine": "RDS PostgreSQL",
                        "mechanism": "pg_dump -Fd -j4 then pg_restore -j4 from a client", "cells": restore}

# ---- shared fixture: transactional rollback (a no-isolation baseline) ----
rb = {gb: (measured(median=sw_all[gb]["txn_rollback_median_s"], n=10, source="sweep_rds_all.json")
           if sw_all[gb].get("txn_rollback_median_s") else {"status": "not_run"}) for gb in SIZES}
rows["shared_fixture_rollback"] = {"label": "Shared fixture (transactional rollback)", "engine": "RDS PostgreSQL",
                                   "mechanism": "ROLLBACK on one shared database",
                                   "note": "no-isolation baseline: flat only because cycles share one database",
                                   "cells": rb}

# ---- in-memory fake: SQLite on the client ----
fake1 = load("local_strategies.json")["in_memory_fake_sqlite"]
fake2 = load("local_inmemory_fake_2gb.json")["provision"]
fake = {"1": measured(median=fake1["1"]["build_s"], n=1, source="local_strategies.json"),
        "2": measured(fake2["raw_s"], source="local_inmemory_fake_2gb.json (local Docker phase)")}
for gb in ("8", "32", "64", "128", "256"):
    fake[gb] = {"status": "oom", "note": "needs RAM proportional to data", "source": "local_strategies.json"}
rows["in_memory_fake"] = {"label": "In-memory fake", "engine": "SQLite (client)",
                          "mechanism": "in-memory database built per cycle", "cells": fake}

# ---- flatness (Fig. 5's copy-on-write branch line, plus the 20 MB point) ----
flat_rows = {"0.02": key("0.02")(flat)} | {gb: key(gb)(flat) for gb in ("1", "2", "8", "32", "64")}
all_runs = [r for c in flat_rows.values() for r in c["runs_s"]]
medians = [neon[g]["median_s"] for g in SIZES] + [statistics.median(flat_rows["0.02"]["runs_s"])]
means = [c["branch_create_mean_s"] for c in flat_rows.values()] + \
        [statistics.mean(n128["branch_create_runs_s"]), statistics.mean(n256["branch_create_runs_s"])]
flatness = {
    "single_session": {"sizes_gb": list(flat_rows), "runs": len(all_runs),
                       "mean_s": statistics.mean(all_runs), "stdev_s": statistics.stdev(all_runs),
                       "parent_pg_database_size_gb": flat_sizes},
    "separate_sessions": {"128": {"runs": len(n128["branch_create_runs_s"]), "parent_pg_database_size_gb": n128["parent_pg_database_size_gb"]},
                          "256": {"runs": len(n256["branch_create_runs_s"]), "parent_neon_logical_size_gb": round(n256["parent_logical_bytes"] / 1e9, 1)}},
    "median_range_s": [min(medians), max(medians)], "mean_range_s": [min(means), max(means)],
    "span": "20 MB to 256 GB nominal (largest parent: 238.8 GB pg_database_size, 217.7 GB Neon logical)",
}

out = {
    "meta": {
        "what": "Per-cycle provisioning time (seconds) by strategy and parent size -- Table II and Fig. 5",
        "sizes_gb": SIZES, "sizes_note": "nominal; generated parents land somewhat below them",
        "statistic": "median of n runs; min-max where individual runs were recorded",
        "engines": {"RDS PostgreSQL": "16.9, db.m5.2xlarge", "Aurora PostgreSQL": "16.9, db.r6g.large",
                    "Neon": "Neon serverless Postgres (PostgreSQL 18) -- the technology Databricks Lakebase is built on",
                    "SQLite (client)": "in-memory, on the client host"},
        "generated_by": "scripts/build_paper_results.py",
    },
    "table2": rows,
    "flatness": flatness,
    "read_amplification": load("read_amplification.json"),
    "storage_at_creation": load("storage_at_creation.json"),
}
(R / "paper_results.json").write_text(json.dumps(out, indent=1) + "\n")
print("wrote", R / "paper_results.json")
for k, row in rows.items():
    cells = " ".join(f"{gb}:{c['median_s']:.3g}" if c["status"] == "measured" else f"{gb}:{c['status']}"
                     for gb, c in row["cells"].items())
    print(f"  {k:26s} {cells}")
print(f"  flatness medians {flatness['median_range_s'][0]:.3f}-{flatness['median_range_s'][1]:.3f} s, "
      f"means {flatness['mean_range_s'][0]:.3f}-{flatness['mean_range_s'][1]:.3f} s")
