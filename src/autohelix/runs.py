# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run identity, file layout, ownership, recovery, and fast-forward landing."""

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NamedTuple
from pathlib import Path

from autohelix.config import (ENTRY_NAME, config_name, config_relative, load_config, not_an_experiment, project_configs,
                              run_command, template_text)
from autohelix.experiment import build_snapshot, snapshot_files, sweep_staging

SCHEMA = 2
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,50}\Z")  # YYYYMMDD-HHMM-<name>-xxxx
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}\Z")
COMMIT = re.compile(r"[0-9a-f]{7,64}")  # a commit id as run files hold it: never an option or a rev expression
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
COPIES_ENV = "AUTOHELIX_COPIES_ROOT"
PROJECT_RECORD = "project.json"  # in each project's copies folder: which checkout it serves


def short_id(run_id: str) -> str:
    """The random suffix: what messages print and every command accepts."""
    return run_id.rsplit("-", 1)[-1]


def git(project: Path, *args: str, check: bool = True):
    from autohelix.workspace import run_git

    result = run_git(
        ["git", "-c", "gc.auto=0", *args], cwd=project, capture_output=True,
        text=True, start_new_session=True,
    )
    if check and result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
    return result


def write_text(path: Path, text: str) -> None:
    """Replace a file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text)
    temporary.replace(path)


def write_json(path: Path, data: dict) -> None:
    write_text(path, json.dumps(data) + "\n")


_ITERATION = re.compile(r"[0-9]{3,}")
_ATTEMPT = re.compile(r"[0-9]{3,}-[1-9][0-9]*")
_POSITION = re.compile(r"[1-9][0-9]*")
_TRANSCRIPT = re.compile(r"(ask|review)(-[0-9]+)?\.log")  # `judge._log_path`'s names
# A command call's place in its iteration folder (`$AUTOHELIX_CALL`): `gates/<k>`, `stop/<k>`, `observe/<name>`.
_CALL = re.compile(rf"(?:gates|stop)/[1-9][0-9]*|observe/{ENTRY_NAME.pattern}")
_CALL_FOLDER = re.compile(rf"iter-(0|[1-9][0-9]*)-((?:gates|stop)-[1-9][0-9]*|observe-{ENTRY_NAME.pattern})")


def is_transcript(name: str) -> bool:
    """A judge or review transcript's file name: `ask.log`, `ask-2.log`, `review.log`, ..."""
    return bool(_TRANSCRIPT.fullmatch(name))


def _at(parts: list, index: int) -> str:
    return parts[index] if index < len(parts) else ""


