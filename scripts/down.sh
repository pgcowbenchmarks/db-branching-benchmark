#!/usr/bin/env bash
# Remove all benchmark containers (parents + any leftover ephemerals) AND their
# anonymous volumes. The `-v` is REQUIRED: `docker rm -f` without it leaks
# anonymous volumes, which fills the Colima VM disk over repeated runs.
set -uo pipefail
docker ps -aq -f "name=bb-pg"    | xargs -r docker rm -f -v
docker volume prune -f
docker container prune -f
echo "Removed benchmark containers + volumes. (Colima left running; 'colima stop' to halt.)"
