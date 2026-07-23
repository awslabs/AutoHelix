#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

docker build -t autohelix-agent:latest -f "$SCRIPT_DIR/Dockerfile" "$REPO_ROOT"
echo "Built: autohelix-agent:latest"
