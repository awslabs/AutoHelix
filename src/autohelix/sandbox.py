# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Child-process environments and readiness checks on the user's checkout."""

import os
import sys
from pathlib import Path

from autohelix.workspace import Repo


def build_worktree_env(project_path: Path, worktree_path: Path) -> dict[str, str]:
    """Environment for a process in a copy, with the project's sys.path entries remapped.

    Editable installs (pip install -e) put the project's source folders on
    sys.path through .pth files; a process in a copy must import the copy's
    sources instead. Entries count only on a path boundary (`/p/proj2` is not
    under `/p/proj`). Installed packages (site-packages, dist-packages, e.g. a
    venv inside the project) and the project's own `.autohelix/` state are not
    sources and are never remapped.
    """
    env = os.environ.copy()
    worktree_str = str(worktree_path)

    # The inherited PWD points at wherever `autohelix run` was launched (the main
    # repo). Some agents (e.g. opencode) resolve their working directory from
    # $PWD rather than the process cwd, so they would operate in the main repo
    # instead of the copy. Pin PWD to the copy to match the subprocess cwd.
    env["PWD"] = worktree_str

    roots = {Path(os.path.normpath(project_path)), Path(os.path.realpath(project_path))}
    remapped_paths = {}  # ordered, without duplicates
    for entry in sys.path:
        if not os.path.isabs(entry):  # also skips "" (the current directory)
            continue
        # Either side may be spelled through a symlink (macOS: /var is /private/var).
        spellings = (Path(os.path.normpath(entry)), Path(os.path.realpath(entry)))
        relative = next((path.relative_to(root) for path in spellings for root in roots
                         if path.is_relative_to(root)), None)
        if (relative is None or relative.parts[:1] == (".autohelix",)
                or {"site-packages", "dist-packages"} & set(relative.parts)):
            continue
        remapped_paths[str(Path(worktree_path) / relative)] = None

    if remapped_paths:
        existing = env.get("PYTHONPATH", "")
        new_pythonpath = os.pathsep.join(remapped_paths)
        env["PYTHONPATH"] = f"{new_pythonpath}{os.pathsep}{existing}" if existing else new_pythonpath

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
                "Check scope.editable in the config."
            )
    if status := user_git(repo, project, "status", "--porcelain", "--", *editable).strip():
        raise RuntimeError(
            "Uncommitted changes in editable files. "
            f"Commit them before running AutoHelix:\n{status}"
        )
