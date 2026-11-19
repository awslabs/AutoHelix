#!/bin/bash
# A quick check for your session: does train.py run in the sandbox and export a model the
# scorer accepts?
#
#   bash ar/smoke.sh
#
# Runs train.py as ar/train_and_score.sh does (same sandbox, AR_GPU's first GPU), on a copy
# whose prepare.py has TIME_BUDGET = SMOKE_BUDGET seconds, killed at SMOKE_LIMIT seconds.
# Then checks the export with the scorer's validation. It scores nothing: it tells you
# that train.py runs and exports, not how good the model is. About 70 s on an H200.
#
# A train.py that doesn't stop on prepare.TIME_BUDGET runs into SMOKE_LIMIT here.
set -uo pipefail
SMOKE_BUDGET=30
SMOKE_LIMIT=180

cd "$(dirname "${BASH_SOURCE[0]}")/.."
. ar/paths.sh

if [ -z "${AR_GPU:-}" ]; then
    echo "smoke: set AR_GPU to this run's GPU indices (as nvidia-smi numbers them)"
    exit 2
fi
export AR_GPU=${AR_GPU%%,*}  # the first
scratch=$(mktemp -d "${TMPDIR:-/tmp}/ar-smoke.XXXXXX")
trap 'rm -rf "$scratch"' EXIT
work=$scratch/work
mkdir -p "$work/ar"
cp train.py prepare.py "$work/"
cp ar/__init__.py ar/export.py ar/score.py "$work/ar/"
sed -i -E "s/^TIME_BUDGET = [0-9]+/TIME_BUDGET = $SMOKE_BUDGET/" "$work/prepare.py"
grep -q "^TIME_BUDGET = $SMOKE_BUDGET\b" "$work/prepare.py" || { echo "smoke: can't set TIME_BUDGET in prepare.py"; exit 2; }

echo "smoke: training for ${SMOKE_BUDGET}s of steps on GPU $AR_GPU (killed at ${SMOKE_LIMIT}s)..."
start=$(date +%s)
AR_EXPORT=$work/model.pt2 timeout --foreground -s KILL "$SMOKE_LIMIT" \
    bash ar/sandbox.sh "$work" python train.py > "$scratch/train.log" 2>&1
status=$?
seconds=$(( $(date +%s) - start ))
tr '\r' '\n' < "$scratch/train.log" | awk '/^step [0-9]+/ { if (n++ % 20) next } { print }' | tail -n 60
if [ "$status" = 124 ] || [ "$status" = 137 ]; then
    echo "smoke: FAILED: killed at ${SMOKE_LIMIT}s. Does train.py stop when prepare.TIME_BUDGET is used up?"
    exit 1
elif [ "$status" != 0 ]; then
    echo "smoke: FAILED: train.py exited with $status after ${seconds}s; see the log above"
    exit 1
elif [ ! -f "$work/model.pt2" ] || [ -L "$work/model.pt2" ]; then
    echo "smoke: FAILED: no model.pt2: call ar.export.save(model) at the end of train.py"
    exit 1
fi
if ! "$AR_PYTHON" -I -B - "$work/model.pt2" <<'EOF'
import sys
sys.path.insert(0, "ar")
import score
try:
    score.validate(sys.argv[1])
except score.Rejected as e:
    sys.exit(f"smoke: FAILED: the scorer rejects the export: {e}")
EOF
then
    exit 1
fi
echo "smoke: OK: train.py ran and exported a model the scorer accepts (${seconds}s)"
