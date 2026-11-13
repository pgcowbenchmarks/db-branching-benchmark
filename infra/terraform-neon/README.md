# Neon leg (Terraform) — copy-on-write branching, anonymously reproducible

This creates the one Neon project the benchmark branches per test cycle. It's a
**separate** Terraform config from `../terraform` (the AWS fleet) on purpose: only
people who want the serverless CoW datapoint need the Neon provider, and AWS-only
users are unaffected.

Neon is the datapoint **anyone can reproduce without a cloud account** — a free Neon
account gives you real Postgres copy-on-write branching.

## Setup

```bash
# 1. Free account -> https://console.neon.tech  ->  Account settings -> API keys
export NEON_API_KEY=napi_...

# 2. provision the project
cd infra/terraform-neon
terraform init
terraform apply          # creates project "db-branching-benchmark"

# 3. wire it into the harness overlay (adds engines.neon to config/cloud.yaml)
cd ../..
python scripts/gen_config.py --neon-dir infra/terraform-neon
#   (or with the AWS fleet too: python scripts/gen_config.py --neon-dir infra/terraform-neon)

# 4. run the CoW branch leg
PYTHONPATH=src python -m branchbench.runner \
    --scales mb,gb --rds config/cloud.yaml --engines neon --runs 20
```

## Teardown

```bash
cd infra/terraform-neon
terraform destroy        # deletes the project and every branch under it
```

Deleting the project cascades to all per-cycle branches, so copy-on-write cleanup is
automatic — nothing lingers or bills.

## What Terraform manages

| Resource | Purpose |
|---|---|
| `neon_project.bench` | The project the harness branches from. Its default branch holds the seeded parent per scale (`parent-mb`, `parent-gb`). |

Per-cycle CoW branches (`cyc-*`) are created and deleted by the harness itself via the
Neon API (using `NEON_API_KEY`), not by Terraform — that create/delete latency **is**
the measurement.

## Notes

- **Region**: defaults to `aws-us-west-2` so latency is comparable to the AWS fleet.
- **Postgres version**: defaults to 17; set `-var pg_version=18` where available.
- **Pooler**: the harness connects through Neon's built-in PgBouncer pooler (`-pooler`
  host) so connections are recycled instead of cold-starting a backend per connect
  (see `engines.neon.use_pooler`).
- The **API key is never written to a file** — the provider reads `NEON_API_KEY` from
  the environment (leave `var.neon_api_key` null).