@dataclass(frozen=True)
class RunPaths:
    """The sole definition of project and per-run state paths.

    Without an id, only the project's paths (`root`, `runs`, `locks`, ...) mean
    anything; which run a command acts on is `default_run`'s choice, never a pointer.

    A run's folder holds what people read (run.json, config.yaml, experiment/, history.jsonl, hints.md,
    report.md/.html, the `notes` link, `iterations/NNN/`, `logs/`); everything the
    harness keeps for itself is in its `.internal/`, and the project's own
    (locks, helper scripts) in `.autohelix/.internal/`.
    """

    project: Path
    run_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "project", Path(self.project).resolve())
        if self.run_id is not None and not _ID.fullmatch(self.run_id):
            raise RuntimeError(f"Invalid run id: {self.run_id!r}")

    # The project's state.
    @property
    def root(self): return self.project / ".autohelix"
    @property
    def project_internal(self): return self.root / ".internal"
    @property
    def locks(self): return self.project_internal / "locks"
    @property
    def selection_lock(self): return self.locks / "selection.lock"
    @property
    def bin(self): return self.project_internal / "bin"  # helper scripts (time left, notes check)
    @property
    def runs(self): return self.root / "runs"

    # One run's: what people read.
    @property
    def run_dir(self): return self.runs / self.run_id if self.run_id else self.root
    @property
    def identity(self): return self.run_dir / "run.json"
    @property
    def config(self): return self.run_dir / "config.yaml"  # the run's config, stored when it started
    @property
    def template(self): return self.run_dir / "prompt_template.md"  # its prompt_template's text, if it has one
    @property
    def experiment(self): return self.run_dir / "experiment"  # its experiment folder's copy, stored when it started
    @property
    def history(self): return self.run_dir / "history.jsonl"
    @property
    def hints(self): return self.run_dir / "hints.md"
    @property
    def report(self): return self.run_dir / "report.md"
    @property
    def report_html(self): return self.run_dir / "report.html"
    @property
    def notes(self): return self.run_dir / "notes"  # a link to the current version
    @property
    def iterations(self): return self.run_dir / "iterations"
    @property
    def logs(self): return self.run_dir / "logs"  # run logs per segment, report.log

    # One run's internal files.
    @property
    def internal(self): return self.run_dir / ".internal"
    @property
    def status(self): return self.internal / "status.json"
    @property
    def segments(self): return self.internal / "segments.jsonl"  # the run's event log (`run_status.SegmentLog`)
    @property
    def stop_request(self): return self.internal / "stop.json"  # `autohelix stop`'s request (`run_status`)
    @property
    def metrics(self): return self.internal / "metrics.json"  # `$AUTOHELIX_METRICS` for stop commands
    @property
    def claude_settings(self): return self.internal / "claude_settings.json"
    @property
    def copies_record(self): return self.internal / "copies"  # the copies base the last segment used
    @property
    def notes_versions(self): return self.internal / "notes"  # notes/<N>/, one per save
    @property
    def report_info(self): return self.internal / "report.json"
    @property
    def report_draft(self): return self.internal / "report.draft.md"
    @property
    def attempts(self): return self.internal / "attempts"  # superseded iteration folders

    @property
    def summary_usage(self): return self.internal / "usage" / "summary.jsonl"  # every summary call's usage

    def usage(self, iteration: int) -> Path:
        """`$AUTOHELIX_USAGE_FILE` for the commands one iteration runs."""
        return self.internal / "usage" / f"{self.iteration_name(iteration)}.jsonl"

    # One iteration's folder: these names in it.
    RECORD, PROMPT, AGENT_LOG, NOTES = "record.json", "prompt.md", "agent.log", "notes"
    GATES, OBSERVE, STOP, REJECTED_PATCH = "gates", "observe", "stop", "rejected.patch"

    @staticmethod
    def iteration_name(iteration: int) -> str:
        """`000`, `001`, ...: three digits, so listings sort in order (past 999 the width grows)."""
        return f"{iteration:03d}"

    def iteration_dir(self, iteration: int) -> Path:
        return self.iterations / self.iteration_name(iteration)

    def record(self, iteration: int) -> Path:
        """The iteration's record, pretty-printed: derived from history.jsonl, written last."""
        return self.iteration_dir(iteration) / self.RECORD

    def prompt(self, iteration: int) -> Path:
        return self.iteration_dir(iteration) / self.PROMPT

    def agent_log(self, iteration: int) -> Path:
        return self.iteration_dir(iteration) / self.AGENT_LOG

    def iteration_notes(self, iteration: int) -> Path:
        """A link to the notes version saved with the iteration, if one was."""
        return self.iteration_dir(iteration) / self.NOTES

    def observe_dir(self, iteration: int) -> Path:
        return self.iteration_dir(iteration) / self.OBSERVE

    def observe_entry(self, iteration: int, name: str) -> Path:
        return self.observe_dir(iteration) / name

    def gate_dir(self, iteration: int, position: int) -> Path:
        """The output of the gate at 1-based `position` in `gates:`."""
        return self.iteration_dir(iteration) / self.GATES / str(position)

    def stop_dir(self, iteration: int, position: int) -> Path:
        """The output of the stop command at 1-based `position` in `stop:`."""
        return self.iteration_dir(iteration) / self.STOP / str(position)

    def rejected_patch(self, iteration: int) -> Path:
        """A rejected attempt's change (`base..tree`), when it had one under the size limit."""
        return self.iteration_dir(iteration) / self.REJECTED_PATCH

    def attempt_dir(self, iteration: int, k: int) -> Path:
        """Where the k-th superseded attempt at an iteration's folder is kept."""
        return self.attempts / f"{self.iteration_name(iteration)}-{k}"

    def parts(self, path: Path) -> list[str]:
        """`path`'s components below the run folder (for descriptor-based reads)."""
        return list(path.relative_to(self.run_dir).parts)

    @staticmethod
    def call_folder_name(iteration: int, call: str) -> str:
        """`iter-N-gates-2`, `iter-N-observe-bench`, `iter-N-stop-1`: the folder, in the
        copies folder, of the command whose output goes to `call` (`gates/2`, ...) of
        iteration N."""
        return f"iter-{iteration}-{call.replace('/', '-')}"

    def usage_log(self, iteration: int, usage: dict):
        """Where the transcript of a usage record of `iteration` is, as the `log` value
        readers take (`relative_log`, `live_transcript`). A record with a `call` (an
        `ask`/`review` in a command the harness ran) names its transcript by file name in
        that call's folder: the stored one (`iterations/NNN/<call>/<log>`, or in the
        earlier attempt `read_usage` puts it in, `.internal/attempts/NNN-k/...`) if it is
        there, else the call folder's `logs/` it is being written to (`live_transcript`,
        while the iteration runs). Any other record's `log` is passed on as it is."""
        call, log = usage.get("call"), usage.get("log")
        if call is None:
            return log
        if not (isinstance(call, str) and _CALL.fullmatch(call) and isinstance(log, str) and is_transcript(log)):
            return None
        attempt = usage.get("attempt")
        if attempt is not None:  # an earlier attempt's call
            folder = self.attempts / attempt if (isinstance(attempt, str) and _ATTEMPT.fullmatch(attempt)
                                                 and attempt.rsplit("-", 1)[0] == self.iteration_name(iteration)
                                                 ) else None
        else:
            folder = self.iteration_dir(iteration)
        stored = folder / call / log if folder else None
        if stored is not None and os.path.lexists(stored):
            return str(stored.relative_to(self.run_dir))
        if attempt is not None:
            return None
        try:
            return str(self.copies / self.call_folder_name(iteration, call) / "logs" / log)
        except RuntimeError:  # no copies folder recorded
            return None

    def copies_under(self, base: Path) -> Path:
        return base / project_key(self.project) / self.run_id

    @property
    def copies(self) -> Path:
        """The run's copies folder, outside the project: `<base>/<dirname>-<hash8>/<id>`,
        under the base the last segment recorded, else the current one. Only observers
        read it; a segment uses the folder `claim_copies` returned."""
        if not self.run_id:
            raise RuntimeError("No run selected.")
        try:
            base = _checked_base(self.copies_record.read_text().strip())
        except (OSError, RuntimeError):
            base = copies_base()
        return self.copies_under(base)

    @property
    def ref(self): return f"refs/autohelix/{self.run_id}"
    @property
    def pin(self): return f"refs/autohelix-tmp/{self.run_id}"  # the frozen candidate tree
    # Rejected attempts' trees, one ref per iteration; not in copies, removed by delete.
    @property
    def rejected_refs(self): return f"refs/autohelix-rejected/{self.run_id}/"
    def rejected_ref(self, iteration: int) -> str: return f"{self.rejected_refs}{iteration}"
    @property
    def lock(self): return self.locks / f"{self.run_id}.lock"

    def relative_log(self, value) -> str | None:
        """Normalize untrusted log metadata, retaining usage without unsafe logs: a
        path inside the run folder (resolved, so no symlink leads out of it) that is
        below `logs/`, or an iteration's agent log or judge/review transcript
        (`is_iteration_log`). Returns it relative to the run folder, else None."""
        if not self.run_id or not isinstance(value, (str, Path)) or not value:
            return None
        try:
            path = Path(value)
            path = (path if path.is_absolute() else self.run_dir / path).resolve()
            root = self.run_dir.resolve()
            if not path.is_relative_to(root):
                return None
            relative = path.relative_to(root)
            logs = self.logs.resolve()
            if logs.is_relative_to(root) and path.is_relative_to(logs):
                return str(relative)
            return str(relative) if self.is_iteration_log(relative.parts) else None
        except (OSError, ValueError, RuntimeError):
            return None

    def is_iteration_log(self, parts) -> bool:
        """Whether `parts` (below the run folder) name a log an iteration keeps:
        `iterations/NNN/agent.log`, a judge or review transcript (`ask.log`, `ask-2.log`,
        `review.log`, ...) in `iterations/NNN/gates/<k>/`, `stop/<k>/` or
        `observe/<name>/`, or the same in a superseded attempt,
        `.internal/attempts/NNN-<k>/`. Nothing else (no stdout.txt, prompt.md or
        record.json)."""
        parts = list(parts)
        iterations, attempts = self.parts(self.iterations), self.parts(self.attempts)
        if parts[:len(iterations)] == iterations and _ITERATION.fullmatch(_at(parts, len(iterations))):
            rest = parts[len(iterations) + 1:]
        elif parts[:len(attempts)] == attempts and _ATTEMPT.fullmatch(_at(parts, len(attempts))):
            rest = parts[len(attempts) + 1:]
        else:
            return False
        if rest == [self.AGENT_LOG]:
            return True
        if len(rest) != 3 or not is_transcript(rest[2]):
            return False
        kind, name = rest[0], rest[1]
        if kind in (self.GATES, self.STOP):
            return bool(_POSITION.fullmatch(name))
        return kind == self.OBSERVE and not name.startswith(".")

    def live_transcript(self, value, running: int | None) -> list[str] | None:
        """A judge or review transcript a running call is writing, named by untrusted
        metadata (`usage_log` before its command ended): an absolute path
        `<copies>/iter-N-<call>/logs/<ask|review>(-n).log`, N being the `running`
        iteration (from the run status). Returns its parts below `copies`, else None;
        no other name, folder or iteration."""
        if not self.run_id or running is None or not isinstance(value, str) or not os.path.isabs(value):
            return None
        try:
            copies = str(self.copies)
        except RuntimeError:
            return None
        path = os.path.normpath(value)
        if not path.startswith(copies + os.sep):
            return None
        parts = path[len(copies) + 1:].split(os.sep)
        if len(parts) != 3 or parts[1] != "logs" or not is_transcript(parts[2]):
            return None
        match = _CALL_FOLDER.fullmatch(parts[0])
        return parts if match and int(match[1]) == running else None

    def readable_log(self, value, running: int | None = None) -> tuple[Path, list[str]] | None:
        """Where a log named by untrusted metadata may be opened, as (folder, parts below
        it) for descriptor-based reads: a run log (`relative_log`) below the run folder,
        or, with `running`, that iteration's live transcript (`live_transcript`) below the
        copies folder. None for anything else."""
        if relative := self.relative_log(value):
            return self.run_dir, list(Path(relative).parts)
        if parts := self.live_transcript(value, running):
            return self.copies, parts
        return None

    def read_identity(self) -> dict:
        try:
            data = json.loads(self.identity.read_text())
        except (OSError, ValueError) as e:
            raise RuntimeError(f"Could not read run {self.run_id}: {e}") from e
        if data.get("schema") != SCHEMA or data.get("id") != self.run_id:
            raise RuntimeError(f"Unsupported or inconsistent run identity: {self.identity}")
        return data


def remove_planted(path: Path) -> bool:
    """Remove anything at `path` that isn't a real folder (a symlink, file, FIFO,
    socket or device someone planted there), never following it. Returns whether
    something was removed."""
    if path.is_symlink() or os.path.lexists(path) and not path.is_dir():
        path.unlink()
        return True
    return False


def iteration_folder(paths: RunPaths, iteration: int) -> Path:
    """`iterations/NNN/`, created as a real folder: anything else planted at it (or at
    `iterations/`) is removed, never written through."""
    for folder in (paths.iterations, paths.iteration_dir(iteration)):
        remove_planted(folder)
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def _checked_base(value: str) -> Path:
    if not os.path.isabs(value) or os.path.realpath(value) != value or os.pathsep in value:
        raise RuntimeError(f"Copies base must be an absolute, resolved path without {os.pathsep!r}: {value}")
    return Path(value)


