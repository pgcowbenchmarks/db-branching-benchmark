#!/usr/bin/env python3
"""Delete any leftover Aurora clone clusters the benchmark created.

The Aurora CoW-clone strategy tears down each clone after its cycle, but a
crash mid-run can leave `bbench-clone-*` clusters/instances behind. Run this
before `terraform destroy` so nothing lingers and bills.

    python scripts/teardown_cloud.py [--region us-west-2] [--profile <aws profile>]
"""
from __future__ import annotations

import argparse

import boto3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--prefix", default="bbench-clone-")
    args = ap.parse_args()

    rds = boto3.Session(profile_name=args.profile, region_name=args.region).client("rds")

    # Instances first (must be deleted before their cluster).
    for db in rds.describe_db_instances()["DBInstances"]:
        if db["DBInstanceIdentifier"].startswith(args.prefix):
            print(f"deleting instance {db['DBInstanceIdentifier']}")
            try:
                rds.delete_db_instance(DBInstanceIdentifier=db["DBInstanceIdentifier"],
                                       SkipFinalSnapshot=True)
            except Exception as e:
                print(f"  {e}")

    for cl in rds.describe_db_clusters()["DBClusters"]:
        if cl["DBClusterIdentifier"].startswith(args.prefix):
            print(f"deleting cluster {cl['DBClusterIdentifier']}")
            try:
                rds.delete_db_cluster(DBClusterIdentifier=cl["DBClusterIdentifier"],
                                      SkipFinalSnapshot=True)
            except Exception as e:
                print(f"  {e}")

    print("done. (Neon per-cycle branches are cleaned by the harness.)")


if __name__ == "__main__":
    main()
