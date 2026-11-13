# Terraform — benchmark database fleet

Provisions the managed databases the cloud phase measures:

| Resource | Purpose |
|---|---|
| `aws_db_instance.rds_postgres` | RDS PostgreSQL — "copy" datapoint (fresh fixture rises with size) |
| `aws_rds_cluster.aurora_pg` (+ 1 instance) | Aurora PostgreSQL — fast clone = storage-layer CoW |
| `aws_security_group.db` | Ingress on 5432, scoped to **your public IP /32** only |
| `random_password.master` | One master password for both (output, sensitive) |

All instances are `publicly_accessible` so the harness can drive them from your
laptop, but the security group only admits your current IP.

## Use

```bash
cd infra/terraform
terraform init

# Default credential chain (env vars / SSO / instance role):
terraform apply
# ...or pin an AWS profile (e.g. an SSO sandbox profile):
terraform apply -var="aws_profile=<your-sso-profile>"

# Turn the outputs into the harness overlay (writes ../../config/cloud.yaml):
cd ../.. && python scripts/gen_config.py
```

Cost knobs (all in `variables.tf`): `rds_instance_class`, `aurora_instance_class`,
`rds_allocated_storage`, and `enable_rds_postgres` /
`enable_aurora_postgres` to provision only a subset.

## Teardown (do this when done — these bill by the hour)

```bash
# Delete any Aurora clones the benchmark created, then destroy the fleet:
python scripts/teardown_cloud.py         # drops leftover bbench-clone-* clusters
cd infra/terraform && terraform destroy
```

Neon is **not** Terraform-managed by this module (see `infra/terraform-neon`) (they are API-provisioned by the
harness itself and clean up their own per-cycle branches).
