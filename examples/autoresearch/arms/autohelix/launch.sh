#!/bin/bash
# The AutoHelix arm: `autohelix run` in projects/ar-NAME with its autohelix.yaml, in the
# run's container (../container.sh).
#
#   bash arms/autohelix/launch.sh NAME GPUS [RUN ARGS]   # e.g. autohelix 0,1; autohelix-2 6,7
#
# RUN ARGS default to `-n 60 --time 24h`. The run's state is in projects/ar-NAME/.autohelix/
# (collect.py and rescore.py read it there), its console output in arms/runs/NAME/console.log.
# Stop it politely with `docker exec -u agent -w PROJECT ar-NAME autohelix stop`.
set -euo pipefail
ARMS=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
name=$1 gpus=$2
shift 2
[ $# -gt 0 ] || set -- -n 60 --time 24h
exec bash "$ARMS/container.sh" "$name" "$gpus" autohelix "$@"
