#!/bin/bash
# The observe command: train the candidate train.py in the sandbox, then score its export.
#
#   AR_GPU=K[,K2...] bash ar/train_and_score.sh
#
# One training per GPU in AR_GPU (indices as nvidia-smi numbers them), all at once, with
# seeds AR_SEED, AR_SEED+1, ... (AR_SEED default 42). For each:
# 1. Copies train.py, prepare.py and ar/ into a scratch folder ($AUTOHELIX_SCRATCH, else a
#    temporary one) and runs `python train.py` there in ar/sandbox.sh, on its GPU. At
#    TIME_LIMIT seconds from launch (imports, compile and the export included) the
#    sandbox is killed with every process in it. train.py's own timer
#    (prepare.TIME_BUDGET) is editable code: this limit is the one that counts.
# 2. Scores the export (model.pt2) with ar/score.py on the validation shard, outside the
#    sandbox, in a network namespace of its own (no network).
# Then prints each training's log and score, and the metrics as ##autohelix[...] lines:
# val_bpb (the mean of the scorer's), wall_s (the longest training's wall clock, measured
# here), peak_vram_gb (the highest GPU memory use as nvidia-smi reports it, polled every
# 0.5 s: the CUDA context and the allocator's cache included), scoring_s (the longest),
# and train_steps (the mean of train.py's own `num_steps:` lines, when it prints them).
# Exits 1, saying why, when any training times out, fails, writes no export or has its
# export rejected.
#
# AR_SCORE_DATA (default: the validation shard) is the parquet shard to score on: the
# operator's test shard, after a run.
#
# In an arm's container (arms/container/) it goes through `sudo ar-gpu train_and_score`,
# which runs this script from the arms' base commit with this train.py on the container's
# GPUs (AR_SEED and AR_SCORE_DATA unset), and only for AutoHelix's observe command.
set -uo pipefail

# Seconds from launch to exit: the patched baseline's wall clock (338-346 s on an H200, 8
# at once or alone) plus about 10%. See README.md, "Time limit and noise".
TIME_LIMIT=380

cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [ -z "${AR_WRAPPED:-}" ] && [ -x /usr/local/sbin/ar-gpu ]; then
    exec sudo -n /usr/local/sbin/ar-gpu train_and_score < train.py
fi
. ar/paths.sh
VAL_SHARD=shard_06542.parquet

if [ -z "${AR_GPU:-}" ]; then
    echo "train_and_score: set AR_GPU to this run's GPU indices (as nvidia-smi numbers them), e.g. AR_GPU=3 or AR_GPU=2,3"
    exit 2
fi
IFS=, read -ra gpus <<< "$AR_GPU"
seed=${AR_SEED:-42}
scratch=${AUTOHELIX_SCRATCH:-}
if [ -z "$scratch" ]; then
    scratch=$(mktemp -d "${TMPDIR:-/tmp}/ar-train.XXXXXX")
    trap 'rm -rf "$scratch"' EXIT
fi

ms_since() { echo $(( ($(date +%s%N) - $1) / 1000000 )); }

train_and_score() {  # train_and_score FOLDER GPU SEED: writes its results in FOLDER
    local out=$1 gpu=$2 work=$1/work start poller
    mkdir -p "$work/ar"
    cp train.py prepare.py "$work/"
    cp ar/__init__.py ar/export.py ar/score.py "$work/ar/"
    nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -i "$gpu" -lms 500 \
        > "$out/vram.txt" 2>/dev/null &
    poller=$!
    start=$(date +%s%N)
    AR_GPU=$gpu AR_SEED=$3 timeout --foreground -s KILL "$TIME_LIMIT" \
        bash ar/sandbox.sh "$work" python train.py > "$out/train.log" 2>&1
    echo $? > "$out/status"
    ms_since "$start" > "$out/wall_ms"
    kill "$poller" 2>/dev/null
    wait "$poller" 2>/dev/null
    local model=$work/model.pt2
    if [ "$(cat "$out/status")" = 0 ] && [ -f "$model" ] && [ ! -L "$model" ] \
            && [ "$(stat -c %s "$model")" -le 4000000000 ]; then
        start=$(date +%s%N)
        unshare --user --map-root-user --net env CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES="$gpu" \
            HF_HOME="$AR_HF_HOME" HF_HUB_OFFLINE=1 "$AR_PYTHON" -I ar/score.py "$model" \
            --data "${AR_SCORE_DATA:-$AR_DATA/$VAL_SHARD}" --tokenizer "$AR_TOKENIZER" \
            > "$out/score.json" 2> "$out/score.err"
        echo $? > "$out/score_status"
        ms_since "$start" > "$out/scoring_ms"
    fi
}

