# Database provisioning-cost benchmark

**How much does it cost to hand each automated-test cycle its own fresh, production-shaped
database — and does that cost stay flat as the data grows?**

This repository is the measurement artifact for the economics section (Table II and
Fig. 5) of *Introducing Branched-Database TDD*. It tests one structural prediction:

> **Copy-on-write branch provisioning is flat in data size**, while every
> fidelity-preserving alternative — fresh fixture, full restore, copy-based branch —
> grows with data size.

A copy-on-write branch shares the parent's storage pages and diverges only on write, so
creating one is a metadata operation independent of how much data the parent holds. A
fresh fixture, a restore or a full copy has to move or rebuild the bytes, so its cost
tracks the dataset.

## Results — Table II

Per-cycle provisioning time in seconds (medians) at each nominal parent size, generated
from [`results/paper_results.json`](results/paper_results.json) by
`scripts/make_paper_table2.py`:

| Strategy | Engine | 1 GB | 2 GB | 8 GB | 32 GB | 64 GB | 128 GB | 256 GB |
|---|---|---|---|---|---|---|---|---|
| In-memory fake | SQLite (client) | 27.5 | 28.9 | OOM | OOM | OOM | OOM | OOM |
| Fresh fixture | RDS PostgreSQL | 31.4 | 58.4 | 223 | 897 | 1,726 | 3,196 | 7,265 |
| Shared fixture (transactional rollback)¹ | RDS PostgreSQL | 0.336 | 0.393 | 0.386 | 0.399 | 0.593 | 0.608 | n.r. |
| Full copy / restore² | RDS PostgreSQL | 67.7 | 162 | 473 | fail | fail | skip | n.r. |
| Copy-based branch | RDS PostgreSQL | 8.5 | 17.2 | 70.5 | 320 | 633 | 1,104 | 2,275 |
| Copy-on-write branch | Neon | 0.388 | 0.378 | 0.429 | 0.400 | 0.381 | 0.398 | 0.389 |
| Copy-on-write clone | Aurora PostgreSQL | 454 | 369 | 353 | 452 | 362 | 461 | 401 |

fail = the attempt died before finishing; OOM = out of memory; skip = not attempted because
the dump would not fit on local disk; n.r. = not run.
¹ A no-isolation baseline: flat only because every cycle shares one database, so work that
commits escapes the reset. ² Completed at 1, 2 and 8 GB only. The 32 GB attempt ran 983 s
(a 786 s dump plus 197 s of restore) before its `pg_restore` worker died mid-transfer; the
64 GB attempt failed the same way, with its time not recorded.

- **Copy-on-write branch (Neon):** flat from 20 MB to 256 GB — medians 0.37–0.43 s, means
  0.38–0.42 s. The 20 MB–64 GB points come from one session with the parent grown in place
  (12 runs per size, 72 in all, mean 0.396 ± 0.050 s); 128 GB and 256 GB are separate
  sessions on 121 GB and 239 GB parents. Neon serverless Postgres is the technology
  Databricks Lakebase is built on.
- **Copy-on-write clone (Aurora):** also flat, 353–461 s at every size — its storage is
  shared, but its compute is a full instance that must boot.
- **Every fidelity-preserving copy grows with data:** the copy-based branch from 8.5 s to
  2,275 s (about 9 s per GB), the fresh fixture from 31 s to 7,265 s. At 256 GB the branch is
  about 5,800 times cheaper per cycle than a copy-based branch and 18,700 times cheaper than
  a fresh fixture.
- **Fig. 5** (`scripts/make_paper_fig5.py`) plots these medians; the paper figure omits the
  rollback baseline (`--with-rollback` draws it).
- **Read amplification:** first-touch reads on a fresh branch were 43 ms (13%) slower than
  warm reads on the same connection, and no slower than reads on a fully copied database.
  Measured on a Databricks Lakebase branch
  ([`results/read_amplification.json`](results/read_amplification.json)).
- **Storage at creation:** a new branch occupied 0 bytes at creation (a Lakebase branch of a
  218 GB parent, [`results/storage_at_creation.json`](results/storage_at_creation.json));
  every copy strategy stores a full copy per environment. The Neon runs did not capture
  storage at creation.

## How it is measured

- **Provisioning time** = wall-clock from "no environment" to "ready to run the first query".
- **One production-shaped dataset.** Every parent is generated deterministically inside the
  engine (power-law key skew, realistic nulls, duplicate keys, wide values), so only the
  provisioning mechanism varies along the data axis. Sizes are nominal; generated parents land
  somewhat below them (actual sizes are in the result files).
- **Engines:** RDS PostgreSQL 16.9 (db.m5.2xlarge) for the copies and the rollback baseline,
  Aurora PostgreSQL 16.9 (db.r6g.large) for the clone, Neon serverless Postgres for the branch,
  SQLite in memory on the client. Instance sizes differ between rows, so Table II compares
  mechanisms along a shared data axis, not engines on equal hardware.
- **Runs per cell:** Neon 12 per size up to 64 GB, 5 at 128 GB, 3 at 256 GB; Aurora 2–3;
  copy-based branch 3 (2 plus a discarded cold run at 256 GB); rollback the median of 10
  cycles; fresh fixture and full restore one timed cycle per size, since one cycle takes up to
  two hours at 256 GB. `paper_results.json` carries every cell's run count, min–max range and
  source file.
- **Honesty rules:** a failed attempt is recorded as a failure with its time to failure,
  never as a measurement; nothing is projected or interpolated; a strategy that could not run
  beyond some size stops at the last size it reached.

More detail: [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md).

