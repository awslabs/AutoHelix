#!/bin/sh
# Container entry point for one task and one arm (runs as root, then drops to `agent`):
#   start.sh TASK autohelix ITERATIONS      # autohelix run -n ITERATIONS --no-report
#   start.sh TASK goal MAX_BUDGET_USD       # claude -p "/goal <the config's goal, goal-text.py>"
#                                           # (exit 124: the 5.5 h cap)
#   start.sh TASK best ATTEMPT              # one autohelix run -n 1 (best-of-N attempt)
# Mounts: /data-in/TASK (dev instances, read-only), /work (results, owned by uid 1000),
# /run/bedrock (the Bedrock key, read-only, kept fresh by run.py).
set -eu
task=$1; arm=$2; arg=$3
printf '%s\n' "$task" > /etc/algotune/task
mkdir -m 700 /etc/algotune/data
cp -r "/data-in/$task" /etc/algotune/data/
chmod -R go-rwx /etc/algotune/data
project=/work/project
if [ ! -f "$project/autohelix/algotune/config.yaml" ]; then  # scaffolded by root: AlgoTune is root's alone
  python -I /opt/algotune-eval/setup.py "$task" --dir "$project" --algotune /opt/algotune
  chown -R agent:agent "$project"
fi
exec setpriv --reuid=agent --regid=agent --init-groups \
  env HOME=/home/agent PATH="$PATH" sh -eu -c '
    task=$1; arm=$2; arg=$3; project=/work/project
    cd "$project"
    case "$arm" in
      autohelix|best)
        n=$arg; [ "$arm" = best ] && n=1
        autohelix run -n "$n" --no-report;;
      goal)
        goal=$(python -I /opt/goal-text.py autohelix/algotune/config.yaml)
        set +e  # a Bedrock API key is not refreshed mid-session and lives ~6 h: stop at 5.5 h
        timeout --signal=TERM 19800 claude-bedrock -p "/goal $goal" --output-format json \
          --dangerously-skip-permissions --max-budget-usd "$arg" > /work/goal.json 2> /work/goal.stderr
        echo "exit $?" >> /work/goal.stderr;;
      *) echo "unknown arm $arm" >&2; exit 2;;
    esac
  ' sh "$task" "$arm" "$arg"
