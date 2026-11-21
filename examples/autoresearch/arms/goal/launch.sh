#!/bin/bash
# The /goal arm: `claude -p "/goal <goal.md>"` in projects/ar-NAME, one headless session in
# the run's container (../container.sh). goal.md is template/autohelix.yaml's goal text,
# its scoring adapted to run_experiment.
#
#   bash arms/goal/launch.sh NAME GPUS   # e.g. goal 4,5; goal-2 4,5
#
# AR_MAX_EXPERIMENTS (60), AR_MAX_BUDGET_USD (150), AR_WALL (24h) as in container.sh.
set -euo pipefail
ARMS=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
name=$1 gpus=$2
goal=$(sed -e "s|RUN_EXPERIMENT|run_experiment|g" -e "s|MAX_EXPERIMENTS|${AR_MAX_EXPERIMENTS:-60}|g" \
    "$ARMS/goal/goal.md")
exec bash "$ARMS/container.sh" "$name" "$gpus" session "/goal $goal"