## Reproduce it

### Path A — local only (free, ~30 min, no cloud account)

Reproduces the *shape* (copies rising with size) on Docker PostgreSQL 16 + SQLite at ~20 MB
and ~2 GB. It is a reproduction path, not a reported result.

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
bash scripts/up.sh                                   # start Colima/Docker
PYTHONPATH=src python -m branchbench.runner --scales mb,gb --engines sqlite,postgres --runs 8
PYTHONPATH=src python -m branchbench.report          # table + figure into results/
bash scripts/down.sh                                 # removes containers and volumes
```

### Path B — the copy-on-write branch on Neon (free account, no AWS)

```bash
export NEON_API_KEY=napi_...          # console.neon.tech -> Account settings -> API keys
cd infra/terraform-neon && terraform init && terraform apply && cd ../..
python scripts/gen_config.py --neon-dir infra/terraform-neon
export NEON_PROJECT_ID=...            # from the Terraform output
python scripts/neon_flatness.py --sizes 0.02,1,2,8,32,64
python scripts/neon_large_scale.py --target-gb 128 --runs 5 --chunk-orders 200000
python scripts/neon_large_scale.py --target-gb 256 --runs 3 --chunk-orders 200000
cd infra/terraform-neon && terraform destroy && cd ../..
```

### Path C — the managed-cloud copies and the Aurora clone (AWS, bills hourly)

```bash
cd infra/terraform && terraform init && terraform apply && cd ../..   # RDS PostgreSQL + Aurora
python scripts/gen_config.py                                          # writes config/cloud.yaml
```

The security group locks ingress to your public IP, and the subnet group uses only your
VPC's internet-routed subnets (set `-var 'public_subnet_ids=[...]'` if auto-detection finds
fewer than two). The 256 GB copy-based and fresh-fixture legs need about twice the data size
in allocated storage (parent plus one copy).

**Table II row → reproduce command** (each writes the file the paper build reads):

| Table II row | Sizes | Command | Writes |
|---|---|---|---|
| Copy-on-write branch (Neon) | 20 MB–64 GB | `python scripts/neon_flatness.py --sizes 0.02,1,2,8,32,64` | `neon_flatness.json` |
| | 128, 256 GB | `python scripts/neon_large_scale.py --target-gb N --chunk-orders 200000` | `neon_large_scale_<N>gb.json` |
| Copy-on-write clone (Aurora) | 2, 8, 32, 256 GB | `python scripts/aurora_large_scale_clone.py --sizes 2,8,32,256 --runs 2` | `aurora_clone_scale.json` |
| | 1, 64, 128 GB | `python scripts/aurora_fill_measure.py seed --size N` then `clone --size N --runs 2` (smallest → largest; set `AURORA_FILL_HOST`, `AURORA_FILL_CLUSTER`) | `aurora_fill.json` |
| Copy-based branch | 1–64 GB | `PYTHONPATH=src python scripts/large_scale_sweep.py --engine rds --sizes 1,2,8,32,64` | `sweep_rds.json` |
| | 128, 256 GB | `PYTHONPATH=src python scripts/rds_large_scale_copy.py --target-gb N --chunk-orders 200000` | `rds_large_scale_copy_<N>gb.json` |
| Fresh fixture, full restore, shared fixture (rollback) | 1–128 GB | `PYTHONPATH=src python scripts/rds_all_strategies.py --sizes 1,2,8,32,64,128` | `sweep_rds_all.json` |
| Fresh fixture | 256 GB | `PYTHONPATH=src python scripts/measure_fresh256.py` | `sweep_rds_all.json` |
| In-memory fake | 2 GB | Path A (`gb` scale); copy the `in_memory_fake` cell from `summary-*.json` into `local_inmemory_fake_2gb.json` | `local_inmemory_fake_2gb.json` |

The 1 GB in-memory-fake point (27.5 s, 8.1 GB peak RAM) was measured ad hoc and recorded
directly in `results/local_strategies.json`; no script in this repository produces it.

> **Seeding note.** Server-side generation runs `INSERT … SELECT generate_series` inside the
> engine. Over a laptop-to-cloud link, a large chunk leaves the client socket idle long enough
> for a network idle-timeout to drop the connection mid-run, so every seeding script commits in
> small 150k–200k-order chunks and resumes from `max(order_id)` after any drop.

**Tear down** (these bill hourly):

```bash
python scripts/teardown_cloud.py                         # stray Aurora clone clusters
cd infra/terraform && terraform destroy && cd ../..
```

### Build the paper outputs

```bash
python scripts/build_paper_results.py   # raw result files -> results/paper_results.json
python scripts/make_paper_table2.py     # -> results/paper_table2.md and .tex
python scripts/make_paper_fig5.py       # -> results/paper_fig5_provisioning.pdf and .png
```

## Layout

```
src/branchbench/          harness: datagen, schema, runner, report
  drivers/                one module per provisioning strategy
    postgres.py sqlite_memory.py   # local Docker + SQLite
    cloud_pg.py                    # managed fresh fixture (RDS PostgreSQL)
    aurora_clone.py                # Aurora fast clone (copy-on-write)
    neon.py                        # Neon copy-on-write branch
config/benchmark.yaml     scales, strategies, engine settings
infra/terraform/          RDS PostgreSQL + Aurora PostgreSQL (IP-scoped security group)
infra/terraform-neon/     Neon project for the copy-on-write branch leg
scripts/                  measurement scripts (table above) and the paper build
results/                  raw result files + paper_results.json, paper_table2.md/.tex
```

## License

Apache-2.0. See [LICENSE](LICENSE).
