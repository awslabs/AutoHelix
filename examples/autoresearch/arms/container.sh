#!/bin/bash
# Start one run of an arm in a container of its own, detached, named ar-NAME (the launchers call it).
#
#   bash arms/container.sh NAME GPUS autohelix [RUN ARGS...]   # autohelix run ARGS
#   bash arms/container.sh NAME GPUS session PROMPT            # one claude -p session
#
# GPUS: the host's indices, e.g. 2,3. The image is ar-arm (container/Dockerfile). The
# container gets:
# - only those GPUs: their device nodes and the control nodes (--device: the device cgroup
#   allows no other), which only root and user ar may open in it (entrypoint.sh); the agent's
#   user reaches them through sudo ar-gpu (container/ar-gpu). The driver's compute and NVML
#   libraries and nvidia-smi are bound read-only (nvidia-container-cli list), not through
#   the NVIDIA runtime: its mask over /proc/driver/nvidia keeps ar/sandbox.sh from mounting
#   a fresh /proc. Don't run nvidia-smi as root in it (docker exec): as root, the driver's
#   libraries recreate the device nodes 0666;
# - its project, projects/ar-NAME, read-write, at its host path; HOME (arms/runs/NAME/home, with
#   CLAUDE_CONFIG_DIR and AUTOHELIX_COPIES_ROOT in it), /tmp and /srv/ar-work (tmpfs) its own;
# - read-only: the venv, its Python and the FA3 cache (ar/paths.sh of projects/autoresearch)
#   at their host paths, Claude Code, AutoHelix's venv (autohelix arm, on a Python there);
#   the training shards, the validation shard and the tokenizer in /srv/ar-data, and the
#   arms' base commit (arms/base.git) in /srv/ar, both out of the agent's reach;
# - arms/runs/NAME at /srv/ar/state (root only): the run's logs and ar-gpu's counter and logs;
# - its own PID namespace (Docker's) and a bridge network: IMDS answers there, so Claude
#   Code reaches Bedrock with the instance role (in AWS_REGION, default us-west-2), and so
#   could any process of the agent (README.md, "Known limitations");
# - seccomp=unconfined and systempaths=unconfined, for ar/sandbox.sh's namespaces and fresh
#   /proc (the agent's user has no capabilities either way).
# Nothing else of the host: no other project or arm, no test shard.
#
# The host folder is the one arms/ is in (setup.py's --local): projects/ (setup.py's
# projects/autoresearch, reset_projects.sh's ar-NAME), claude-home/.local/bin/claude
# (Claude Code's native build) and, for the AutoHelix arm, autohelix-venv/ (AutoHelix).
# AR_MAX_EXPERIMENTS (60), AR_MAX_BUDGET_USD (150) and AR_WALL (24h) are for the session arms.
set -euo pipefail
ARMS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LOCAL=$(dirname "$ARMS")
name=$1 gpus=$2 kind=$3
shift 3
project=$LOCAL/projects/ar-$name
state=$ARMS/runs/$name
. "$LOCAL/projects/autoresearch/ar/paths.sh"  # AR_VENV, AR_PYTHON_HOME, AR_HF_HOME, AR_DATA, AR_TOKENIZER
claude=$(readlink -f "$LOCAL/claude-home/.local/bin/claude")
autohelix=$LOCAL/autohelix-venv
model=us.anthropic.claude-opus-5-5  # as template/autohelix/autoresearch/config.yaml's agent.model

die() { echo "container: $*" >&2; exit 2; }
[ -d "$project/.git" ] || die "no project $project: run arms/reset_projects.sh ar-$name"
if docker container inspect "ar-$name" > /dev/null 2>&1; then
    die "a container ar-$name exists (docker rm ar-$name, once its run is over)"
fi
for f in session.jsonl experiments.jsonl console.log calls.jsonl; do
    [ ! -e "$state/$f" ] || die "$state already has a run's logs ($f): run arms/reset_projects.sh ar-$name"
done
mkdir -p "$state/home"

ro() { args+=(-v "$1:${2:-$1}:ro"); }
devices=(--device /dev/nvidiactl --device /dev/nvidia-uvm --device /dev/nvidia-uvm-tools)
for gpu in ${gpus//,/ }; do  # the node is the minor number's, not always the index's
    minor=$(nvidia-smi -q -i "$gpu" | awk '/Minor Number/ {print $4}')
    [[ "$minor" =~ ^[0-9]+$ ]] || die "no GPU $gpu"
    devices+=(--device "/dev/nvidia$minor")
done
driver=$(nvidia-container-cli list --libraries --binaries |
         grep -E '/(nvidia-smi|lib(cuda|nvidia-ml|nvidia-ptxjitcompiler|nvidia-nvvm|nvidia-gpucomp)\.so\..*)$') ||
    die "no driver libraries from nvidia-container-cli list"
for f in $driver; do
    devices+=(-v "$f:$f:ro")  # entrypoint.sh's ldconfig adds the soname links
done
args=(-d --name "ar-$name" --hostname "ar-$name" --init
      --security-opt seccomp=unconfined --security-opt systempaths=unconfined
      --tmpfs /tmp:rw,exec,nosuid,size=64g --tmpfs /srv/ar-work:rw,exec,nosuid,size=200g,mode=0711
      "${devices[@]}" -v "$project:$project" -v "$state:/srv/ar/state" -v "$state/home:/home/agent")
ro "$AR_VENV"
ro "$AR_PYTHON_HOME"
ro "$AR_HF_HOME"
ro "$claude" /opt/claude/claude
ro "$AR_DATA" /srv/ar-data/data
ro "$AR_TOKENIZER" /srv/ar-data/tokenizer
ro "$ARMS/base.git" /srv/ar/base.git
path=/usr/local/bin:/usr/bin:/usr/sbin
if [ "$kind" = autohelix ]; then
    # Its Python must be one of uv's in AR_PYTHON_HOME, mounted above (README.md)
    [[ $(readlink -f "$autohelix/bin/python") == "$AR_PYTHON_HOME"/* ]] ||
        die "$autohelix/bin/python isn't a Python in $AR_PYTHON_HOME"
    ro "$autohelix"
    path=$autohelix/bin:$path
fi
for var in AR_RUN="$name" AR_GPUS="$gpus" AR_BASE="$(cat "$ARMS/BASE")" AR_PROJECT="$project" \
        AR_MAX_EXPERIMENTS="${AR_MAX_EXPERIMENTS:-60}" AR_MAX_BUDGET_USD="${AR_MAX_BUDGET_USD:-150}" \
        AR_WALL="${AR_WALL:-24h}" PATH="$path" HOME=/home/agent CLAUDE_CONFIG_DIR=/home/agent/.claude \
        AUTOHELIX_CLAUDE_CODE_CMD=/opt/claude/claude AUTOHELIX_COPIES_ROOT=/home/agent/copies \
        CLAUDE_CODE_USE_BEDROCK=1 AWS_REGION="${AWS_REGION:-us-west-2}" ANTHROPIC_MODEL="$model" DISABLE_AUTOUPDATER=1; do
    args+=(-e "$var")
done
[ "$kind" = session ] && args+=(-e CLAUDE_CODE_DISABLE_AUTO_MEMORY=1)

docker run "${args[@]}" ar-arm "$kind" "$@" > /dev/null
echo "container: ar-$name started ($kind, GPUs $gpus); logs in $state, docker logs ar-$name"
