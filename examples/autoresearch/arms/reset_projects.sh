#!/bin/bash
# (Re)create the arms' projects, all at one commit: the setup-made project's first commit
# with the current template/autohelix.yaml.
#
#   bash arms/reset_projects.sh [NAME...]    # default: the four wave-1 projects; e.g. ar-karpathy-2
#
# The commit is kept in arms/base.git (BASE: its hash); run_experiment.sh and rescore.sh
# score from it, so only train.py ever comes from an arm's project. Each NAME is removed
# and cloned again from base.git (no remote); with no project named, base.git is rebuilt
# first (same hash: fixed author and date). Stop the arm's session first.
set -euo pipefail
ARMS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LOCAL=$(dirname "$ARMS")
SRC=$LOCAL/projects/autoresearch        # made by setup.py; only its first commit is used
YAML=$LOCAL/example/template/autohelix.yaml
names=("$@")
[ ${#names[@]} -gt 0 ] || names=(ar-autohelix ar-autohelix-2 ar-karpathy ar-goal)

if [ $# -eq 0 ] || [ ! -d "$ARMS/base.git" ]; then
    tree=$(mktemp -d)
    git -C "$SRC" archive "$(git -C "$SRC" rev-list --max-parents=0 HEAD)" | tar -x -C "$tree"
    cp "$YAML" "$tree/autohelix.yaml"
    rm -rf "$ARMS/base.git"
    git init -q --bare "$ARMS/base.git"
    export GIT_AUTHOR_NAME=autoresearch GIT_AUTHOR_EMAIL=autoresearch@localhost \
        GIT_COMMITTER_NAME=autoresearch GIT_COMMITTER_EMAIL=autoresearch@localhost \
        GIT_AUTHOR_DATE=2026-11-19T00:00:00Z GIT_COMMITTER_DATE=2026-11-19T00:00:00Z
    git --git-dir="$ARMS/base.git" --work-tree="$tree" add -A
    git --git-dir="$ARMS/base.git" --work-tree="$tree" commit -q -m "autoresearch at upstream 228791f, patched"
    git --git-dir="$ARMS/base.git" rev-parse HEAD > "$ARMS/BASE"
    rm -rf "$tree"
fi
base=$(cat "$ARMS/BASE")
for name in "${names[@]}"; do
    rm -rf "${LOCAL:?}/projects/$name"
    git clone -q "$ARMS/base.git" "$LOCAL/projects/$name"
    git -C "$LOCAL/projects/$name" remote remove origin
    # Only the AutoHelix arms use autohelix.yaml; the others shouldn't read its goal text.
    if [[ $name != ar-autohelix* ]]; then
        git -C "$LOCAL/projects/$name" rm -q autohelix.yaml
        git -C "$LOCAL/projects/$name" commit -q -m "Remove autohelix.yaml (not this arm's)"
    fi
    echo "projects/$name at $(git -C "$LOCAL/projects/$name" rev-parse --short HEAD)"
done
