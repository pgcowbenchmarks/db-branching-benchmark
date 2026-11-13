# Methodology notes

See the README for the results and the reproduce commands. This file records the
measurement definitions and controls behind the paper's Table II and Fig. 5.

## Definitions
- **Provisioning time**: wall-clock, "no environment" → "ready to run the first query".
  This is the only metric in Table II.
- **Time to failure**: for an attempt that did not finish, the wall-clock spent before it
  failed. Reported separately and never as a provisioning time (the 32 GB full restore).
- **Steady-state read latency**: representative reads after provisioning; the
  first-touch-vs-warm delta on the *same* connection exposes copy-on-write read
  amplification (client round-trip cancels as common-mode).

## Controls
- Every parent is generated deterministically inside the engine with the same production
  shape (power-law key skew, realistic nulls, duplicate keys), so only the provisioning
  mechanism varies along the data axis. Sizes are nominal; the actual parent sizes are in
  the result files (`neon_flatness_parent_sizes.json`, `parent_bytes` fields).
- Engines and instance sizes differ between rows (RDS PostgreSQL db.m5.2xlarge, Aurora
  PostgreSQL db.r6g.large, Neon serverless), so Table II compares mechanisms along a shared
  data axis, not engines on equal hardware.
- Runs per cell: Neon branch 12 per size up to 64 GB (one session, parent grown in place),
  5 at 128 GB and 3 at 256 GB (separate sessions); Aurora clone 2-3; copy-based branch 3
  (2 plus a discarded cold run at 256 GB); rollback the median of 10 cycles; fresh fixture
  and full restore one timed cycle per size (a cycle takes up to two hours at 256 GB).
  Cells report the median and, where runs were recorded, the min-max range.
- A failed attempt is a failure, not a measurement: a restore whose worker dies or whose
  link drops is recorded as failed with its time to failure.
- Local Postgres strategies run with consistent durability tuning
  (`fsync=off`/`synchronous_commit=off`/`full_page_writes=off`) so the measurement
  reflects the provisioning mechanism, not fsync behaviour.

## Engine-specific notes
- **Neon (serverless copy-on-write branch):** Neon serverless Postgres — the technology
  Databricks Lakebase is built on. Branch creation shares the parent's pages; per-cycle
  branches are cut from a permanent parent branch. The Neon API's `logical_size` lags
  writes, so parent sizes are taken from `pg_database_size`.
- **Aurora fast clone (storage copy-on-write):** `restore-to-point-in-time` with
  `RestoreType=copy-on-write` shares the source cluster's storage volume, but the clone's
  compute is a full Aurora instance that must reach `available`, so its wall-clock is
  boot-bound (~350-460 s) while staying flat in data size.
- **Copy-based branch:** `CREATE DATABASE … TEMPLATE` — a full physical copy that is
  branch-shaped but not copy-on-write; its cost tracks the data.
- **Shared fixture (transactional rollback):** a no-isolation baseline — flat only because
  every cycle shares one database, so work that commits escapes the reset.
- **SQLite in-memory (floor):** fast-but-fake lower bound; no production-shaped fidelity,
  and it runs out of memory from 8 GB — that failure is a recorded result.
- **Read amplification and storage at creation** were measured on a Databricks Lakebase
  branch (labeled `"engine": "lakebase"` in the result files); the Neon runs captured
  neither.
