#!/bin/bash
# Runs of one arm one after the other on the same GPUs: each starts when the previous
# one's container (ar-NAME, by its exact name) has exited.
#
#   nohup bash arms/queue.sh ARM GPUS NAME... > logs/queue-ARM.log 2>&1 &
#   # e.g. karpathy 2,3 karpathy karpathy-2;  goal 4,5 goal goal-2
#
# Stops at the first launch that fails. Kill it to cancel the runs not yet started.
set -euo pipefail
ARMS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
arm=$1 gpus=$2
shift 2
for name in "$@"; do
    echo "queue: $(date -u +%FT%TZ) starting $name on GPUs $gpus"
    bash "$ARMS/$arm/launch.sh" "$name" "$gpus"
    code=$(docker wait "ar-$name")
    echo "queue: $(date -u +%FT%TZ) ar-$name exited with $code"
done
