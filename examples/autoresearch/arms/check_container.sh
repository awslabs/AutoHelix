#!/bin/bash
# Check a running arm's container (container.sh) from the host: what its agent's user can't do.
#
#   bash arms/check_container.sh NAME [OTHER_NAME...]   # e.g. karpathy autohelix karpathy-2
#
# As the agent's user (docker exec -u agent), each of these must fail: opening a GPU
# (torch.cuda.init, nvidia-smi), sudo beyond ar-gpu's three calls, changing ar-gpu, ar-gpu
# train_and_score from outside AutoHelix's harness, reading the data (the validation shard
# included) or ar-gpu's counter and logs, writing the read-only mounts, seeing another
# arm's project or state (OTHER_NAMEs), the operator's home or the test shard, seeing
# processes outside the container. As the container's root: opening a GPU outside the run's
# pair (the device cgroup) and writing the validation shard (a read-only mount). And IMDS
# must hand out the instance role's credentials: Claude Code reaches Bedrock with them (any
# process in the container can; see README.md, "Known limitations"). Prints PASS or FAIL
# per check; exits 1 if any failed.
# Touches no GPU (CUDA init fails before it would). Its refused train_and_score call is logged
# in arms/runs/NAME/calls.jsonl (collect.py flags it): check a test run's container, not a real one's.
set -uo pipefail
ARMS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LOCAL=$(dirname "$ARMS")
name=$1
shift
c=ar-$name
. "$LOCAL/projects/autoresearch/ar/paths.sh"
failed=0

check() {  # check NAME USER COMMAND: PASS if COMMAND fails in the container as USER
    local out
    if out=$(docker exec -u "$2" -w / "$c" bash -c "$3" 2>&1); then
        echo "FAIL $1: it worked: ${out:0:200}"
        failed=1
    else
        echo "PASS $1: ${out:0:160}"
    fi
}

works() {  # works NAME USER COMMAND: PASS if COMMAND succeeds
    local out
    if out=$(docker exec -u "$2" -w / "$c" bash -c "$3" 2>&1); then
        echo "PASS $1: ${out:0:160}"
    else
        echo "FAIL $1: ${out:0:200}"
        failed=1
    fi
}

docker container inspect "$c" > /dev/null || exit 2
project=$LOCAL/projects/ar-$name
minors=$(docker inspect -f '{{range .HostConfig.Devices}}{{.PathOnHost}} {{end}}' "$c" |
         tr ' ' '\n' | sed -n -E 's|^/dev/nvidia([0-9]+)$|\1|p' | paste -sd,)
other=$(for m in 0 1 2 3 4 5 6 7; do [[ ",$minors," == *",$m,"* ]] || { echo "$m"; break; }; done)
echo "== $c: GPU nodes $minors"

# GPUs
check "gpu: torch.cuda.init()" agent "$AR_PYTHON -c 'import torch; torch.cuda.init()'"
check "gpu: nvidia-smi" agent "nvidia-smi -L | grep GPU"
check "gpu: open /dev/nvidia${minors%%,*}" agent "exec 3<> /dev/nvidia${minors%%,*}"
check "gpu: open /dev/nvidiactl" agent "exec 3<> /dev/nvidiactl"
check "gpu: root, a node for GPU $other (outside the pair)" root \
    "mknod /dev/nvidia-x c 195 $other; exec 3<> /dev/nvidia-x; s=\$?; rm -f /dev/nvidia-x; exit \$s"
works "gpu: only the pair's nodes, 0660 root:ar" agent \
    "[ \"\$(stat -c '%n %a %U:%G' /dev/nvidia* | paste -sd' ')\" = \"\$(for n in /dev/nvidia*; do echo \$n 660 root:ar; done | paste -sd' ')\" ] &&
     [ \$(ls /dev | grep -cE '^nvidia[0-9]+$') = 2 ] && ls /dev | grep nvidia | paste -sd' '"

# sudo and the wrapper
check "sudo: a shell" agent "sudo -n bash -c id"
check "sudo: ar-gpu with other arguments" agent "sudo -n /usr/local/sbin/ar-gpu smoke --budget 999 < /dev/null"
check "wrapper: write ar-gpu" agent "echo >> /usr/local/sbin/ar-gpu"
check "wrapper: replace ar-gpu" agent "cp /bin/true /tmp/x && mv /tmp/x /usr/local/sbin/ar-gpu"
check "wrapper: write sudoers" agent "echo >> /etc/sudoers.d/ar"
check "wrapper: train_and_score from outside the harness" agent \
    "sudo -n /usr/local/sbin/ar-gpu train_and_score < $project/train.py"

# data, counter and logs
check "data: list /srv/ar-data" agent "ls /srv/ar-data"
check "data: read the validation shard" agent "head -c 1 /srv/ar-data/data/shard_06542.parquet"
check "data: write the validation shard" agent "echo >> /srv/ar-data/data/shard_06542.parquet"
check "data: root, write the validation shard" root "echo >> /srv/ar-data/data/shard_06542.parquet"
check "data: the host path of the data" agent "ls $AR_DATA"
check "state: read the counter" agent "cat /srv/ar/state/count"
check "state: write the counter" agent "echo 0 > /srv/ar/state/count"
check "state: write experiments.jsonl" agent "echo {} >> /srv/ar/state/experiments.jsonl"
check "state: read run.env" agent "cat /srv/ar/run.env"
check "state: the base commit" agent "git --git-dir=/srv/ar/base.git log -1"

# read-only mounts
check "mounts: write the venv" agent "touch $AR_VENV/x"
check "mounts: write the HF cache" agent "touch $AR_HF_HOME/x"
check "mounts: write Claude Code" agent "echo >> /opt/claude/claude"

# other arms, the test shard, the host
for o in "$@"; do
    check "others: project ar-$o" agent "ls $LOCAL/projects/ar-$o"
    check "others: state of $o" agent "ls $ARMS/runs/$o"
done
works "others: only this project under projects/" agent "[ \"\$(ls $LOCAL/projects)\" = ar-$name ] && ls $LOCAL/projects"
check "others: arms/" agent "ls $ARMS"
check "test shard: root, any test*.parquet" root "find / -path /proc -prune -o -name 'test*.parquet' -print 2>/dev/null | grep ."
check "host: the operator's home" agent "ls $HOME/.ssh $HOME/.aws"
check "host: the Docker socket" agent "ls /var/run/docker.sock"
works "processes: only the container's (pid 1 is its init)" agent \
    "ps -p 1 -o comm= | grep -qx docker-init && echo \$(ps -e --no-headers | wc -l) processes"
check "processes: another arm's (host pids)" agent "ps -eo args | grep -E 'ar-(autohelix|karpathy|goal)' | grep -v ar-$name | grep -v grep"

# Bedrock, through the instance role
works "imds: the instance role's credentials" agent \
    't=$(curl -sf -m 5 -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60") &&
     r=$(curl -sf -m 5 -H "X-aws-ec2-metadata-token: $t" http://169.254.169.254/latest/meta-data/iam/security-credentials/) &&
     curl -sf -m 5 -H "X-aws-ec2-metadata-token: $t" http://169.254.169.254/latest/meta-data/iam/security-credentials/$r | grep -o "\"Code\" : \"Success\""'

exit "$failed"
