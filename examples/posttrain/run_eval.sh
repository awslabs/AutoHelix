#!/bin/bash
# Evaluate the agent's model with the official scorer, natively.
#
# Reads the model from the FIXED artifacts path (outside the git repo), so it
# works regardless of cwd / worktree. Falls back to the base model if no model
# has been trained yet (used for the baseline measurement).
set -euo pipefail

TASK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$TASK_DIR/config.sh"

# Keep inspect_ai's run transcripts in artifacts (not scattered in the worktree).
export INSPECT_LOG_DIR="$ARTIFACTS/logs"
mkdir -p "$INSPECT_LOG_DIR"

# Eval settings (override via env for faster local tests).
LIMIT="${EVAL_LIMIT:-150}"
FEWSHOT="${EVAL_FEWSHOT:-0}"   # part of the frozen eval protocol; pin it explicitly
MAX_CONNECTIONS="${MAX_CONNECTIONS:-2}"
MAX_TOKENS="${MAX_TOKENS:-4000}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.9}"

# Use the trained model if present, else the base model (baseline).
# TODO(setup): keep BASE_MODEL in sync with the goal in autohelix.yaml.
BASE_MODEL="${BASE_MODEL:-Qwen/Qwen3-4B-Base}"
if [ -f "$ARTIFACTS/final_model/config.json" ]; then
  MODEL="$ARTIFACTS/final_model"
else
  MODEL="$BASE_MODEL"
fi

"$TASK_DIR/run_native.sh" python evaluate.py \
  --model-path "$MODEL" \
  --limit "$LIMIT" \
  --fewshot "$FEWSHOT" \
  --json-output-file "$ARTIFACTS/eval_metrics.json" \
  --templates-dir templates/ \
  --max-connections "$MAX_CONNECTIONS" \
  --max-tokens "$MAX_TOKENS" \
  --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION"

# Parse the score and emit it for AutoHelix to capture.
"$TASK_DIR/run_native.sh" python3 - "$ARTIFACTS/eval_metrics.json" <<'__PARSE__'
import json, sys
data = json.loads(open(sys.argv[1]).read())
acc = data.get("accuracy")
if acc is None:
    raise SystemExit(f"no accuracy in {data}")
print("metric_source=accuracy")
print(f"##autohelix[score={acc}]")
__PARSE__
