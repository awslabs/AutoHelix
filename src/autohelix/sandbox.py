# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Child-process environments: the project's sys.path entries remapped to a copy."""

import os
import sys
from pathlib import Path


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
