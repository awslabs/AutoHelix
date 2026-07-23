#!/usr/bin/env bash
# Run autohelix inside Docker with the current directory mounted.
#
# Usage:
#   docker/run.sh --env ~/my-env.sh run --config autohelix.yaml -n 5
#   docker/run.sh --env ~/my-env.sh report
#
# Tip: alias autohelix-docker='path/to/docker/run.sh --env ~/my-env.sh'
set -euo pipefail

# Parse --env flag (must be first argument)
ENV_SCRIPT=""
if [ "${1:-}" = "--env" ]; then
  ENV_SCRIPT="$2"
  shift 2
fi

IMAGE="${AUTOHELIX_IMAGE:-autohelix-agent:latest}"

ARGS=(
  --rm -it
  -u "$(id -u):$(id -g)"
  -e HOME=/home/user
  -v "$(pwd):/workspace:rw"
  --network=host
)

# Mount and source env script
if [ -n "$ENV_SCRIPT" ] && [ -f "$ENV_SCRIPT" ]; then
  ARGS+=(-v "$(cd "$(dirname "$ENV_SCRIPT")" && pwd)/$(basename "$ENV_SCRIPT"):/home/user/env.sh:ro")
  ARGS+=(-e AUTOHELIX_ENV_SCRIPT=/home/user/env.sh)
fi

exec docker run "${ARGS[@]}" "$IMAGE" "$@"
