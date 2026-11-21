#!/bin/bash
# Score runs again once they end (rescore.py): 6 seed pairs on the validation shard, then
# 3 on the test shard, one item after the other.
#
#   nohup bash arms/score_chain.sh GPUS TEST_SHARD NAME[:EXPERIMENT]... > logs/score_chain.log 2>&1 &
#   # e.g. 0,1 ~/ar-heldout/test.parquet autohelix karpathy karpathy:12
#
# GPUS: two free GPUs (host indices). EXPERIMENT: as rescore.py's, default best. Each item
# first waits for its run's container (ar-NAME) to exit, if there is one. The output goes
# to logs/rescore/NAME[-EXPERIMENT]-val.log and -test.log.
set -uo pipefail
ARMS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
logs=$(dirname "$ARMS")/logs/rescore
gpus=$1 test_shard=$(realpath "$2")
shift 2
mkdir -p "$logs"
for item in "$@"; do
    IFS=: read -r name experiment <<< "$item"
    experiment=${experiment:-best}
    docker wait "ar-$name" > /dev/null 2>&1
    tag=$name
    [ "$experiment" = best ] || tag=$name-$experiment
    AR_GPU=$gpus python3 "$ARMS/rescore.py" "$name" "$experiment" -k 6 > "$logs/$tag-val.log" 2>&1
    AR_GPU=$gpus AR_SCORE_DATA=$test_shard python3 "$ARMS/rescore.py" "$name" "$experiment" -k 3 \
        > "$logs/$tag-test.log" 2>&1
done
