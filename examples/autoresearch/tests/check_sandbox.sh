#!/bin/bash
# Check ar/sandbox.sh's restrictions on this machine:
#
#   AR_GPU=K bash tests/check_sandbox.sh PROJECT [HIDDEN_PATH...]
#
# PROJECT is a project setup.py laid out. Runs tests/sandbox_check.py inside the sandbox
# (network, GPU, data, writes, mounts, capabilities, what's visible: the host's data
# folder, ~/.ssh, ~/.aws and each HIDDEN_PATH, e.g. the test shard's folder, must not exist
# inside), then checks that killing the sandbox at a timeout kills every process in it.
set -uo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project=$(cd "$1" && pwd)
shift
. "$project/ar/paths.sh"
: "${AR_GPU:?set AR_GPU}"
scratch=$(mktemp -d "${TMPDIR:-/tmp}/ar-check.XXXXXX")
trap 'rm -rf "$scratch"' EXIT
cp "$here/sandbox_check.py" "$project/prepare.py" "$scratch/"
bus=$(nvidia-smi --query-gpu=pci.bus_id --format=csv,noheader -i "$AR_GPU")
bash "$project/ar/sandbox.sh" "$scratch" python sandbox_check.py "$bus" "$AR_DATA" \
    "$AR_DATA" "$HOME/.ssh" "$HOME/.aws" "$@"
status=$?

# The kill: two sleepers in the background, one in a new session; the sandbox is killed
# at 3 s, as train_and_score.sh kills training at its limit.
n=1$$  # durations no other sleep has
timeout --foreground -s KILL 3 bash "$project/ar/sandbox.sh" "$scratch" \
    sh -c "sleep ${n}1 & setsid sleep ${n}2 & wait" 2>/dev/null
code=$?
sleep 1
left=$(pgrep -f "sleep ${n}[12]")
if [ -z "$left" ] && [ "$code" = 124 ]; then
    echo "PASS kill: no process left after the timeout (exit $code)"
else
    echo "FAIL kill: exit $code, left: $left"
    pkill -9 -f "sleep ${n}[12]"
    status=1
fi
exit $status
