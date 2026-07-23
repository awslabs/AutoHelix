#!/usr/bin/env bash
# Quick real-agent dev test against a bundled example.
#
# Scaffolds the example into a fresh temp dir (via setup_example.py, the same
# tool users run), then `autohelix init` + runs the loop. Use --parallel to run
# N identical workers through `autohelix parallel` instead.
#
# Usage:
#   bash scripts/run_dev_test.sh                       # sorting, 1 iteration
#   bash scripts/run_dev_test.sh sorting 3             # sorting, 3 iterations
#   bash scripts/run_dev_test.sh ml-recipe 2           # any bundled example
#   bash scripts/run_dev_test.sh sorting --parallel 2  # 2 identical workers
#   bash scripts/run_dev_test.sh sorting 2 --parallel 2  # 2 workers, 2 iters each
#   bash scripts/run_dev_test.sh sorting 3 --verbose   # forward extra flags
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

EXAMPLE="sorting"
ITERATIONS=1
ITER_SET=0        # was an explicit iteration count given?
PARALLEL=0
EXTRA_ARGS=()

# First positional (if it doesn't start with -) is the example name.
if [[ $# -gt 0 && "$1" != -* ]]; then
  EXAMPLE="$1"; shift
fi
# Second positional (if numeric) is the iteration count.
if [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]]; then
  ITERATIONS="$1"; ITER_SET=1; shift
fi
# Remaining args: --parallel N is consumed here, everything else is forwarded.
while [[ $# -gt 0 ]]; do
  case "$1" in
    --parallel)
      PARALLEL="${2:?--parallel needs a worker count}"; shift 2 ;;
    *)
      EXTRA_ARGS+=("$1"); shift ;;
  esac
done

PROJECT="$(mktemp -d)/$EXAMPLE"
python "$REPO_ROOT/examples/setup_example.py" "$EXAMPLE" --dir "$PROJECT"
cd "$PROJECT"
autohelix init .

if [[ "$PARALLEL" -gt 0 ]]; then
  echo ""
  echo "=== Parallel dev test: $EXAMPLE ==="
  echo "  Dir: $PROJECT"
  echo "  Workers: $PARALLEL (identical configs)"
  # `autohelix parallel` reads iterations from each worker's budget.iterations
  # (it forbids forwarding -n), so bake an explicit count into the configs.
  [[ "$ITER_SET" -eq 1 ]] && echo "  Iterations per worker: $ITERATIONS"
  echo ""
  worker_flags=()
  for i in $(seq 1 "$PARALLEL"); do
    cp autohelix.yaml "worker-$i.yaml"
    if [[ "$ITER_SET" -eq 1 ]]; then
      python - "worker-$i.yaml" "$ITERATIONS" <<'PY'
import sys, yaml
path, iters = sys.argv[1], int(sys.argv[2])
cfg = yaml.safe_load(open(path)) or {}
cfg.setdefault("budget", {})["iterations"] = iters
yaml.safe_dump(cfg, open(path, "w"), sort_keys=False)
PY
    fi
    worker_flags+=(--worker "worker-$i.yaml")
  done
  autohelix parallel "${worker_flags[@]}" -p "$PROJECT" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
else
  echo ""
  echo "=== Dev test: $EXAMPLE ==="
  echo "  Dir: $PROJECT"
  echo "  Iterations: $ITERATIONS"
  echo ""
  autohelix run -n "$ITERATIONS" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
fi

echo ""
echo "Done. Project at: $PROJECT"
