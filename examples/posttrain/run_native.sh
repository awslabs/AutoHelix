#!/bin/bash
# Native environment entrypoint.
#
# Runs commands in the prebuilt env, as the host user — NO Docker, NO root, so the
# AutoHelix worktree stays clean and never crashes on cleanup.
#
# Usage:  ./run_native.sh python train_sft.py ...
#         ./run_native.sh python evaluate.py ...
set -euo pipefail

TASK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$TASK_DIR/config.sh"

# GPU assignment from committed gpu_device.txt (next to this script).
GPU_DEVICE_FILE="$TASK_DIR/gpu_device.txt"
if [ ! -f "$GPU_DEVICE_FILE" ]; then
  echo "missing GPU assignment file: $GPU_DEVICE_FILE" >&2
  exit 2
fi
GPU_DEVICE="$(tr -d '[:space:]' < "$GPU_DEVICE_FILE")"
case "$GPU_DEVICE" in
  ""|*[!0-9]*) echo "invalid GPU assignment: '$GPU_DEVICE'" >&2; exit 2 ;;
esac

# Put the env's bin on PATH (so the `vllm` CLI is found by inspect_ai), pin the GPU,
# and point HF caches at the shared location.
export PATH="$UV_ENV/bin:$PATH"
export VIRTUAL_ENV="$UV_ENV"
export CUDA_VISIBLE_DEVICES="$GPU_DEVICE"
export HF_HUB_CACHE="${HF_HUB_CACHE:-$HF_HOME/hub}"
export HF_DATASETS_CACHE="${HF_DATASETS_CACHE:-$HF_HOME/datasets}"
export VLLM_API_KEY="${VLLM_API_KEY:-inspectai}"

exec "$@"
