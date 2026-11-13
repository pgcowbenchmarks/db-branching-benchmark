#!/usr/bin/env bash
# Bring up the local Docker runtime (Colima) sized for GB-scale benchmarks.
set -euo pipefail

if ! colima status >/dev/null 2>&1; then
  echo "Starting Colima (6 cpu / 12 GB / 120 GB disk)..."
  colima start --cpu 6 --memory 12 --disk 120
else
  echo "Colima already running."
fi
docker ps >/dev/null && echo "Docker OK."
echo "Parent + ephemeral containers are created on demand by the runner."
