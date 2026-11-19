# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""An experiment folder's snapshot (`autohelix/NAME/`, `Config.experiment`): what a run
stores of it at creation, as `runs/<id>/experiment/` (`RunPaths.experiment`).

`snapshot_files` decides what goes in, and refuses, before anything is written;
`build_snapshot` copies it into a folder beside its destination, which the caller
renames into place; `snapshot_changes` is what `resume` compares."""

import os
import secrets
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from autohelix.config import CONFIG_FILE, Config, ConfigError

MAX_FILES = 200
MAX_BYTES = 10 * 1024 * 1024
SKIPPED_FOLDERS = ("__pycache__",)


@dataclass(frozen=True)
class SnapshotFile:
    """One file of the snapshot: its name in the folder (`/`-separated), the file read
    (a link's target), its size and whether it is executable."""

    name: str
    source: Path
    size: int
    executable: bool


def _checkout(project: Path) -> Path:
    """The git checkout links may point into (the project's top level)."""
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=project, capture_output=True, text=True)
    return Path(os.path.realpath(top.stdout.strip() if top.returncode == 0 and top.stdout.strip() else project))


def _size(count: int) -> str:
    return f"{count / (1024 * 1024):.1f} MB" if count >= 100 * 1024 else f"{max(1, round(count / 1024))} KB"


def shown(project: Path, folder: Path) -> str:
    """The folder as messages name it: project-relative, with a trailing slash."""
    try:
        return folder.relative_to(project).as_posix() + "/"
    except ValueError:
        return str(folder) + "/"


def snapshot_files(config: Config, project: Path) -> list[SnapshotFile] | None:
    """What the run stores of the config's experiment folder, by name; None without one
    (the project's autohelix.yaml).

    Left out: the config file and the prompt template (stored already), dotfiles and
    dot-folders, `__pycache__/`. A link is followed when its target is inside the git
    checkout. Raises ConfigError, naming the files, for links outside it (or to nothing),
    files that aren't regular (a FIFO, a socket) and a folder over the cap
    (`MAX_FILES`, `MAX_BYTES`)."""
    folder = config.experiment
    if folder is None:
        return None
    project = Path(project)
    checkout = _checkout(project)
    left_out = {os.path.realpath(folder / CONFIG_FILE)}
    if config.prompt_template is not None:
        left_out.add(os.path.realpath(config.prompt_template))
    found: list[SnapshotFile] = []
    refused: list[str] = []

    def visit(directory: Path, prefix: str, seen: frozenset[str]) -> None:
        with os.scandir(directory) as scan:
            entries = sorted(scan, key=lambda e: e.name)
        for entry in entries:
            if entry.name.startswith("."):
                continue
            name = prefix + entry.name
            path = Path(entry.path)
            target = os.path.realpath(path)
            if target in left_out:
                continue
            if entry.is_symlink():
                link = os.readlink(path)
                if not Path(target).is_relative_to(checkout):
                    refused.append(f"{name} (a link to {link}, outside the checkout)")
                    continue
                if not os.path.exists(target):
                    refused.append(f"{name} (a link to {link}, which doesn't exist)")
                    continue
            info = os.stat(target)
            if stat.S_ISDIR(info.st_mode):
                if entry.name in SKIPPED_FOLDERS:
                    continue
                if target in seen:
                    refused.append(f"{name} (a link to a folder that contains it)")
                    continue
                visit(path, name + "/", seen | {target})
            elif stat.S_ISREG(info.st_mode):
                found.append(SnapshotFile(name, Path(target), info.st_size, bool(info.st_mode & stat.S_IXUSR)))
            else:
                refused.append(f"{name} (not a regular file)")

    visit(folder, "", frozenset({os.path.realpath(folder)}))
    where = shown(project, folder)
    if refused:
        raise ConfigError(f"{where} can't be stored with the run: " + "; ".join(refused) + ". "
                          "Move these out of it, or link only to files inside the checkout.")
    total = sum(file.size for file in found)
    if len(found) > MAX_FILES or total > MAX_BYTES:
        largest = sorted(found, key=lambda file: (-file.size, file.name))[:5]
        raise ConfigError(
            f"{where} is too large to store with the run: {len(found)} files, {_size(total)} "
            f"(at most {MAX_FILES} files and {MAX_BYTES // (1024 * 1024)} MB). Largest: "
            + ", ".join(f"{file.name} ({_size(file.size)})" for file in largest) + ". "
            "Keep datasets out of it: name them by path (in the goal or a command) instead.")
    return found


def build_snapshot(files: list[SnapshotFile], beside: Path) -> Path:
    """Copy `files` into a new folder in `beside`, named with a leading dot (no run
    listing reads it), and return it for the caller to rename into place. Files lose
    their write bits and keep their exec bit; folders stay writable by their owner, so
    the snapshot can be deleted. On failure, nothing is left."""
    beside.mkdir(parents=True, exist_ok=True)
    staging = beside / f".experiment-{secrets.token_hex(4)}"
    staging.mkdir(mode=0o755)
    try:
        for file in files:
            destination = staging / file.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file.source, destination)
            os.chmod(destination, 0o555 if file.executable else 0o444)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def snapshot_changes(config: Config, project: Path, stored: Path) -> str | None:
    """How the live experiment folder differs from a run's snapshot (`stored`), as
    words: the files changed (bytes or exec bit), added and removed, or why the folder
    can no longer be stored; None when they match. Compared file by file: the snapshot
    is the manifest."""
    try:
        live = {file.name: file for file in snapshot_files(config, project) or []}
    except ConfigError as e:
        return f"it can no longer be stored: {e}"
    kept = {path.relative_to(stored).as_posix(): path for path in stored.rglob("*")
            if path.is_file() and not path.is_symlink()}
    changed = []
    for name in sorted(live.keys() & kept.keys()):
        file, copy = live[name], kept[name]
        if (file.executable != bool(copy.stat().st_mode & stat.S_IXUSR)
                or file.size != copy.stat().st_size or file.source.read_bytes() != copy.read_bytes()):
            changed.append(name)
    parts = [f"{label}: {', '.join(names)}" for label, names in (
        ("changed", changed), ("added", sorted(live.keys() - kept.keys())),
        ("removed", sorted(kept.keys() - live.keys()))) if names]
    return "; ".join(parts) or None
