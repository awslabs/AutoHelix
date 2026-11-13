# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Opening run-state files that agents or candidate code may have tampered with.

Every component below a trusted folder is opened relative to the one before,
without following symlinks; files must be regular (never a FIFO or device),
and with `single_link` have no other hard links (one planted to point at a
file elsewhere on the same disk)."""

import os
import stat
from pathlib import Path

_OPEN = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def bad_parts(parts) -> bool:
    return any(not isinstance(part, str) or part in ("", ".", "..") or "/" in part or "\0" in part
               for part in parts)


def contained(root: Path, parts) -> list[str] | None:
    """`parts` as the components of a normalized path that stays below `root`, or None.
    `bad_parts` already refuses anything that could leave `root`; this is the same
    check in the normalize-then-prefix form static analysis recognizes."""
    if bad_parts(parts):
        return None
    base = os.path.normpath(str(root))
    full = os.path.normpath(os.path.join(base, *parts))
    if full != base and not full.startswith(base + os.sep):
        return None
    relative = os.path.relpath(full, base)
    return [] if relative == "." else relative.split(os.sep)


def open_dir(root: Path, parts) -> int | None:
    """A descriptor for the folder `root/parts...`, or None. `root`'s last component and
    every part below it are opened without following symlinks, each relative to the
    one before: no `..`, no absolute paths, nothing outside `root`."""
    parts = contained(root, parts)
    if parts is None:
        return None
    fd = None
    try:
        fd = os.open(root, _OPEN | os.O_DIRECTORY)
        for part in parts:
            child = os.open(part, _OPEN | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError:
        if fd is not None:
            os.close(fd)
        return None


def open_file(root: Path, parts, single_link: bool = False) -> int | None:
    """A read descriptor for the regular file `root/parts...` (opened as `open_dir`
    opens folders), or None: never a symlink, FIFO or device, and with `single_link`
    never a file with other hard links (one planted to point at a file elsewhere)."""
    parts = contained(root, parts) if parts else None
    if not parts:
        return None
    parent = open_dir(root, parts[:-1])
    if parent is None:
        return None
    try:
        fd = os.open(parts[-1], _OPEN | os.O_NONBLOCK, dir_fd=parent)
    except OSError:
        return None
    finally:
        os.close(parent)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or (single_link and info.st_nlink != 1):
        os.close(fd)
        return None
    return fd


def open_run_log(paths, value) -> int | None:
    """A read descriptor for a run log named by untrusted metadata (a history
    record's `calls[].log`, the status file's `log`), or None: only a regular,
    single-link file below the run's `logs/` folder or an iteration's
    `iterations/NNN/agent.log` (`RunPaths.relative_log`), reached without symlinks."""
    relative = paths.relative_log(value)
    if not relative:
        return None
    return open_file(paths.run_dir, Path(relative).parts, single_link=True)
