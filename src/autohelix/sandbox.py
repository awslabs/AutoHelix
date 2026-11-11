# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Child-process environments and readiness checks on the user's checkout."""

import os
import sys
from pathlib import Path

from autohelix.workspace import Repo


def build_worktree_env(project_path: Path, worktree_path: Path) -> dict[str, str]:
    """Build environment with PYTHONPATH remapped from project to worktree.

    When using editable installs (pip install -e), .pth files add the project's
    source directories to sys.path. In worktrees, we need to remap those paths
    so Python imports from the worktree, not the main repo.
    """
    env = os.environ.copy()
    project_str = str(project_path)
    worktree_str = str(worktree_path)

    # The inherited PWD points at wherever `autohelix run` was launched (the main
    # repo). Some agents (e.g. opencode) resolve their working directory from
    # $PWD rather than the process cwd, so they would operate in the main repo
    # instead of the worktree — editing the wrong files and failing the merge.
    # Pin PWD to the worktree to match the subprocess cwd.
    env["PWD"] = worktree_str

    # Find sys.path entries under the project directory and remap to worktree
    remapped_paths = []
    for p in sys.path:
        if p.startswith(project_str) and "/.autohelix/" not in p and not p.endswith("/.autohelix"):
            relative = p[len(project_str):]
            remapped = worktree_str + relative
            remapped_paths.append(remapped)

    if remapped_paths:
        existing = env.get("PYTHONPATH", "")
        new_pythonpath = ":".join(remapped_paths)
        env["PYTHONPATH"] = f"{new_pythonpath}:{existing}" if existing else new_pythonpath

    return env


def user_git(repo: Repo, project: Path, *args: str) -> str:
    """Run git on the user's checkout (its own work tree and index); stdout as text."""
    result = repo.git(*args, work_tree=repo.root, cwd=project, user_checkout=True)
    return result.stdout.decode(errors="replace")


def ensure_clean_working_tree(repo: Repo, project: Path) -> None:
    """Ensure the main repo has no uncommitted changes to tracked files."""
    dirty = []
    for line in user_git(repo, project, "status", "--porcelain").strip().split("\n"):
        if not line or line.strip().startswith("?"):
            continue
        path = line[3:] if len(line) >= 4 else line
        if path == ".autohelix" or path.startswith(".autohelix/"):
            continue
        dirty.append(line)
    if dirty:
        raise RuntimeError(
            "Uncommitted changes in the repo:\n"
            + "\n".join(f"  {line}" for line in dirty[:10])
            + "\n\nCommit or stash them before running AutoHelix."
        )


def ensure_editable_files_ready(repo: Repo, project: Path, editable: list[str]) -> None:
    """Ensure editable files exist and have no uncommitted changes."""
    if not editable:
        return
    for pattern in editable:
        if pattern not in (".", "./") and not list(project.glob(pattern)):
            raise RuntimeError(
                f"No files found matching editable pattern '{pattern}'. "
                "Check your autohelix.yaml scope.editable config."
            )
    if status := user_git(repo, project, "status", "--porcelain", "--", *editable).strip():
        raise RuntimeError(
            "Uncommitted changes in editable files. "
            f"Commit them before running AutoHelix:\n{status}"
        )
