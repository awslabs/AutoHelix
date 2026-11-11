# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Persistent `.autohelix/` state management: git exclusions, archival, active config.

Utilities for the state that lives across runs under a project's `.autohelix/`
directory — as opposed to per-run scaffolding (worktrees, runtime/bin, etc.),
which the harness manages directly.

These helpers have no CLI or harness dependency, so both
the CLI commands (`init`, `clear`, `report`) and the harness can import them
without creating an import cycle.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

# Location (relative to a project's .autohelix/) where `run` records which
# config file the current run used, so a later run can detect a config switch
# and `report` can default to the same config.
_CONFIG_PATH_FILE = ("runtime", "config_path")


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


def read_stored_config_rel(project_path: Path) -> str | None:
    """Return the raw relative config path recorded by the last run, or None.

    Existence-agnostic: returns the stored string even if the file it names no
    longer exists (used to detect a config switch between runs).
    """
    stored = project_path / ".autohelix" / _CONFIG_PATH_FILE[0] / _CONFIG_PATH_FILE[1]
    if not stored.exists():
        return None
    rel = stored.read_text().strip()
    return rel or None


def read_stored_config_path(project_path: Path) -> Path | None:
    """Resolve the stored config path, or None if unset or no longer present.

    Used to default `report` to the same config the run used, so the file must
    still exist.
    """
    rel = read_stored_config_rel(project_path)
    if rel is None:
        return None
    candidate = project_path / rel
    return candidate if candidate.exists() else None


def store_config_path(project_path: Path, rel_path: str) -> None:
    """Record which config the current run is using (relative to project root)."""
    runtime_dir = project_path / ".autohelix" / _CONFIG_PATH_FILE[0]
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / _CONFIG_PATH_FILE[1]).write_text(rel_path + "\n")


def clear_stored_config_path(project_path: Path) -> None:
    """Forget the stored config path so a different config can be used next."""
    stored = project_path / ".autohelix" / _CONFIG_PATH_FILE[0] / _CONFIG_PATH_FILE[1]
    if stored.exists():
        stored.unlink()


def archive_state(project_path: Path, console) -> None:
    """Archive AutoHelix state to .autohelix/archive/<timestamp>/.

    Used by the `clear` command and the config-mismatch prompt in the harness.
    """
    autohelix_dir = project_path / ".autohelix"
    if not autohelix_dir.exists():
        return

    # Create archive directory
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_dir = autohelix_dir / "archive" / timestamp
    archive_dir.mkdir(parents=True, exist_ok=True)

    archived_something = False

    # Archive history.jsonl
    history_file = autohelix_dir / "history.jsonl"
    if history_file.exists():
        shutil.move(str(history_file), str(archive_dir / "history.jsonl"))
        console.print("  [dim]✓[/dim] Archived history")
        archived_something = True

    # Archive agent-facing run state: notes/, observations/, reviews/, peer_notes/,
    # and hints.md. notes/ is recreated clean; the rest are moved wholesale.
    notes_dir = autohelix_dir / "notes"
    if notes_dir.exists() and any(notes_dir.iterdir()):
        shutil.move(str(notes_dir), str(archive_dir / "notes"))
        notes_dir.mkdir()
        console.print("  [dim]✓[/dim] Archived notes")
        archived_something = True

    for name in ("observations", "reviews", "peer_notes"):
        d = autohelix_dir / name
        if d.exists() and any(d.iterdir()):
            shutil.move(str(d), str(archive_dir / name))
            console.print(f"  [dim]✓[/dim] Archived {name}")
            archived_something = True

    hints_path = autohelix_dir / "hints.md"
    if hints_path.exists():
        shutil.move(str(hints_path), str(archive_dir / "hints.md"))
        console.print("  [dim]✓[/dim] Archived hints")
        archived_something = True

    # Archive logs/
    logs_path = autohelix_dir / "logs"
    if logs_path.exists() and any(logs_path.iterdir()):
        shutil.move(str(logs_path), str(archive_dir / "logs"))
        logs_path.mkdir()
        console.print("  [dim]✓[/dim] Archived logs")
        archived_something = True

    # Archive the last run's live status so viewers don't report a cleared run.
    run_status = autohelix_dir / "runtime" / "run.json"
    if run_status.exists():
        shutil.move(str(run_status), str(archive_dir / "run.json"))

    # Archive output/
    output_path = autohelix_dir / "output"
    if output_path.exists() and any(output_path.iterdir()):
        shutil.move(str(output_path), str(archive_dir / "output"))
        console.print("  [dim]✓[/dim] Archived output")
        archived_something = True

    # Clean up empty archive if nothing was archived
    if not archived_something:
        shutil.rmtree(archive_dir)
        archive_parent = autohelix_dir / "archive"
        if archive_parent.exists() and not any(archive_parent.iterdir()):
            archive_parent.rmdir()
        console.print("  [dim]-[/dim] Nothing to archive")
    else:
        console.print(f"  [dim]  → .autohelix/archive/{timestamp}/[/dim]")

    # Clear stored config path so a different config can be used next
    clear_stored_config_path(project_path)