def copies_base() -> Path:
    """Where candidate copies live: ${AUTOHELIX_COPIES_ROOT}, else
    ${XDG_CACHE_HOME:-~/.cache}/autohelix/copies, resolved."""
    value = os.path.expanduser(os.environ.get(COPIES_ENV, ""))
    if value and not os.path.isabs(value):
        raise RuntimeError(f"{COPIES_ENV} must be an absolute path: {value}")
    if not value:
        cache = os.environ.get("XDG_CACHE_HOME", "")
        if not os.path.isabs(cache):  # the XDG spec ignores relative values
            cache = os.path.join(os.path.expanduser("~"), ".cache")
        value = os.path.join(cache, "autohelix", "copies")
    return _checked_base(os.path.realpath(value))


def project_key(project: Path) -> str:
    """`<dirname>-<hash8>` naming a project's folder under the copies base."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", project.name)[:40].lstrip(".") or "project"
    return f"{name}-{hashlib.sha256(os.fsencode(project)).hexdigest()[:8]}"


def _check_base(base: Path) -> None:
    """The base may be shared (like /tmp) only if others can't rename entries in it."""
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = base.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (os.getuid(), 0)
            or info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
        raise RuntimeError(f"Copies base must be owned by you or root and not writable by others: "
                           f"{base}. Point {COPIES_ENV} at a private folder.")


def _own_dir(path: Path, *, create: bool) -> None:
    """A real directory owned by this user, made private (mode 0700)."""
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"Copies folder is not a directory owned by you: {path}")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)


def ensure_copies(root: Path) -> Path:
    """Create a run's copies folder and its project folder on first use: real
    directories owned by this user, mode 0700."""
    def make():
        _check_base(root.parent.parent)
        _own_dir(root.parent, create=True)
        _own_dir(root, create=True)
        return root

    try:
        return make()
    except FileNotFoundError:  # the sweep of another checkout removed the project's folder
        return make()


def remove_copies(root: Path, warn=None) -> None:
    """Delete a run's copies folder (the project's folder and its record stay, for
    the sweep). Errors raise unless `warn` is given."""
    from autohelix.workspace import _check_root, remove_copy

    try:
        if not os.path.lexists(root):
            return
        _own_dir(root, create=False)  # never empty a folder that isn't ours
        _check_root(root)
        for entry in root.iterdir():
            remove_copy(entry)
    except Exception as e:  # noqa: BLE001 - cleanup reports, the caller decides
        if warn is None:
            raise
        warn(f"Could not remove copies in {root}: {e}")
        return
    try:
        root.rmdir()
    except OSError:
        pass


def sweep_copies(project: Path) -> None:
    """Remove copies left by processes that died before their cleanup (SIGKILL, a
    crash): this project's runs whose lock is free, and other projects' folders on
    this host whose checkout is gone for over a day (a shared base may be on NFS or
    in a container, where a missing path can be temporary). Best effort."""
    base = copies_base()
    if not base.is_dir():
        return
    own = base / project_key(project)
    if own.is_dir():
        for entry in own.iterdir():
            if not _ID.fullmatch(entry.name) or not entry.is_dir():
                continue
            try:
                lock = RunLock(RunPaths(project, entry.name).lock, retries=())
            except RuntimeError:  # live (a probe's moment aside: swept next time)
                continue
            with lock:
                remove_copies(entry, warn=lambda message: None)
    host = socket.gethostname()
    for folder in base.iterdir():
        record = folder / PROJECT_RECORD
        try:
            data = json.loads(record.read_text())
            old = time.time() - record.stat().st_mtime > 86400
        except (OSError, ValueError):
            continue
        if folder == own or data.get("host") != host or not old or os.path.lexists(data.get("path") or "/"):
            continue
        try:
            _own_dir(folder, create=False)
            for entry in folder.iterdir():
                if entry.is_dir():
                    remove_copies(entry)
            if [entry.name for entry in folder.iterdir()] == [PROJECT_RECORD]:  # else keep the record
                record.unlink()
                folder.rmdir()
        except Exception:  # noqa: BLE001 - someone else's folder, or in use: leave it
            continue


def claim_copies(paths: "RunPaths", warn=None) -> Path:
    """Fix the copies folder this process uses for the run (call under the run lock):
    record its base for observers and stop git discovery there, so a process that
    deletes its copy's .git can't climb into an enclosing repository. The folder
    itself is created on first use (`ensure_copies`)."""
    base = copies_base()
    _check_base(base)
    root = paths.copies_under(base)
    previous = paths.copies
    if previous != root:  # the base changed since the last segment
        remove_copies(previous, warn)
    # The sweep removes other projects' folders by this record once their checkout is gone.
    _own_dir(root.parent, create=True)
    # Rewritten each segment: its age is the time since the project was last used here.
    write_json(root.parent / PROJECT_RECORD, {"path": str(paths.project), "host": socket.gethostname()})
    write_text(paths.copies_record, f"{base}\n")
    ceiling = os.environ.get("GIT_CEILING_DIRECTORIES", "")
    if str(root) not in ceiling.split(os.pathsep):
        os.environ["GIT_CEILING_DIRECTORIES"] = str(root) + (os.pathsep + ceiling if ceiling else "")
    return root


_HELD: set[str] = set()  # the run locks this process holds (`held_here`)


def held_here(path: Path) -> bool:
    """Whether this process holds the run lock at `path` (a `RunLock`)."""
    return os.path.realpath(path) in _HELD


LOCK_RETRIES = (0.01, 0.02, 0.04, 0.08)  # seconds between nonblocking tries: outlast an `is_live` probe


