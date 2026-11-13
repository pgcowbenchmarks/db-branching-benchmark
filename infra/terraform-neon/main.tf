# Neon copy-on-write branching leg — self-contained Terraform for anonymous reproducers.
#
# This is intentionally a SEPARATE Terraform config from ../terraform (the AWS
# fleet): the Neon provider is only downloaded/needed by people who want the
# serverless CoW datapoint, and AWS-only users are unaffected. It creates one
# Neon project (free tier is enough for the `mb`/`gb` scales) that the harness
# then branches per test cycle.
#
#   1. Make a free Neon account -> https://console.neon.tech
#   2. Account settings -> API keys -> create one
#   3. export NEON_API_KEY=napi_...
#   4. terraform init && terraform apply
#   5. python ../../scripts/gen_config.py --neon-dir .   # adds neon to config/cloud.yaml
#   ... run the benchmark ...
#   6. terraform destroy        # deletes the project + every branch (CoW cleanup is automatic)

terraform {
  required_version = ">= 1.5"
  required_providers {
    neon = {
      source  = "kislerdm/neon"
      version = "~> 0.15"
    }
  }
}

# api_key is read from the NEON_API_KEY environment variable when var.neon_api_key
# is left null (the default), so no secret ever lands in a committed file.
provider "neon" {
  api_key = var.neon_api_key
}

resource "neon_project" "bench" {
  name       = var.project_name
  region_id  = var.region_id
  pg_version = var.pg_version

  # Keep the parent endpoint responsive for seeding; per-cycle branch endpoints
  # are created by the harness. history_retention low = less storage overhead.
  history_retention_seconds = var.history_retention_seconds
}
