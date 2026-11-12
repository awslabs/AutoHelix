#!/bin/bash
# Gate: did the agent produce a model at the fixed artifacts location?
# (Minimal: just existence + a loadable config.)
set -euo pipefail
TASK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$TASK_DIR/config.sh"
# Checks run in a fresh copy of the candidate code, so a model saved inside the
# copy the agent worked in is gone by now. Refuse the in-repo fallback outright.
case "$(cd "$ARTIFACTS" 2>/dev/null && pwd || echo "$ARTIFACTS")/" in
  "$TASK_DIR"/*) echo "ARTIFACTS ($ARTIFACTS) is inside the project; set it to a fixed path outside it (config.sh)"; exit 1 ;;
esac
test -f "$ARTIFACTS/final_model/config.json" \
  || { echo "no final_model at $ARTIFACTS/final_model"; exit 1; }
echo "final_model present at $ARTIFACTS/final_model"
