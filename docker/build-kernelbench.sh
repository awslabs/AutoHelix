#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${AUTOHELIX_KERNELBENCH_IMAGE:-autohelix-kernelbench:latest}"

docker build -t "$IMAGE" -f "$SCRIPT_DIR/Dockerfile.kernelbench" "$SCRIPT_DIR"
echo "Built: $IMAGE"
