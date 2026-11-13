#!/usr/bin/env bash
# Quick real-agent dev test against a bundled example.
#
# Scaffolds the example into a fresh temp dir (via setup_example.py, the same
# tool users run: a git repo with the config committed), then runs the loop.
#
# Usage:
#   bash scripts/run_dev_test.sh                       # sorting, 1 iteration
#   bash scripts/run_dev_test.sh sorting 3             # sorting, 3 iterations
#   bash scripts/run_dev_test.sh ml-recipe 2           # any bundled example
#   bash scripts/run_dev_test.sh sorting 3 --verbose   # forward extra flags
#   bash scripts/run_dev_test.sh sorting 2 --group g   # a grouped run (start more with
#                                                      #   autohelix run -p <dir> --group g)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

EXAMPLE="sorting"
ITERATIONS=1

# First positional (if it doesn't start with -) is the example name.
if [[ $# -gt 0 && "$1" != -* ]]; then
  EXAMPLE="$1"; shift
fi
# Second positional (if numeric) is the iteration count.
if [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]]; then
  ITERATIONS="$1"; shift
fi

PROJECT="$(mktemp -d)/$EXAMPLE"
python "$REPO_ROOT/examples/setup_example.py" "$EXAMPLE" --dir "$PROJECT"
cd "$PROJECT"

echo ""
echo "=== Dev test: $EXAMPLE ==="
echo "  Dir: $PROJECT"
echo "  Iterations: $ITERATIONS"
echo ""
autohelix run -n "$ITERATIONS" "$@"

echo ""
echo "Done. Project at: $PROJECT"
