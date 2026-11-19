#!/bin/bash
# The /goal arm: `claude -p "/goal <goal.md>"` in projects/ar-goal, one headless session.
# goal.md is template/autohelix.yaml's goal text, its scoring adapted to run_experiment.sh.
# See ../session.sh.
#
#   AR_GPU=4,5 bash arms/goal/launch.sh [NAME]   # NAME: goal (default), goal-2, ...
#
# AR_MAX_EXPERIMENTS (60), AR_MAX_BUDGET_USD (150), AR_WALL (24h) as in session.sh.
set -euo pipefail
ARMS=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
goal=$(sed -e "s|RUN_EXPERIMENT|$ARMS/run_experiment.sh|g" -e "s|MAX_EXPERIMENTS|${AR_MAX_EXPERIMENTS:-60}|g" \
    "$ARMS/goal/goal.md")
exec bash "$ARMS/session.sh" "${1:-goal}" "/goal $goal"
