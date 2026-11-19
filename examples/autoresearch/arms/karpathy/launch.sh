#!/bin/bash
# The Karpathy arm: upstream's program.md (adapted to run_experiment.sh) and its kickoff,
# one headless Claude Code session in projects/ar-karpathy. See ../session.sh.
#
#   AR_GPU=2,3 bash arms/karpathy/launch.sh [NAME]   # NAME: karpathy (default), karpathy-2, ...
#
# AR_MAX_EXPERIMENTS (60), AR_MAX_BUDGET_USD (150), AR_WALL (24h) as in session.sh.
set -euo pipefail
ARMS=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
name=${1:-karpathy}
project=$(dirname "$ARMS")/projects/ar-$name
# program.md goes into the project untracked, as upstream's sits in its checkout
sed -e "s|RUN_EXPERIMENT|$ARMS/run_experiment.sh|g" -e "s|MAX_EXPERIMENTS|${AR_MAX_EXPERIMENTS:-60}|g" \
    "$ARMS/karpathy/program.md" > "$project/program.md"
exec bash "$ARMS/session.sh" "$name" \
    "Hi have a look at program.md and let's kick off a new experiment! let's do the setup first."
