#!/usr/bin/env python3
"""Turn `terraform output` into the harness overlay config/cloud.yaml.

Run after `terraform apply` (from repo root):
    python scripts/gen_config.py                        # AWS fleet only
    python scripts/gen_config.py --neon-dir infra/terraform-neon   # + Neon leg

Then drive the cloud engines:
    PYTHONPATH=src python -m branchbench.runner --scales gb --rds config/cloud.yaml \
        --engines rds_postgres,aurora_postgres,neon
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TF_DIR = ROOT / "infra" / "terraform"
OUT = ROOT / "config" / "cloud.yaml"


def tf_output(tf_dir: Path) -> dict:
    raw = subprocess.check_output(["terraform", "output", "-json"], cwd=tf_dir)
    return {k: v["value"] for k, v in json.loads(raw).items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--aws-dir", default=str(TF_DIR),
                    help="Terraform dir for the AWS fleet (RDS/Aurora).")
    ap.add_argument("--neon-dir", default="",
                    help="Terraform dir for the Neon leg (infra/terraform-neon). "
                         "Omit to skip Neon; the harness reads NEON_API_KEY from env.")
    args = ap.parse_args()

    engines: dict = {}

    # ---- AWS fleet (RDS PostgreSQL, Aurora PostgreSQL) ----
    o = tf_output(Path(args.aws_dir))
    user, pw, region = o["master_username"], o["master_password"], o["region"]
    if o.get("rds_postgres_host"):
        engines["rds_postgres"] = {
            "host": o["rds_postgres_host"], "port": 5432, "user": user,
            "password": pw, "admin_db": "postgres", "sslmode": "require",
            "version_note": f"RDS PostgreSQL ({region})"}
    if o.get("aurora_postgres_host"):
        engines["aurora_postgres"] = {
            "host": o["aurora_postgres_host"], "port": 5432, "user": user,
            "password": pw, "admin_db": "postgres", "sslmode": "require",
            "parent_db": "branchbench_parent",
            "aws": {"region": region,
                    "source_cluster_id": o["aurora_postgres_cluster_id"],
                    "instance_class": o.get("aurora_instance_class", "db.r6g.large")},
            "version_note": f"Aurora PostgreSQL fast-clone ({region})"}

    # ---- Neon leg (optional; created by infra/terraform-neon) ----
    if args.neon_dir:
        n = tf_output(Path(args.neon_dir))
        engines["neon"] = {
            "api_key_env": "NEON_API_KEY",          # secret stays in the environment
            "project_id": n["project_id"],
            "region_id": n.get("region_id", "aws-us-west-2"),
            "pg_version": int(n.get("pg_version", 17)),
            "role": "neondb_owner",
            "use_pooler": True,                      # PgBouncer pooler endpoint
            "version_note": f"Neon serverless Postgres {n.get('pg_version', '')} "
                            f"(CoW branching, PgBouncer pooler)"}

    OUT.write_text(yaml.safe_dump({"engines": engines}, sort_keys=False))
    print(f"Wrote {OUT} with engines: {', '.join(engines)}")
    print("NOTE: config/cloud.yaml contains the DB password and is .gitignored.")


if __name__ == "__main__":
    main()
