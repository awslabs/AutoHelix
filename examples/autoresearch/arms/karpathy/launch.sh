#!/bin/bash
# The Karpathy arm: upstream's program.md (adapted to run_experiment) and its kickoff, one
# headless Claude Code session in projects/ar-NAME, in the run's container (../container.sh).
#
#   bash arms/karpathy/launch.sh NAME GPUS   # e.g. karpathy 2,3; karpathy-2 2,3
#
# AR_MAX_EXPERIMENTS (60), AR_MAX_BUDGET_USD (150), AR_WALL (24h) as in container.sh.
set -euo pipefail
ARMS=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
name=$1 gpus=$2
project=$(dirname "$ARMS")/projects/ar-$name
# program.md goes into the project untracked, as upstream's sits in its checkout
sed -e "s|RUN_EXPERIMENT|run_experiment|g" -e "s|MAX_EXPERIMENTS|${AR_MAX_EXPERIMENTS:-60}|g" \
    "$ARMS/karpathy/program.md" > "$project/program.md"
exec bash "$ARMS/container.sh" "$name" "$gpus" session \
    "Hi have a look at program.md and let's kick off a new experiment! let's do the setup first."
