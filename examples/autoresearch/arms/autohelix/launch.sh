#!/bin/bash
# The AutoHelix arm: `autohelix run` in PROJECT with its autohelix.yaml.
#
#   bash arms/autohelix/launch.sh projects/ar-autohelix 0,1 [RUN ARGS]
#
# RUN ARGS default to `-n 60 --time 24h`. Start it in tmux; the run's state is in
# PROJECT/.autohelix/ (collect.py and rescore.sh read it there).
set -euo pipefail
ARMS=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
LOCAL=$(dirname "$ARMS")
project=$(cd "$1" && pwd) gpus=$2
shift 2
[ $# -gt 0 ] || set -- -n 60 --time 24h
. "$LOCAL/agent-env.sh"
export AR_GPU=$gpus ANTHROPIC_MODEL=us.anthropic.claude-opus-5-5
export TMPDIR=$LOCAL/tmp  # for ar/smoke.sh: its sandbox fails with a scratch under /tmp
cd "$project"
exec "$LOCAL/autohelix-venv/bin/autohelix" run "$@"
