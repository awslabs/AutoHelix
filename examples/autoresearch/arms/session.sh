#!/bin/bash
# One headless Claude Code session for the Karpathy or /goal arm (their launch.sh call it).
#
#   AR_GPU=J,K bash arms/session.sh ARM PROMPT
#
# Runs `claude -p PROMPT` in projects/ar-ARM with the flags AutoHelix's backend uses
# (src/autohelix/agents/claudecode.py: stream-json with partial messages,
# --dangerously-skip-permissions, the model and effort of template/autohelix.yaml, auto
# memory off) plus --max-budget-usd AR_MAX_BUDGET_USD (default 150), under an outer
# `timeout AR_WALL` (default 24h). The agent gets AR_ARM, AR_PROJECT, AR_GPU and
# AR_MAX_EXPERIMENTS for run_experiment.sh.
#
# Writes arms/ARM/session.jsonl (the stream), session.err, gpu.jsonl (nvidia-smi on
# AR_GPU every 30 s, for the audit in collect.py), session-end.json (exit code: 124 for
# the outer timeout; the stream's last result event has Claude Code's own stop reason) and
# transcript.jsonl, Claude Code's own session record (/goal's verdicts are only there).
set -uo pipefail
ARMS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LOCAL=$(dirname "$ARMS")
arm=$1 prompt=$2
out=$ARMS/$arm
: "${AR_GPU:?set AR_GPU to the two GPUs of this arm, e.g. AR_GPU=2,3}"
if [ -e "$out/session.jsonl" ] || [ -e "$out/experiments.jsonl" ]; then
    echo "session: $out already has a session's logs; move them away first" >&2
    exit 2
fi
mkdir -p "$out"
. "$LOCAL/agent-env.sh"
export AR_ARM=$arm AR_PROJECT=$LOCAL/projects/ar-$arm AR_GPU AR_MAX_EXPERIMENTS=${AR_MAX_EXPERIMENTS:-60}
export ANTHROPIC_MODEL=us.anthropic.claude-opus-5-5 CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
export TMPDIR=$LOCAL/tmp  # for ar/smoke.sh: its sandbox fails with a scratch under /tmp

(  # the GPU poller: one line per GPU every 30 s, until the session ends
    while :; do
        nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader,nounits -i "$AR_GPU" |
            awk -v t="$(date +%s)" -F', *' '{ printf "{\"t\": %s, \"gpu\": %s, \"util\": %s, \"mem_mib\": %s}\n", t, $1, $2, $3 }'
        sleep 30
    done >> "$out/gpu.jsonl"
) &
poller=$!

started=$(date +%s)
echo "session: $arm in $AR_PROJECT on GPUs $AR_GPU, up to $AR_MAX_EXPERIMENTS experiments, \$${AR_MAX_BUDGET_USD:-150}, ${AR_WALL:-24h}"
cd "$AR_PROJECT"
# In a process group of its own, killed whole at the end: nothing the session started outlives it.
setsid --wait timeout "${AR_WALL:-24h}" "$AUTOHELIX_CLAUDE_CODE_CMD" --print --verbose --output-format stream-json \
    --include-partial-messages --dangerously-skip-permissions \
    --model "$ANTHROPIC_MODEL" --effort high --max-budget-usd "${AR_MAX_BUDGET_USD:-150}" \
    "$prompt" > "$out/session.jsonl" 2> "$out/session.err" < /dev/null &
session=$!
trap 'kill -- -$session 2>/dev/null' INT TERM HUP
wait "$session"
code=$?
kill -- -"$session" 2>/dev/null
kill "$poller" 2>/dev/null
wait "$poller" 2>/dev/null
sid=$(head -n 1 "$out/session.jsonl" | python3 -I -c 'import json, sys; print(json.load(sys.stdin)["session_id"])' 2>/dev/null)
cp "$CLAUDE_CONFIG_DIR"/projects/*/"$sid.jsonl" "$out/transcript.jsonl" 2>/dev/null
printf '{"arm": "%s", "started": %s, "ended": %s, "exit_code": %s}\n' "$arm" "$started" "$(date +%s)" "$code" \
    > "$out/session-end.json"
if [ "$code" = 124 ]; then echo "session: stopped by the outer ${AR_WALL:-24h} cap (exit 124)"; else echo "session: ended, exit code $code"; fi
python3 -I "$ARMS/collect.py" --arm "$arm" || true
exit "$code"
