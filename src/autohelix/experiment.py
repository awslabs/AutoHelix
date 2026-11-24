# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""An experiment folder's snapshot (`autohelix/NAME/`, `Config.experiment`): what a run
stores of it at creation, as `runs/<id>/experiment/` (`RunPaths.experiment`).

`snapshot_files` decides what goes in, and refuses, before anything is written;
`build_snapshot` copies it into a folder beside its destination, which the caller
renames into place; `snapshot_changes` is what `resume` compares."""

import errno
import os
import secrets
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from autohelix.config import CONFIG_DIR, CONFIG_FILE, Config, ConfigError

MAX_FILES = 200
MAX_FOLDERS = 200
MAX_BYTES = 10 * 1024 * 1024
MAX_REFUSED = 20  # refusals a message lists; the walk stops after MAX_FILES of them
SKIPPED_FOLDERS = ("__pycache__",)


@dataclass(frozen=True)
class SnapshotFile:
    """One file of the snapshot: its name in the folder (`/`-separated), the file read
    (a link's target), its size and its permission bits."""

    name: str
    source: Path
    size: int
    mode: int

    @property
    def executable(self) -> bool:
        return bool(self.mode & stat.S_IXUSR)


def _size(count: int) -> str:
    return f"{count / (1024 * 1024):.1f} MB" if count >= 100 * 1024 else f"{max(1, round(count / 1024))} KB"


def shown(project: Path, folder: Path) -> str:
    """The folder as messages name it: project-relative, with a trailing slash."""
    try:
        return folder.relative_to(project).as_posix() + "/"
    except ValueError:
        return str(folder) + "/"


def has_files(stored: Path) -> bool:
    """Whether a run's snapshot (`RunPaths.experiment`) holds a file: an empty one (a
    folder with only its config) is kept for `resume` to compare, and otherwise treated
    as no folder (no copy, no `$AUTOHELIX_EXPERIMENT`, no prompt line)."""
    return os.path.isdir(stored) and not os.path.islink(stored) and any(
        files for _, _, files in os.walk(stored))


def snapshot_files(config: Config, project: Path) -> list[SnapshotFile]:
    """What the run stores of the config's experiment folder (`Config.experiment`), by name.

    Left out: the config file and the prompt template (stored already), dotfiles and
    dot-folders, `__pycache__/`. A link is followed when its target is inside the
    project's autohelix/ (`lib -> ../lib`) and not under a dot-name there
    (`autohelix/.git/`); a committed project file is named by its path instead. Raises
    ConfigError, naming the files, for other links (or links to nothing), a folder
    reached twice (a link to one that holds it, or a second way to one stored already),
    files that aren't regular (a FIFO, a socket) or can't be read, and, as soon as the
    walk passes it, the cap (`MAX_FILES`, `MAX_FOLDERS`, `MAX_BYTES`; `MAX_FILES`
    refusals stop it too). The folder itself must be inside autohelix/, not a link out."""
    folder = config.experiment
    project = Path(project)
    root, allowed = os.path.realpath(folder), Path(os.path.realpath(project / CONFIG_DIR))
    left_out = {os.path.realpath(folder / CONFIG_FILE)}
    if config.prompt_template is not None:
        left_out.add(os.path.realpath(config.prompt_template))
    where = shown(project, folder)

    def refuse(refused: list[str], stopped: bool = False) -> ConfigError:
        more = len(refused) - MAX_REFUSED
        return ConfigError(f"{where} can't be stored with the run: " + "; ".join(refused[:MAX_REFUSED])
                           + (" and more" if stopped else f" and {more} more" if more > 0 else "") + ". "
                           f"Move these out of it, or link only to files inside {CONFIG_DIR}/ (name a project "
                           "file by its path in a command instead).")

    # The folder itself (a link, `config._experiments` accepts one) follows the links' rule.
    pointed = os.readlink(folder) if os.path.islink(folder) else root
    place = None
    if Path(root) == allowed:
        place = f"{CONFIG_DIR}/ itself"
    elif not Path(root).is_relative_to(allowed):
        place = f"outside {CONFIG_DIR}/"
    elif hidden := next((part for part in Path(root).relative_to(allowed).parts if part.startswith(".")), None):
        place = f"into {hidden}"
    if place is not None:
        raise ConfigError(f"{where} can't be stored with the run: it is a link to {pointed} ({place}). An "
                          f"experiment must be a folder inside {CONFIG_DIR}/, not a link out of it.")
    found: list[SnapshotFile] = []
    refused: list[str] = []
    visited = {root: ""}  # each folder walked, by real path: its name in the snapshot
    total = 0

    def too_large(what: str) -> ConfigError:
        largest = sorted(found, key=lambda file: (-file.size, file.name))[:5]
        return ConfigError(
            f"{where} is too large to store with the run: {what} (at most {MAX_FILES} files, "
            f"{MAX_FOLDERS} folders and {MAX_BYTES // (1024 * 1024)} MB)."
            + (" Largest: " + ", ".join(f"{file.name} ({_size(file.size)})" for file in largest) + "."
               if largest else "")
            + " Keep datasets out of it: name them by path (in the goal or a command) instead.")

    def visit(directory: Path, prefix: str) -> None:
        nonlocal total
        try:
            with os.scandir(directory) as scan:
                entries = sorted(scan, key=lambda e: e.name)
        except OSError as e:
            refused.append(f"{prefix or 'the folder itself'} (can't be read: {e.strerror or e})")
            return
        for entry in entries:
            if len(refused) >= MAX_FILES:
                raise _Stopped
            if entry.name.startswith("."):
                continue
            name = prefix + entry.name
            path = Path(entry.path)
            try:
                target = os.path.realpath(path)
                if target in left_out:
                    continue
                link = entry.is_symlink()
                if link:
                    pointed = os.readlink(path)
                    if not Path(target).is_relative_to(allowed):
                        refused.append(f"{name} (a link to {pointed}, outside {CONFIG_DIR}/)")
                        continue
                    hidden = next((part for part in Path(target).relative_to(allowed).parts
                                   if part.startswith(".")), None)
                    if hidden is not None:  # autohelix/.git/ (its config may hold credentials), .env, ...
                        refused.append(f"{name} (a link to {pointed}, into {hidden})")
                        continue
                    if not os.path.exists(target):
                        refused.append(f"{name} (a link to {pointed}, which doesn't exist)")
                        continue
                info = os.stat(target)
                directory_entry = stat.S_ISDIR(info.st_mode)
                if directory_entry and entry.name in SKIPPED_FOLDERS:
                    continue
                if directory_entry:
                    if target == root or root.startswith(target + os.sep):  # autohelix/, or the folder
                        through = f"a link to {pointed}, " if link else ""
                        refused.append(f"{name} ({through}a folder holding {where})")
                        continue
                    if target in visited:
                        first = visited[target]
                        if name.startswith(first + "/"):
                            refused.append(f"{name} ({'a link to ' if link else ''}a folder that contains it)")
                        else:
                            refused.append(f"{name} (the same folder as {first}/, stored already)")
                        continue
                    visited[target] = name
                    if len(visited) - 1 > MAX_FOLDERS:
                        raise too_large(f"over {MAX_FOLDERS} folders")
                    visit(path, name + "/")
                elif stat.S_ISREG(info.st_mode):
                    if not os.access(target, os.R_OK):
                        refused.append(f"{name} (can't be read: {os.strerror(errno.EACCES)})")
                        continue
                    found.append(SnapshotFile(name, Path(target), info.st_size, stat.S_IMODE(info.st_mode)))
                    total += info.st_size
                    if len(found) > MAX_FILES:
                        raise too_large(f"over {MAX_FILES} files")
                    if total > MAX_BYTES:
                        raise too_large(f"over {MAX_BYTES // (1024 * 1024)} MB")
                else:
                    refused.append(f"{name} (not a regular file)")
            except OSError as e:  # vanished meanwhile, a name too long, ...
                refused.append(f"{name} (can't be read: {e.strerror or e})")

    try:
        visit(folder, "")
    except _Stopped:
        raise refuse(refused, stopped=True) from None
    if refused:
        raise refuse(refused)
    return found


class _Stopped(Exception):
    """`snapshot_files`' walk stopping at `MAX_FILES` refusals."""


STAGING_PREFIX = ".experiment-"


def build_snapshot(files: list[SnapshotFile], beside: Path) -> Path:
    """Copy `files` into a new folder in `beside`, named with a leading dot (no run
    listing reads it), and return it for the caller to rename into place. Files lose
    their write bits and keep the others (a private file stays private); folders stay
    writable by their owner, so the snapshot can be deleted. On failure, nothing is
    left; a file that can't be read now, or whose size differs from the walk's, is a
    ConfigError naming it."""
    beside.mkdir(parents=True, exist_ok=True)
    staging = beside / f"{STAGING_PREFIX}{secrets.token_hex(4)}"
    staging.mkdir(mode=0o755)
    try:
        for file in files:
            destination = staging / file.name
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file.source, destination)
                size = destination.stat().st_size
                os.chmod(destination, file.mode & 0o555)
            except OSError as e:  # removed or made unreadable since the walk, a full disk
                raise ConfigError(f"The experiment folder's {file.name} can't be stored with the run: "
                                  f"{e.strerror or e}.") from None
            if size != file.size:
                raise ConfigError(f"The experiment folder's {file.name} changed while being stored; "
                                  "start the run again.")
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def sweep_staging(beside: Path) -> None:
    """Remove the copies `build_snapshot` left in `beside` when its process was killed
    before the rename. Only for a caller that holds the lock every build is made under."""
    for path in beside.glob(f"{STAGING_PREFIX}*"):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)


