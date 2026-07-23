#!/bin/bash
# Constraint: did the agent produce a model at the fixed artifacts location?
# (Minimal: just existence + a loadable config.)
set -euo pipefail
TASK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$TASK_DIR/config.sh"
test -f "$ARTIFACTS/final_model/config.json" \
  || { echo "no final_model at $ARTIFACTS/final_model"; exit 1; }
echo "final_model present at $ARTIFACTS/final_model"
