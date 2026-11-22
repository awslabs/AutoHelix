#!/bin/bash
# (Re)create the arms' projects, all at one commit: the first commit of setup.py's project
# (projects/autoresearch: upstream's files, patched, template/ and its ar/paths.sh).
#
#   bash arms/reset_projects.sh [NAME...]   # default: the comparison's six; e.g. ar-karpathy-2
#
# The commit is kept in arms/base.git (BASE: its hash); ar-gpu and rescore.py score from
# it, so only train.py ever comes from an arm's project. Each NAME is removed and cloned
# again from base.git (no remote), and its run's logs, arms/runs/NAME without ar-, are
# removed (through Docker: ar-gpu's files there are root's). With no project named,
# base.git is rebuilt first (fixed author and date: the same files give the same hash).
# Refuses while a container ar-NAME exists.
set -euo pipefail
ARMS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LOCAL=$(dirname "$ARMS")
SRC=$LOCAL/projects/autoresearch        # made by setup.py; only its first commit is used
names=("$@")
[ ${#names[@]} -gt 0 ] || names=(ar-autohelix ar-autohelix-2 ar-karpathy ar-karpathy-2 ar-goal ar-goal-2)
for name in "${names[@]}"; do
    if docker container inspect "${name}" > /dev/null 2>&1; then
        echo "reset_projects: a container $name exists; stop and remove it first (docker rm -f $name)" >&2
        exit 2
    fi
done

if [ $# -eq 0 ] || [ ! -d "$ARMS/base.git" ]; then
    tree=$(mktemp -d)
    git -C "$SRC" archive "$(git -C "$SRC" rev-list --max-parents=0 HEAD)" | tar -x -C "$tree"
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
for name in "${names[@]}"; do
    rm -rf "${LOCAL:?}/projects/$name"
    if [ -d "$ARMS/runs/${name#ar-}" ]; then
        docker run --rm --entrypoint rm -v "$ARMS/runs:/runs" ar-arm -rf "/runs/${name#ar-}"
    fi
    git clone -q "$ARMS/base.git" "$LOCAL/projects/$name"
    git -C "$LOCAL/projects/$name" remote remove origin
    # Only the AutoHelix arms use the experiment folder autohelix/; the others shouldn't read its goal text.
    if [[ $name != ar-autohelix* ]]; then
        git -C "$LOCAL/projects/$name" rm -rq autohelix
        git -C "$LOCAL/projects/$name" commit -q -m "Remove autohelix/ (not this arm's)"
    fi
    echo "projects/$name at $(git -C "$LOCAL/projects/$name" rev-parse --short HEAD)"
done