def snapshot_changes(config: Config, project: Path, stored: Path) -> tuple[str, set[str]] | None:
    """How the live experiment folder differs from a run's snapshot (`stored`): words
    naming the files changed (bytes or exec bit), added and removed, or why the folder
    can no longer be stored (a file that can't be read too), and which of `changed`,
    `added`, `removed` there are (none for a folder that can't be stored); None when
    they match. Compared file by file: the snapshot is the manifest."""
    try:
        live = {file.name: file for file in snapshot_files(config, project)}
    except ConfigError as e:
        return f"it can no longer be stored: {e}", set()
    kept = {path.relative_to(stored).as_posix(): path for path in stored.rglob("*")
            if path.is_file() and not path.is_symlink()}
    changed = []
    for name in sorted(live.keys() & kept.keys()):
        file, copy = live[name], kept[name]
        try:
            if (file.executable != bool(copy.stat().st_mode & stat.S_IXUSR)
                    or file.size != copy.stat().st_size or file.source.read_bytes() != copy.read_bytes()):
                changed.append(name)
        except OSError as e:  # gone or unreadable since the walk
            return f"it can no longer be stored: {e.filename or name} can't be read ({e.strerror or e})", set()
    found = {label: names for label, names in (
        ("changed", changed), ("added", sorted(live.keys() - kept.keys())),
        ("removed", sorted(kept.keys() - live.keys()))) if names}
    if not found:
        return None
    return "; ".join(f"{label}: {', '.join(names)}" for label, names in found.items()), set(found)
