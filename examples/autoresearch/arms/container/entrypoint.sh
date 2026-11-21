#!/bin/bash
# The command of an arm's container, run as root (arms/container.sh starts it):
#
#   entrypoint.sh autohelix [RUN ARGS...]   # `autohelix run ARGS` in AR_PROJECT
#   entrypoint.sh session PROMPT            # one `claude -p PROMPT` in AR_PROJECT (Karpathy, /goal)
#
# 1. Writes /srv/ar/run.env, what ar-gpu trusts: the run's kind, the base commit, the
#    host's GPU indices, the experiment cap and Claude Code's path, as the container was
#    started.
# 2. Gives the GPU device nodes to root and group ar (0660): the agent's user can't open
#    them, ar-gpu's trainings (user ar) can. Links the driver's libraries (ldconfig).
#    Nothing here may use the GPUs as root (nvidia-smi included): the driver's libraries
#    would recreate the nodes 0666, as the host's /proc/driver/nvidia/params says.
# 3. Runs the arm as the agent's user, its output in /srv/ar/state (arms/runs/NAME on the host):
#    console.log (autohelix), or session.jsonl (the stream), session.err, session-end.json
#    (exit code: 124 for the AR_WALL cap) and transcript.jsonl (Claude Code's own record).
#    The session gets the flags AutoHelix's backend uses (stream-json with partial messages,
#    --dangerously-skip-permissions, model and effort of template/autohelix.yaml) plus
#    --max-budget-usd AR_MAX_BUDGET_USD, under `timeout AR_WALL`.
# 4. Kills whatever the agent's user left running. Then the container ends.
set -uo pipefail
kind=$1
shift
state=/srv/ar/state
umask 022

printf 'AR_KIND=%q\nAR_BASE=%q\nAR_GPUS=%q\nAR_MAX_EXPERIMENTS=%q\nAR_CLAUDE=%q\n' \
    "$kind" "$AR_BASE" "$AR_GPUS" "$AR_MAX_EXPERIMENTS" "$AUTOHELIX_CLAUDE_CODE_CMD" > /srv/ar/run.env
chown root:ar /dev/nvidia* && chmod 0660 /dev/nvidia* || exit 2
ldconfig && ln -sf libcuda.so.1 /usr/lib64/libcuda.so  # Triton links -lcuda

as_agent=(setpriv --reuid=agent --regid=agent --init-groups -- env HOME=/home/agent USER=agent LOGNAME=agent)
cd "$AR_PROJECT" || exit 2
pid=
trap '[ -n "$pid" ] && kill -TERM "$pid"' TERM INT  # docker stop: Ctrl-C for the harness or the session
started=$(date +%s)
echo "entrypoint: $AR_RUN ($kind) in $AR_PROJECT on host GPUs $AR_GPUS"
case $kind in
    autohelix)
        "${as_agent[@]}" autohelix run "$@" > "$state/console.log" 2>&1 < /dev/null &
        pid=$!
        echo "$pid" > /srv/ar/harness.pid
        ;;
    session)
        timeout "$AR_WALL" "${as_agent[@]}" "$AUTOHELIX_CLAUDE_CODE_CMD" --print --verbose \
            --output-format stream-json --include-partial-messages --dangerously-skip-permissions \
            --model "$ANTHROPIC_MODEL" --effort high --max-budget-usd "$AR_MAX_BUDGET_USD" \
            "$1" > "$state/session.jsonl" 2> "$state/session.err" < /dev/null &
        pid=$!
        ;;
    *)
        echo "entrypoint: unknown kind $kind" >&2
        exit 2
        ;;
esac
wait "$pid"
code=$?
while kill -0 "$pid" 2>/dev/null; do wait "$pid"; code=$?; done  # a signal interrupted the first wait
pkill -KILL -u agent

if [ "$kind" = session ]; then
    sid=$(head -n 1 "$state/session.jsonl" | python3 -I -c 'import json, sys; print(json.load(sys.stdin)["session_id"])' 2>/dev/null)
    # read as the agent's user: the path is the agent's, and could be a symlink to anything
    [ -n "$sid" ] && "${as_agent[@]}" sh -c 'cat "$1"/projects/*/"$2.jsonl"' sh "$CLAUDE_CONFIG_DIR" "$sid" \
        > "$state/transcript.jsonl" 2>/dev/null
    printf '{"arm": "%s", "started": %s, "ended": %s, "exit_code": %s}\n' "$AR_RUN" "$started" "$(date +%s)" "$code" \
        > "$state/session-end.json"
fi
echo "entrypoint: $AR_RUN ended, exit code $code"
exit "$code"