class RunLock:
    """Nonblocking, process-held ownership; the persistent inode is never removed. A
    held lock is tried again for a moment (`LOCK_RETRIES`): an observer's `is_live`
    probe takes a free lock for microseconds. `retries=()` tries once (a sweep that
    skips running runs)."""

    def __init__(self, path: Path, *, blocking=False, retries=LOCK_RETRIES):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a+")
        for pause in (*retries, None):
            try:
                fcntl.flock(self._file, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                break
            except BlockingIOError:
                if pause is None:
                    self._file.close()
                    raise RuntimeError(f"Run {short_id(path.stem)} is running.") from None
                time.sleep(pause)
        self._key = os.path.realpath(path)
        _HELD.add(self._key)

    def close(self):
        if not self._file.closed:
            _HELD.discard(self._key)
            fcntl.flock(self._file, fcntl.LOCK_UN)
            self._file.close()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()
    def __del__(self):
        if hasattr(self, "_file"):
            self.close()


@dataclass
class RunState:
    paths: RunPaths
    data: dict
    expected_tip: str
    copies_root: Path | None = None  # from claim_copies


@contextmanager
def critical_section():
    """Defer Ctrl-C and SIGTERM (`autohelix stop --now`, `kill`) until the commit, ref
    update, and durable record agree; then the first one acts as it would have. An
    ignored signal stays ignored (a background run's SIGINT)."""
    pending = []
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            handler = signal.getsignal(signum)
            if handler != signal.SIG_IGN:
                previous[signum] = handler
                signal.signal(signum, lambda *args: pending.append(args))
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        for signum, handler in previous.items():
            # None: a handler not installed from Python; restored as the default.
            signal.signal(signum, signal.SIG_DFL if handler is None else handler)
        if pending and not failed:
            signum, frame = pending[0]
            handler = previous[signum]
            if callable(handler):
                handler(signum, frame)
            elif signum == signal.SIGINT:
                raise KeyboardInterrupt
            else:
                signal.raise_signal(signum)  # the default action, now that it is restored


class NoRunToResume(RuntimeError):
    """A bare `resume` found nothing to continue."""


def is_archived(data: dict) -> bool:
    return bool(data.get("archived"))


def is_live(paths: RunPaths) -> bool:
    """Whether a process holds the run's lock (a brief probe: advisory). A run that never
    ran has no lock file, and the probe doesn't create one; it opens the file read-only
    (observers need no write access) and takes the lock only when nobody holds it. A
    lock that can't be opened counts as held."""
    try:
        fd = os.open(paths.lock, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:  # BlockingIOError: held
        return True
    finally:
        os.close(fd)  # releases the probe's lock
    return False


def last_activity(paths: RunPaths, data: dict, status: dict | None = None) -> float | None:
    """When the run last did something: created, recorded an iteration, or reported status."""
    if status is None:
        try:
            status = json.loads(paths.status.read_text())
        except (OSError, ValueError):
            status = {}
    try:
        history = paths.history.stat().st_mtime
    except OSError:
        history = None
    times = [data.get("created_at"), history, (status or {}).get("updated_at")]
    return max((t for t in times if isinstance(t, (int, float))), default=None)


def check_group(name: str) -> str:
    """A group name as typed; '' clears a run's group."""
    if name and not _NAME.fullmatch(name):
        raise RuntimeError("group must use lowercase letters, digits or hyphens, with at most 32 characters.")
    return name


def group_members(paths: RunPaths, data: dict) -> list[tuple[RunPaths, dict]]:
    """The other non-archived runs in the project with this run's group, most recently active first."""
    group = data.get("group")
    if not group:
        return []
    return [(p, d) for p, d in project_runs(paths.project)
            if p.run_id != paths.run_id and d.get("group") == group and not is_archived(d)]


def imported_run(paths: RunPaths, data: dict) -> tuple[RunPaths, dict] | None:
    """The run this one imports (`run --import`, run.json's `import.run`), or None
    without one or when it can't be read (deleted, say): another run's state must not
    stop this run."""
    source = data.get("import")
    source = source.get("run") if isinstance(source, dict) else None
    if not isinstance(source, str) or not _ID.fullmatch(source):  # a real id
        return None
    try:
        other = RunPaths(paths.project, source)
        if other.run_id == paths.run_id:
            return None
        return other, other.read_identity()
    except RuntimeError:
        return None


WHY_LIMIT = 300  # characters of a `--why` note kept


def check_why(text: str) -> str:
    """A `--why` note as stored: one clean line (`formatting.clean`), at most WHY_LIMIT characters."""
    from autohelix.formatting import clean

    why = clean(text)
    if not why:
        raise RuntimeError("--why needs some text.")
    if len(why) > WHY_LIMIT:
        raise RuntimeError(f"--why is {len(why)} characters; use at most {WHY_LIMIT} (one line).")
    return why


def has_baseline(paths: RunPaths) -> bool:
    from autohelix.history import History

    return any(r.iteration == 0 and r.accepted for r in History(paths.project, paths.run_id).load())


@dataclass(frozen=True)
class Landing:
    """Where a run's tip stands against its target branch (`landing`).

    `relation`: `same` (the target is at the tip), `ahead` (the run has commits the
    target lacks and the target can fast-forward onto them), `behind` (the tip is in the
    target, which has more; also a tip with no commits of its own the target lacks),
    `diverged` (each has commits the other lacks), `landed` (the target lacks the tip, but
    it is the one `autohelix land` last landed on this same branch, run.json's `landed`, as
    after a squash-merged or rebased PR; the code isn't compared), or None when
    a side is missing (no ref; no target branch: started detached, or it is gone).
    Commits accepted after a recorded landing are `diverged`, and `ahead` counts only them.
    `ahead`: the run's commits beyond its base that the target lacks, the unlanded ones
    (all of them without a target branch; squash-merged or cherry-picked work counts,
    unless `autohelix land` recorded it as landed on this branch);
    `behind`: the target's commits the tip lacks;
    `accepted`: whether the run accepted a commit (its tip isn't its base);
    `since`: the recorded landed tip, which the target lacks, that the relation counts
    from (`landed`, or `diverged` by the commits accepted after it), else None: those
    commits apply with `git cherry-pick <since>..autohelix/<id>` (a merge would re-apply
    the squashed ones)."""

    tip: str | None
    target: str | None
    target_tip: str | None
    relation: str | None
    ahead: int = 0
    behind: int = 0
    accepted: bool = False
    since: str | None = None

    def payload(self) -> dict | None:
        """`view --json`'s `landing` (None without a relation): `commits` is `ahead`, and
        `fast_forward` whether the target can fast-forward onto the tip (`same`, `ahead`)."""
        return None if self.relation is None else {
            "relation": self.relation, "commits": self.ahead, "behind": self.behind,
            "fast_forward": self.relation in ("same", "ahead")}


def _commit(project: Path, ref: str) -> str | None:
    found = git(project, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
    return found.stdout.strip() or None if found.returncode == 0 else None


def _count(project: Path, *revisions: str) -> int:
    counted = git(project, "rev-list", "--count", *revisions, check=False)
    return int(counted.stdout.strip()) if counted.returncode == 0 and counted.stdout.strip().isdigit() else 0


def _has(project: Path, commit: str, ancestor: str) -> bool:
    """Whether `commit` contains `ancestor` (or is it)."""
    return ancestor == commit or git(project, "merge-base", "--is-ancestor", ancestor, commit,
                                     check=False).returncode == 0


def _commit_id(value) -> str | None:
    """A commit id from run.json (`base_commit`, `landed.tip`), or None when it isn't one."""
    return value if isinstance(value, str) and COMMIT.fullmatch(value) else None


def landing(paths: RunPaths, data: dict, tip: str | None = None, *, into: str | None = None,
            refs: dict[str, str] | None = None, cache: dict | None = None) -> Landing:
    """The one run/target relation (`Landing`): the run's tip (`tip`, else its ref)
    against its target branch (`into`, else run.json's `target_branch`). `refs` (ref ->
    commit, a `for-each-ref` snapshot) replaces the lookups; `cache` (a dict the caller
    keeps) saves the counts of a pair seen before."""
    project, target = paths.project, into or data.get("target_branch")
    if refs is not None:
        tip = tip or refs.get(paths.ref)
        target_tip = refs.get(f"refs/heads/{target}") if target else None
    else:
        tip = tip or _commit(project, paths.ref)
        target_tip = _commit(project, f"refs/heads/{target}") if target else None
    base = _commit_id(data.get("base_commit"))
    exclude = [f"^{base}"] if base else []
    if not tip:
        return Landing(None, target, target_tip, None)
    accepted = tip != base
    # What `autohelix land` last landed on this branch (a PR squash-merged into it since).
    landed = data.get("landed") if isinstance(data.get("landed"), dict) else {}
    landed_tip = _commit_id(landed.get("tip")) if landed.get("target") == target else None
    key = (tip, target, target_tip, base, landed_tip)
    if cache is not None and key in cache:
        return cache[key]
    if not target_tip:
        found = Landing(tip, target, None, None, _count(project, tip, *exclude), accepted=accepted)
    else:
        sides = git(project, "rev-list", "--left-right", "--count", f"{target_tip}...{tip}", check=False)
        counts = sides.stdout.split()
        if sides.returncode or len(counts) != 2 or not all(c.isdigit() for c in counts):
            found = Landing(tip, target, target_tip, None, accepted=accepted)
        else:
            behind, ahead = int(counts[0]), int(counts[1])
            if ahead and base:  # only the run's own commits: not those of its base the target lacks
                ahead = _count(project, tip, f"^{target_tip}", *exclude)
            # Without commits of its own to land, a tip the target lacks base commits of is `behind`.
            relation = ("same" if tip == target_tip else "ahead" if not behind else "behind" if not ahead
                        else "diverged")
            if relation == "diverged" and landed_tip and _has(project, tip, landed_tip) \
                    and not _has(project, target_tip, landed_tip):
                # Landed here, then merged as other commits: only what it accepted since counts.
                ahead = _count(project, tip, f"^{landed_tip}", f"^{target_tip}")
                relation, since = "diverged" if ahead else "landed", landed_tip
            else:
                since = None
            found = Landing(tip, target, target_tip, relation, ahead, behind, accepted, since)
    if cache is not None:
        cache[key] = found
    return found


class LandingWords(NamedTuple):
    """A relation's words (`landing_words`): the runs table's and the web run list's cell
    (`landed`, `2 to land`, `2 diverged`, `—`), a sentence saying what it means and what
    works (the run list's tooltip, `land`'s refusal, `resume`'s warning and refusal,
    `delete`'s refusal), the end block's note after `N commits on autohelix/<id>`
    (`manual landing`, `diverged from main`), the commands that apply (`autohelix land
    <id>` only when the target can fast-forward, `git merge autohelix/<id>` when it
    diverged), `state`: the sentence without its advice (an auto-landing `resume`'s
    warning: the segment lands it), and `missing`: `<id>'s N accepted commits aren't in
    <target>` when the target lacks commits of the run (`run --import`'s `land_first`)."""

    cell: str
    sentence: str
    note: str = ""
    commands: tuple[str, ...] = ()
    state: str = ""
    missing: str = ""


def landing_words(found: Landing, run_id: str, land_mode: str | None = None) -> LandingWords:
    """The one wording of a run/target relation (`landing`'s `Landing`: its target,
    relation, counts and whether the run accepted a commit; `land_mode` the run's, `auto`
    or `manual`)."""
    short, manual = short_id(run_id), "manual landing" if land_mode == "manual" else ""
    target, accepted = found.target, found.accepted

    def words(cell, sentence, note=manual, commands=(), state=None, missing=""):
        return LandingWords(cell, sentence, note, commands, sentence if state is None else state, missing)

    if not target:
        return words("—", f"Started on a detached HEAD: land with `autohelix land {short} --into <branch>`.")
    if found.relation is None:
        if found.tip and not found.target_tip:  # the target is the missing side: another branch works
            return words("—", f"Branch {target} is not available (`autohelix land {short} --into <branch>` "
                         f"lands it on another branch).")
        if found.target_tip and not found.tip:
            return words("—", f"Run {short}'s commits are not available (no autohelix/{run_id}).")
        return words("—", f"Branch {target} or the run's commits are not available.")
    ahead, behind, relation = found.ahead, found.behind, found.relation
    missing = (f"{short}'s {plural(ahead, 'accepted commit')} {'is' if ahead == 1 else 'are'}n't in {target}"
               if ahead else "")
    if relation == "diverged":
        stop = (f", or `autohelix resume {short} --land manual` to stop trying to land it"
                if land_mode == "auto" else "")
        state = (f"{target} diverged from run {short}: {plural(ahead, 'commit')} on autohelix/{run_id} that "
                 f"{target} lacks, {plural(behind, 'commit')} on {target} that it lacks")
        if found.since:  # after a landing merged as other commits: a merge would re-apply those
            pick = f"git cherry-pick {found.since}..autohelix/{run_id}"
            how = (f"Its landed commits are in {target} as other commits; apply the ones after them by hand: "
                   f"`{pick}`")
        else:
            pick = f"git merge autohelix/{run_id}"
            how = f"Merge it by hand: `{pick}`"
        return words(f"{ahead} diverged", f"{state}, so `autohelix land` can't fast-forward {target}. {how}{stop}.",
                     f"diverged from {target}", (pick,), f"{state}.", missing)
    if not ahead:
        if relation == "behind" and not accepted:
            return words("—", f"{target} has {plural(behind, 'commit')} past run {short}'s base (committed since "
                         f"it started; it hasn't accepted any changes).")
        if not accepted:
            return words("—", "No commits accepted yet.")
        if relation == "landed":
            return words("landed", f"Run {short} landed on {target} (`autohelix land`), which has other "
                         f"commits in their place since (a squash-merged or rebased PR).")
        past = (f", which has {plural(behind, 'commit')} past its tip (committed after it)"
                if relation == "behind" else "")
        return words("landed", f"Everything run {short} accepted is in {target}{past}.")
    state = f"{plural(ahead, 'commit')} on autohelix/{run_id} not in {target} yet"
    return words(f"{ahead} to land", f"{state}; {target} can fast-forward: `autohelix land {short}`.",
                 commands=(f"autohelix land {short}",), state=f"{state}.", missing=missing)


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'s' * (count != 1)}"


def project_runs(project: Path, config_path: str | None = None) -> list[tuple[RunPaths, dict]]:
    """The project's readable runs (for one config path, if given), most recently active first."""
    found = []
    for identity in RunPaths(project).runs.glob("*/run.json"):
        if not _ID.fullmatch(identity.parent.name):
            continue
        paths = RunPaths(project, identity.parent.name)
        try:
            data = paths.read_identity()
        except RuntimeError:
            continue
        if config_path is None or data.get("config_path") == config_path:
            found.append((paths, data, last_activity(paths, data) or 0))
    found.sort(key=lambda item: item[2], reverse=True)
    return [(paths, data) for paths, data, _ in found]


def resolve_run(project: Path, value: str) -> str:
    """A run id as typed: the full id, or its last part (`k3f9`)."""
    runs = RunPaths(project).runs  # folders, not run.json: `delete --force` reaches a torn one
    if _ID.fullmatch(value) and (runs / value).is_dir():
        return value
    matches = sorted(p.name for p in runs.glob("*") if p.is_dir() and short_id(p.name) == value)
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise RuntimeError(f"{value!r} matches several runs ({', '.join(matches)}); use the full id.")
    folder = f" ({value} is a folder: use -p {value})" if Path(value).is_dir() else ""
    raise RuntimeError(f"No run {value!r} in {Path(project).resolve()}{folder}. Use a run id or its last "
                       "part, e.g. k3f9 (names aren't ids); `autohelix view --once` lists runs.")


def resolve_runs(project: Path, values) -> list[str]:
    """Several run ids as typed, all resolved before anything acts on them: an unknown
    one refuses the whole command (RuntimeError). A run named twice counts once."""
    return list(dict.fromkeys(resolve_run(project, value) for value in values))


def how_to_start(project: Path) -> str:
    """How to start a first run here, by the configs there are (lowercase, a sentence end)."""
    configs = project_configs(project)
    if not configs:
        return "set one up with `autohelix` (in a terminal), then `autohelix run`."
    if len(configs) == 1:
        return f"start one with `{run_command(configs[0])}`."
    return f"start one with `autohelix run [CONFIG]` (configs: {', '.join(configs)})."


class NoRun(RuntimeError):
    """No run for a command without a run id to act on (`default_run`)."""


def no_run(project: Path) -> str:
    """The one message for "no run to act on": none at all, or every one archived."""
    project = Path(project).resolve()
    if project_runs(project):
        return (f"No run to act on in {project}: every run is archived. Name one (`autohelix view --json` "
                f"lists them), or {how_to_start(project)}")
    return f"No runs in {project}; {how_to_start(project)}"


def default_run(project: Path) -> tuple[RunPaths, dict, str]:
    """The run a command given no run id acts on (`land`, `hint`, `view --terminal`, `report`): the
    one running run, else the most recently active run that isn't archived. Returns it
    with why it was chosen ("running", "most recently active") for the caller to say.
    RuntimeError when several runs are running (name one), NoRun when there is none."""
    runs = project_runs(project)
    live = [(p, d) for p, d in runs if is_live(p)]
    if len(live) > 1:
        raise RuntimeError(f"Several runs are running: {', '.join(short_id(p.run_id) for p, _ in live)}; "
                           "name one.")
    if live:
        return (*live[0], "running")
    for paths, data in runs:
        if not is_archived(data):
            return paths, data, "most recently active"
    raise NoRun(no_run(project))


def find_project(path) -> Path:
    """The project a command that reads runs acts on, from `path` (`--path`, default the
    current folder): that folder when it has runs (`.autohelix/runs/`), else the nearest
    folder above it, up to the top of its git checkout, that has; else `path` itself."""
    start = Path(path).resolve()
    if RunPaths(start).runs.is_dir():
        return start
    top = git(start, "rev-parse", "--show-toplevel", check=False)
    if top.returncode:
        return start
    top_path = Path(top.stdout.strip()).resolve()
    for folder in start.parents:
        if not folder.is_relative_to(top_path):
            break
        if RunPaths(folder).runs.is_dir():
            return folder
    return start


def _ago(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def _resuming(paths: RunPaths, data: dict) -> str:
    from autohelix.history import History

    last = History(paths.project, paths.run_id).get_last_iteration()
    when = last_activity(paths, data)
    ago = f", last active {_ago(time.time() - when)}" if when else ""
    back = (f" It was archived; resuming brings it back into group {data['group']}."
            if is_archived(data) and data.get("group") else "")
    return f"Resuming {short_id(paths.run_id)}: {last} iteration{'s' * (last != 1)}{ago}.{back}"


def _new_run_notices(project: Path, config_path: str, target: str | None,
                     group: str | None = None, imported: str | None = None,
                     base: str | None = None, land_mode: str | None = None) -> list[tuple[str, str]]:
    """What `run` says about this config's other runs: it never refuses. Runs of a
    group are meant to run side by side, and when either run lands manually only one of
    them lands on its own, so a live one isn't worth a warning then. With an `imported`
    run, only that run (whether `base` lacks its accepted commits, `land_first`) and the
    live ones: no "Previous run" notice."""
    notices = []
    if imported:
        paths = RunPaths(project, imported)
        try:
            said = land_first(paths, paths.read_identity(), target, base)
        except RuntimeError:
            said = None
        if said:
            notices.append(("warn", said))
    runs = project_runs(project, config_path)
    for paths, data in runs:
        if not group and land_mode != "manual" and data.get("land") != "manual" and is_live(paths):
            notices.append(("warn", f"{short_id(paths.run_id)} is also running for this config. Both land on "
                            f"{target or 'their branch'}; whichever finishes second may not fast-forward "
                            f"(its commits stay on autohelix/{paths.run_id})."))
    if imported:
        return notices
    latest = next(((p, d) for p, d in runs if not is_archived(d)), None)
    if group and latest and latest[1].get("group") == group:
        return notices  # a fellow member: unlanded and side by side is the point of a group
    if latest and not is_live(latest[0]):
        paths, data = latest
        short = short_id(paths.run_id)
        found = landing(paths, data)
        count = found.ahead
        if not has_baseline(paths):  # a failed first attempt: nothing to continue
            if not notices:
                notices.append(("info", f"Previous run for this config: {short}, without an accepted baseline "
                                f"(`autohelix delete {short}` removes it)."))
        elif count:
            words = landing_words(found, paths.run_id, data.get("land") or "auto")
            notices.append(("warn", f"{short}: {words.sentence} This run starts from {target or 'HEAD'} without "
                            f"{'it' if count == 1 else 'them'}: `autohelix resume {short}` to continue {short} "
                            f"instead, or `autohelix archive {short}` if it was merged another way."))
        elif not notices:
            notices.append(("info", f"Previous run for this config: {short} "
                            f"(`autohelix resume {short}` to continue it)."))
    return notices


def land_first(paths: RunPaths, data: dict, branch: str | None, base: str | None = None) -> str | None:
    """For a new run that continues `paths`' run (`run --import`), started on `branch`
    (a new run's base is its tip) or, detached, at `base`: when that lacks the run's
    accepted commits, how to start from them (land it on `branch`, or start a branch at
    its tip); None when it has them (or `landed`: a squash merge of a landing on it)."""
    if branch:
        found = landing(paths, data, into=branch)
        count = found.ahead if found.relation in ("ahead", "diverged") else 0
        words = landing_words(found, paths.run_id)
        command = words.commands[0] if words.commands else None
        if command and command.startswith("autohelix land") and data.get("target_branch") != branch:
            command += f" --into {branch}"
    else:  # detached: what `base` lacks, worded as a target lacking it
        tip = _commit(paths.project, paths.ref)
        count = _count(paths.project, tip, f"^{base}", *[f"^{b}" for b in [data.get("base_commit")] if b]) \
            if tip and base else 0
        words = landing_words(Landing(tip, "HEAD", base, "ahead", count, 0, True), paths.run_id)
        command = None
    if not count:
        return None
    first = f"`{command}`, or " if command else ""
    return (f"{words.missing}: land it first ({first}`git switch -c <branch> autohelix/{paths.run_id}`) "
            f"to start from {'it' if count == 1 else 'them'}.")


def _owned(paths: RunPaths) -> tuple[dict, RunLock, list[tuple[str, str]]]:
    """Take an existing run's lock for a segment."""
    lock = RunLock(paths.lock)
    try:
        data = paths.read_identity()
        if git(paths.project, "rev-parse", "--verify", paths.ref, check=False).returncode:
            raise RuntimeError(f"Run {short_id(paths.run_id)} has no ref left to resume; "
                               f"remove it with `autohelix delete {short_id(paths.run_id)}`.")
        return data, lock, [("info", _resuming(paths, data))]
    except BaseException:
        lock.close()
        raise


def require_clean(project: Path) -> None:
    """A run starts from a commit: refuse uncommitted changes in tracked files."""
    # Read-only: no opportunistic index refresh (a check like `run --check` writes nothing).
    dirty = git(project, "--no-optional-locks", "status", "--porcelain", "--untracked-files=no").stdout
    if dirty:
        raise RuntimeError("Uncommitted changes in tracked files. Commit them before starting a run:\n" + dirty)


def no_run_to_resume(config_path: str) -> str:
    return f"No run to resume for {config_path}; start one with `{run_command(config_path)}`."


def development_run(paths: RunPaths, missing: str) -> str:
    """For a run folder without a file every released version writes (`missing`): it
    was made by a development build, and is not read."""
    return (f"Run {short_id(paths.run_id)} has no {missing}: it was made by a development version of AutoHelix. "
            f"Delete it (`autohelix delete --force {short_id(paths.run_id)}`).")


def load_run_config(paths: RunPaths, data: dict | None = None):
    """The run's own config (`load_config` of its stored copy, with its stored template
    text, `RunPaths.template`; the file it came from, as run.json names it, is the
    `source`). RuntimeError (`development_run`) without one, and for a config that
    isn't an experiment's (`not_an_experiment`)."""
    data = data if data is not None else paths.read_identity()
    if not paths.config.is_file():
        raise RuntimeError(development_run(paths, "stored config"))
    try:
        text = paths.template.read_text()
    except FileNotFoundError:
        text = None
    config = load_config(paths.project, paths.config, source=paths.project / data["config_path"], template_text=text)
    if config.experiment is None:
        raise RuntimeError(f"Run {short_id(paths.run_id)}: {not_an_experiment(data['config_path'])}")
    if config.prompt_template is not None and text is None:
        raise RuntimeError(development_run(paths, "stored copy of its prompt_template"))
    return config


def new_run_name(config_path: str, name: str | None) -> str | None:
    """A new run's readable name: `name`, else its experiment's (`config_name` of the
    project-relative `config_path`: autohelix/NAME/config.yaml; None only when no letter
    or digit is left of it)."""
    if name is None:
        name = re.sub(r"[^a-z0-9-]", "-", config_name(config_path).lower()).strip("-")[:32] or None
    if name is not None and not _NAME.fullmatch(name):
        raise RuntimeError(f"Run name {name!r}: use lowercase letters, digits or hyphens, starting with a letter "
                           "or digit, at most 32 characters.")
    return name


def _refuse_legacy(project: Path) -> None:
    if (RunPaths(project).root / "history.jsonl").exists():
        raise RuntimeError("Legacy AutoHelix state: move or delete .autohelix/, then run git worktree prune.")


def _resumable(project: Path, config_path: str) -> list[RunPaths]:
    """A config's runs a bare resume may take: not archived, those with a baseline first
    (an empty run, `run` typed by mistake and interrupted, must not shadow the real one,
    but is resumed when it is all there is), then by activity."""
    candidates = [p for p, d in project_runs(project, config_path) if not is_archived(d)]
    def baseline(paths: RunPaths) -> bool:
        try:
            return has_baseline(paths)
        except RuntimeError:  # a corrupt history: that run's own resume says so, not every other's
            return False

    candidates.sort(key=baseline, reverse=True)  # stable: by activity within each
    return candidates


def planned_run(project: Path, config: Path, *, new: bool = False, run_id: str | None = None,
                create: bool = True) -> RunPaths | None:
    """Read-only, before anything is written: the run `select_run` is expected to own, or
    None for a new one. Refuses as it would: legacy state, a named run without its ref,
    no run to resume (`create` false). `select_run` decides again under its lock."""
    _refuse_legacy(project)
    if run_id:
        paths = RunPaths(project, run_id)
        if git(project, "rev-parse", "--verify", "-q", paths.ref, check=False).returncode:
            raise RuntimeError(f"Run {short_id(run_id)} has no ref left to resume; "
                               f"remove it with `autohelix delete {short_id(run_id)}`.")
        return paths
    if new:
        return None
    config_path = config_relative(project, config)
    if candidates := _resumable(project, config_path):
        return candidates[0]
    if not create:
        raise NoRunToResume(no_run_to_resume(config_path))
    return None


def select_run(project: Path, config: Path, *, name: str | None = None, new: bool = False,
               run_id: str | None = None, create: bool = True, ready=None,
               group: str | None = None, land_mode: str | None = None,
               imported: str | None = None, why: str | None = None,
               ) -> tuple[RunPaths, dict, RunLock, list[tuple[str, str]]]:
    """Select and own a run atomically before creating any per-run state.

    `run_id` resumes that run; `new` creates one (`autohelix run`); otherwise the
    config's most recently active run that isn't archived resumes, preferring
    runs with a baseline (`autohelix resume`), else one is created unless
    `create` is false. A new run stores its config (`RunPaths.config`, from `config`)
    and records `group`, the `imported` run's id (`import.run`) and `why` (if any) and
    `land_mode`.
    Returns notices, `(level, text)`, for the caller to print. `ready` runs the
    caller's readiness checks (a clean tree included) before a new run is allocated;
    raising refuses the start without leaving an identity, ref or pointer. Callers
    that check first (`planned_run`, as the Harness does before writing anything) pass
    None when the decision was a new run."""
    _refuse_legacy(project)
    # Serialize selection/creation for this project. The per-run lock remains
    # authoritative throughout the segment, including final landing.
    with RunLock(RunPaths(project).selection_lock, blocking=True):
        # Backstops, each on its own; neither blocks a run.
        for sweep in (
            lambda: sweep_staging(RunPaths(project).runs),  # a killed `run`'s; built only under this lock
            lambda: sweep_copies(project),
        ):
            try:
                sweep()
            except Exception:  # noqa: BLE001
                pass
        config_path = config_relative(project, config)
        if run_id:
            paths = RunPaths(project, run_id)
            return (paths, *_owned(paths))
        if not new:
            if candidates := _resumable(project, config_path):
                paths = candidates[0]
                if is_live(paths):  # never fall back to an older run behind the user's back
                    raise RuntimeError(f"Run {short_id(paths.run_id)} is running; name another run to resume it.")
                return (paths, *_owned(paths))
            if not create:
                raise NoRunToResume(no_run_to_resume(config_path))
        if config_name(config_path) is None:
            raise RuntimeError(not_an_experiment(config_path))
        name = new_run_name(config_path, name)
        # Refused first starts must not leave an identity, owned ref or pointer.
        if ready is not None:
            ready()
        stored = config.read_text()  # verbatim; its template's text beside it (`RunPaths.template`)
        loaded = load_config(project, config)
        template = template_text(loaded)
        files = snapshot_files(loaded, project)  # refused here, before anything is written
        base = git(project, "rev-parse", "HEAD").stdout.strip()
        target = git(project, "symbolic-ref", "--quiet", "HEAD", check=False).stdout.strip().removeprefix("refs/heads/") or None
        notices = _new_run_notices(project, config_path, target, group, imported, base, land_mode)
        # Suffixes are unique across the repository (refs of old-format ids included).
        taken = {short_id(ref) for ref in git(project, "for-each-ref", "--format=%(refname:lstrip=2)",
                                              "refs/autohelix/").stdout.split()}
        taken |= {short_id(p.name) for p in RunPaths(project).runs.glob("*")}
        # The experiment folder's copy, built beside the run folders (a dot-name no listing
        # reads) and renamed into the new run's folder.
        staging = build_snapshot(files, RunPaths(project).runs)
        try:
            while True:
                suffix = "".join(secrets.choice(_ALPHABET) for _ in range(4))
                if suffix in taken:
                    continue
                candidate = "-".join(part for part in (time.strftime("%Y%m%d-%H%M"), name, suffix) if part)
                paths = RunPaths(project, candidate)
                paths.runs.mkdir(parents=True, exist_ok=True)
                try:
                    paths.run_dir.mkdir()
                except FileExistsError:
                    continue
                lock = RunLock(paths.lock)
                created = git(project, "update-ref", "--no-deref", "--create-reflog", "-m", "AutoHelix create run",
                              paths.ref, base, "0" * len(base), check=False)
                if created.returncode:
                    lock.close()
                    paths.run_dir.rmdir()
                    if git(project, "rev-parse", "--verify", paths.ref, check=False).returncode == 0:
                        continue
                    raise RuntimeError(created.stderr.strip())
                data = {"schema": SCHEMA, "id": candidate, "name": name, "config_path": config_path,
                        "created_at": time.time(), "base_commit": base, "target_branch": target,
                        "ref": paths.ref, **({"group": group} if group else {}),
                        **({"land": land_mode} if land_mode else {}),
                        **({"import": {"run": imported}} if imported else {}),
                        **({"why": why} if why else {})}
                try:
                    # First: a readable run (run.json) has its config and experiment folder.
                    # Never edited: a run keeps the config it started with.
                    os.rename(staging, paths.experiment)
                    for path, text in ((paths.config, stored), (paths.template, template)):
                        if text is not None:
                            write_text(path, text)
                            os.chmod(path, 0o444)
                    write_json(paths.identity, data)
                except BaseException:
                    lock.close()
                    raise
                return paths, data, lock, notices
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


def repair_history(paths: RunPaths) -> None:
    """Under run ownership: repair only a torn final JSON record (its iteration is left
    without a record.json: its folder is incomplete), and rewrite the record.json of
    iterations whose record.json is missing or differs from history.jsonl (a process
    killed between the two writes)."""
    from autohelix.history import stale_records, valid_record, write_records

    path = paths.history
    if not path.exists():
        return
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    records = []
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            if index != len(lines) - 1 or line.endswith(b"\n"):
                raise RuntimeError(f"Corrupt history record {index + 1}: {path}") from None
            torn = re.match(rb'\s*\{\s*"iteration"\s*:\s*(\d+)\s*[,}]', line)
            write_records(paths, records, mode="replace",
                          iterations=stale_records(paths, records) | ({int(torn[1])} if torn else set()))
            return
        if not valid_record(record):
            raise RuntimeError(f"Invalid history record {index + 1}: {path}")
        records.append(record)
    stale = stale_records(paths, records)
    if raw and not raw.endswith(b"\n"):  # complete, but unterminated
        write_records(paths, records, mode="replace", iterations=stale)
    elif stale:
        write_records(paths, records, mode="derived", iterations=stale)


def target_ahead_refusal(paths: RunPaths, data: dict, found: Landing) -> str:
    """Why an auto-landing `resume` refuses a run whose target moved past its tip (`found`
    is `behind`: by you or another run, after its tip, or since it started if it never
    accepted a change; or `landed`, merged there as other commits)."""
    short = short_id(paths.run_id)
    said = landing_words(found, paths.run_id, "auto").sentence
    return (f"{said} A run continues its own code: start a new run that continues from it, "
            f"`{run_command(data['config_path'])} --import {short}`, or continue without landing: "
            f"`autohelix resume {short} --land manual`.")


def reconcile(paths: RunPaths, data: dict, history, land_mode: str) -> tuple[str, str | None]:
    """Recover a ref advance without its record, then compare the run's tip with its
    target. A run continues its own tip: a target that moved past it refuses an
    auto-landing run (`target_ahead_refusal`) and is a warning for a manual one."""
    from autohelix.history import IterationResult

    repair_history(paths)
    history._invalidate_cache()
    tip = git(paths.project, "rev-parse", paths.ref).stdout.strip()
    commits = git(paths.project, "log", "--format=%H%x00%B%x00",
                  f"{data['base_commit']}..{tip}").stdout.split("\x00")
    for index in range(0, len(commits) - 1, 2):
        commit, message = commits[index].strip(), commits[index + 1]
        if re.search(rf"^AutoHelix-Run: {re.escape(paths.run_id)}$", message, re.MULTILINE):
            iteration = re.search(r"^AutoHelix-Iteration: (\d+)$", message, re.MULTILINE)
            if iteration and int(iteration[1]) > history.get_last_iteration():
                history.append(IterationResult(
                    iteration=int(iteration[1]), accepted=True, reason="recovered", commit=commit,
                    calls=[{"role": "agent", "usage": {}, "log": None}],
                ))
            break
    warning = None
    baseline_captured = any(row.iteration == 0 and row.accepted for row in history.load())
    found = landing(paths, data, tip)
    short, target = short_id(paths.run_id), found.target
    words = landing_words(found, paths.run_id, land_mode)
    # Nothing to say before the baseline, or when the target is the tip.
    if not baseline_captured:
        return tip, None
    if found.relation in ("behind", "landed"):
        if land_mode == "auto":
            raise RuntimeError(target_ahead_refusal(paths, data, found))
        merge = (f"git cherry-pick {found.since}..autohelix/{paths.run_id}" if found.since
                 else f"git merge autohelix/{paths.run_id}")
        warning = (f"{words.sentence} Run {short} continues from its own tip, without {target}'s commits "
                   f"(manual landing: once it accepts a change, {'apply' if found.since else 'merge'} it "
                   f"yourself: {merge}).")
    elif found.relation == "diverged":
        warning = f"{words.sentence} Run {short} continues from its accepted tip."
    elif found.relation == "ahead" and found.ahead and land_mode == "auto":  # manual: ahead is just not landed yet
        warning = (f"{words.state} Auto landing lands {'it' if found.ahead == 1 else 'them'} again at the end "
                   f"of this segment; if a reset of {target} meant to discard the run, press Ctrl-C and "
                   f"`autohelix archive {short}`.")
    return tip, warning


def land(paths: RunPaths, data: dict, expected_tip: str, into: str | None = None) -> None:
    """Fast-forward the target to the harness's validated tip, never a moving ref."""
    target = into or data.get("target_branch")
    if not target:
        raise RuntimeError("This run started detached; land requires --into <branch>.")
    ref = f"refs/heads/{target}"
    found = landing(paths, data, expected_tip, into=target)
    if not found.target_tip:
        raise RuntimeError(f"No branch {target}; create it first (git branch {target} <base>).")
    if found.relation in ("same", "behind", "landed"):
        # Already there (a `behind` target that has the tip), or nothing accepted to land.
        there = found.relation != "behind" or _has(paths.project, found.target_tip, expected_tip)
        if there or not found.accepted:
            if there and found.relation != "landed":
                _record_landed(paths, data, found)
            return
    hint = f"Run {paths.run_id} remains available: git merge autohelix/{paths.run_id}"
    if found.relation != "ahead":  # diverged (also commits accepted after a squash-merged landing)
        why = (landing_words(found, paths.run_id, data.get("land") or "auto").sentence
               if found.relation else f"can't compare {target} with the run's tip.")
        raise RuntimeError(f"Cannot land: {why}" + ("" if found.relation else f" {hint}"))
    checkout = None
    for record in git(paths.project, "worktree", "list", "--porcelain").stdout.split("\n\n"):
        lines = record.splitlines()
        if f"branch {ref}" in lines:
            checkout = Path(lines[0].removeprefix("worktree ")).resolve()
            break
    root = Path(git(paths.project, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    with critical_section():
        if checkout == root:
            result = git(root, "merge", "--ff-only", expected_tip, check=False)
            if result.returncode:
                raise RuntimeError(f"Cannot land (checkout would be overwritten). {hint}")
        elif checkout:
            raise RuntimeError(f"Cannot land: {target} is checked out in another worktree ({checkout}). {hint}")
        else:
            result = git(paths.project, "fetch", "--no-write-fetch-head", "--no-tags",
                         "--recurse-submodules=no", ".", f"{expected_tip}:{ref}", check=False)
            if result.returncode:
                raise RuntimeError(f"Cannot land {target}: branch is busy or changed. {hint}")
        _record_landed(paths, data, found)


def _record_landed(paths: RunPaths, data: dict, found: Landing) -> None:
    """run.json's `landed: {tip, target, at}`: what `land` last put on the run's own
    target (the only one `landing` reads), so the run still reads landed once a PR merges
    it as other commits (squash, rebase). Not for a landing on another branch (`--into`),
    or for a run that accepted nothing."""
    tip, target = found.tip, found.target
    if not found.accepted or not tip or target != data.get("target_branch"):
        return
    landed = data.get("landed")
    if isinstance(landed, dict) and (landed.get("tip"), landed.get("target")) == (tip, target):
        return
    data["landed"] = {"tip": tip, "target": target, "at": datetime.now(timezone.utc).isoformat()}
    write_json(paths.identity, data)


def archive(paths: RunPaths, *, undo: bool = False, why: str | None = None) -> None:
    """Set a run aside (hidden from the run list, skipped by a bare `resume`), or bring it
    back. `why` (archive only) replaces the archive note, `archive_why`; undo keeps it."""
    with RunLock(paths.lock):
        data = paths.read_identity()
        if undo:
            data.pop("archived", None)
        else:
            data["archived"] = True
            if why:
                data["archive_why"] = why
        write_json(paths.identity, data)


def group_landing_note(paths: RunPaths, data: dict, group: str | None, land_mode: str | None) -> str | None:
    """`resume --group NAME` without `--land` on a run that lands automatically: the
    landing mode doesn't change as a side effect, so say so."""
    if group and not land_mode and (data.get("land") or "auto") == "auto":
        return f"{short_id(paths.run_id)} still lands automatically; pass `--land manual` for a group."
    return None


def set_options(paths: RunPaths, *, land_mode: str | None = None, group: str | None = None) -> list[str]:
    """`resume --land` / `--group` with nothing to run: store them now, under the run's
    lock (a run that is running refuses), and say what changed (nothing: [])."""
    with RunLock(paths.lock):
        data = paths.read_identity()
        changed = []
        if land_mode and (data.get("land") or "auto") != land_mode:
            data["land"] = land_mode
            changed.append(f"landing set to {land_mode}")
        if group is not None and (data.get("group") or "") != group:
            changed.append(f"joins group {group}" if group else f"leaves group {data['group']}")
            if group:
                data["group"] = group
            else:
                data.pop("group", None)
        if changed:
            write_json(paths.identity, data)
        return changed


def delete(paths: RunPaths, *, force: bool = False, warn=None) -> None:
    """Remove a run's state, ref, pin, rejected trees and copies. Refuses while it runs, and, unless
    forced, while it has unlanded commits (they'd become unreachable)."""
    short = short_id(paths.run_id)
    with RunLock(paths.lock):
        try:
            data = paths.read_identity()
        except RuntimeError:
            if not force:
                raise
            data = {}
        found = landing(paths, data) if data else None
        if found and found.ahead and not force:
            sentence = landing_words(found, paths.run_id).sentence
            raise RuntimeError(f"Not deleting run {short}, which has {plural(found.ahead, 'unlanded commit')}: "
                               f"{sentence} "
                               f"`autohelix delete --force {short}` discards {'it' if found.ahead == 1 else 'them'}.")
        remove_copies(paths.copies, warn or (lambda message: None))
        git(paths.project, "update-ref", "-d", paths.pin, check=False)
        kept = git(paths.project, "for-each-ref", "--format=%(refname)", paths.rejected_refs, check=False)
        if kept.returncode == 0 and kept.stdout.split():  # in one transaction
            from autohelix.workspace import run_git

            run_git(["git", "-c", "gc.auto=0", "update-ref", "--stdin"], cwd=paths.project, capture_output=True,
                    text=True, start_new_session=True,
                    input="".join(f"delete {ref}\n" for ref in kept.stdout.split()))
        tip = git(paths.project, "rev-parse", "--verify", paths.ref, check=False)
        if tip.returncode == 0:
            git(paths.project, "update-ref", "--no-deref", "-d", paths.ref, tip.stdout.strip())
        shutil.rmtree(paths.run_dir)


def run_tip(paths: RunPaths, data: dict | None = None) -> str:
    """The run's accepted code: its ref, else (no ref yet) its base commit."""
    result = git(paths.project, "rev-parse", "--verify",
                 paths.ref if paths.run_id else "HEAD", check=False)
    if result.returncode == 0:
        return result.stdout.strip()
    return (data or paths.read_identity())["base_commit"]
