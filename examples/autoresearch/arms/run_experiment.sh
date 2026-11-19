#!/bin/bash
# One experiment for the Karpathy and /goal arms: score the project's train.py.
#
#   run_experiment.sh          (AR_ARM, AR_PROJECT, AR_GPU set by the launch.sh of the arm)
#
# Copies the project's train.py into a fresh copy of the arms' base commit (base.git at
# BASE: only train.py comes from the project) and runs its ar/train_and_score.sh: one
# training per GPU in AR_GPU, seeds 42, 43, then the trusted scorer. Prints that output,
# then the metrics as `name: value` lines, val_bpb first (`val_bpb: none` when it failed).
#
# At most AR_MAX_EXPERIMENTS calls (default 60); after that it refuses and runs nothing.
# Each call appends a JSON line to arms/<arm>/experiments.jsonl (call, start, end,
# train.py's sha256, status, metrics or the failure) and keeps train.py as
# arms/<arm>/train_py/NNN.py and the output as NNN.log.
set -uo pipefail
ARMS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
: "${AR_ARM:?set by the launch.sh of the arm}" "${AR_PROJECT:?set by the launch.sh of the arm}" "${AR_GPU:?set by the launch.sh of the arm}"
max=${AR_MAX_EXPERIMENTS:-60}
out=$ARMS/$AR_ARM
mkdir -p "$out/train_py"

exec 9> "$out/count.lock"
flock 9  # one counter update at a time
n=$(( $(cat "$out/count" 2>/dev/null || echo 0) + 1 ))
if [ "$n" -le "$max" ]; then echo "$n" > "$out/count"; fi
exec 9>&-
if [ "$n" -gt "$max" ]; then
    printf '{"refused": true, "at": %s}\n' "$(date +%s.%N)" >> "$out/experiments.jsonl"
    echo "run_experiment: REFUSED. The experiment budget is used up: all $max experiments have run."
    echo "No more experiments can be run. The experiment loop is over: end the session now."
    exit 3
fi

num=$(printf '%03d' "$n")
start=$(date +%s.%N)
cp "$AR_PROJECT/train.py" "$out/train_py/$num.py"
sha=$(sha256sum "$out/train_py/$num.py" | cut -d' ' -f1)
# Not under /tmp: ar/sandbox.sh fails with its scratch there (its /tmp bind hides the later mount points)
work=$(mktemp -d "$(dirname "$ARMS")/tmp/ar-experiment.XXXXXX")
log=$out/train_py/$num.log
status=killed
finish() {  # the log line, also when the call is killed
    local end
    end=$(date +%s.%N)
    python3 -I - "$n" "$start" "$end" "$sha" "$status" "$log" >> "$out/experiments.jsonl" <<'EOF'
import json, os, re, sys
n, start, end, sha, status, log = sys.argv[1:]
text = open(log, errors="replace").read() if os.path.exists(log) else ""
metrics = {k: float(v) for k, v in re.findall(r"^##autohelix\[(\w+)=([-+0-9.eE]+)\]$", text, re.M)}
problems = re.findall(r"^train_and_score: (.*)$", text, re.M)
print(json.dumps({"call": int(n), "start": float(start), "end": float(end), "gpus": os.environ["AR_GPU"], "train_py_sha256": sha,
                  "status": status, "metrics": metrics if status == "ok" else {},
                  "failure": None if status == "ok" else "; ".join(problems) or status}))
EOF
    rm -rf "$work"
}
trap 'finish; exit 130' INT TERM HUP

echo "run_experiment: experiment $n of $max (GPUs $AR_GPU, about 7 minutes)"
git --git-dir="$ARMS/base.git" archive "$(cat "$ARMS/BASE")" | tar -x -C "$work"
cp "$out/train_py/$num.py" "$work/train.py"
TMPDIR=$work bash "$work/ar/train_and_score.sh" > "$log" 2>&1
code=$?
[ "$code" = 0 ] && status=ok || status=failed
trap - INT TERM HUP
finish
cat "$log"
echo "---"
if [ "$status" = ok ]; then
    sed -n -E 's/^##autohelix\[(\w+)=(.*)\]$/\1: \2/p' "$log"
else
    echo "val_bpb: none (the experiment failed; see above)"
fi
echo "experiments: $n of $max used"
exit "$code"