for i in "${!gpus[@]}"; do
    mkdir -p "$scratch/run$i"
    train_and_score "$scratch/run$i" "${gpus[$i]}" $((seed + i)) &
done
wait

problems=()
for i in "${!gpus[@]}"; do
    out=$scratch/run$i
    status=$(cat "$out/status")
    wall_s=$(awk '{ printf "%.1f", $1 / 1000 }' "$out/wall_ms")
    echo "== training $((i + 1)) (GPU ${gpus[$i]}, AR_SEED=$((seed + i))): log, every 50th step line"
    tr '\r' '\n' < "$out/train.log" | awk '/^step [0-9]+/ { if (n++ % 50) next } { print }' | tail -n 400
    echo "== training $((i + 1)): exit $status after ${wall_s}s (limit ${TIME_LIMIT}s)"
    if [ "$status" = 124 ] || [ "$status" = 137 ]; then
        problems+=("training $((i + 1)) timed out: killed at the ${TIME_LIMIT}s limit (launch to exit, compile and export included)")
    elif [ "$status" != 0 ]; then
        problems+=("training $((i + 1)) failed (exit $status); see its log above")
    elif [ ! -f "$out/score_status" ]; then
        problems+=("training $((i + 1)) wrote no export (call ar.export.save(model) at the end), or one over 4 GB")
    else
        echo "== score $((i + 1)) (exit $(cat "$out/score_status"))"
        cat "$out/score.json"
        echo
        if [ "$(cat "$out/score_status")" != 0 ]; then
            tail -n 20 "$out/score.err"
            problems+=("the scorer rejected training $((i + 1))'s export, or failed; see above")
        fi
    fi
    echo
done
if [ ${#problems[@]} -gt 0 ]; then
    printf 'train_and_score: %s\n' "${problems[@]}"
    exit 1
fi

"$AR_PYTHON" -I - "$scratch" "${#gpus[@]}" <<'EOF'
import json, re, statistics, sys
from pathlib import Path

runs = [Path(sys.argv[1]) / f"run{i}" for i in range(int(sys.argv[2]))]
bpb = [json.loads((run / "score.json").read_text())["val_bpb"] for run in runs]
peak = max(max([float(x) for x in (run / "vram.txt").read_text().split()] or [0]) for run in runs)
steps = [re.findall(r"^num_steps:\s+(\d+)", (run / "train.log").read_text().replace("\r", "\n"), re.M)
         for run in runs]
print("val_bpb per training:", " ".join(f"{b:.6f}" for b in bpb))
print(f"##autohelix[val_bpb={statistics.mean(bpb):.6f}]")
print(f"##autohelix[wall_s={max(int((run / 'wall_ms').read_text()) for run in runs) / 1000:.1f}]")
print(f"##autohelix[peak_vram_gb={peak / 1024:.1f}]")
print(f"##autohelix[scoring_s={max(int((run / 'scoring_ms').read_text()) for run in runs) / 1000:.1f}]")
if all(steps):
    print(f"##autohelix[train_steps={statistics.mean(int(s[-1]) for s in steps):.0f}]")
EOF
