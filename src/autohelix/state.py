# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Persistent `.autohelix/` state management: git exclusions, archival, active config.

Utilities for the state that lives across runs under a project's `.autohelix/`
directory — as opposed to per-run scaffolding (runs/, runtime/bin, etc.),
which the harness manages directly.

These helpers have no CLI or harness dependency, so both
the CLI commands (`init`, `clear`, `report`) and the harness can import them
without creating an import cycle.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

class GitExcludeError(RuntimeError):
    """Local exclusions could not keep AutoHelix runtime state out of git."""


def ensure_git_exclude_entry(project_path: Path, entry: str) -> None:
    """Add a local ignore rule without changing the working tree or index.

    The common git directory's info/exclude is shared by linked worktrees.
    Git may return a relative path, including when the project is a subdirectory.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=project_path, capture_output=True, text=True, check=True,
    )
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = project_path / common_dir
    exclude = common_dir / "info" / "exclude"
    content = exclude.read_text() if exclude.exists() else ""
    if entry in content.splitlines():
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as f:
        if content and not content.endswith("\n"):
            f.write("\n")
        f.write(f"{entry}\n")


def ensure_runtime_ignored(project_path: Path) -> None:
    """Require an effective runtime exclusion, including on repeated calls."""
    try:
        ensure_git_exclude_entry(project_path, ".autohelix/")
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", ".autohelix/"],
            cwd=project_path, capture_output=True, text=True,
        )
        if result.returncode == 0:
            return
        if result.returncode != 1:
            raise GitExcludeError(
                f"Could not check whether .autohelix/ is ignored: {result.stderr.strip()}"
            )
        # Without the trailing slash Git also identifies negations such as !.*.
        detail = subprocess.run(
            [
                "git", "check-ignore", "--verbose", "--non-matching", "--",
                ".autohelix/", ".autohelix",
            ],
            cwd=project_path, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as e:
        raise GitExcludeError(f"Could not configure local git exclusions: {e}") from e
    rule = next(
        (line.split("\t", 1)[0] for line in detail.stdout.splitlines()
         if not line.startswith("::")),
        None,
    )
    source = f" Conflicting ignore rule: {rule}." if rule else ""
    raise GitExcludeError(
        ".autohelix/ is not effectively ignored; a git ignore rule overrides "
        f"the local exclusion.{source} Add .autohelix/ after the conflicting "
        "negation rules in your .gitignore, then retry. "
        "AutoHelix has not modified your .gitignore."
    )


def read_stored_config_path(project_path: Path, run_id: str | None = None) -> Path | None:
    """The selected run's original config; observers may override it explicitly."""
    from autohelix.runs import RunPaths

    paths = RunPaths(project_path, run_id)
    if not paths.run_id:
        return None
    stored = paths.read_identity()["config_path"]
    candidate = project_path / stored
    return candidate if candidate.exists() else None
