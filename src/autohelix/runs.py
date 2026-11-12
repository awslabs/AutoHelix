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
from pathlib import Path

SCHEMA = 1
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,50}\Z")  # YYYYMMDD-HHMM-<name>-xxxx
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}\Z")
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


def allocate_call(directory: Path, role: str) -> Path:
    """Reserve one numbered directory per actual call, including concurrent asks."""
    directory.mkdir(parents=True, exist_ok=True)
    index = 1
    while True:
        if any(directory.glob(f"call-{index:03d}-*")):
            index += 1
            continue
        path = directory / f"call-{index:03d}-{role}"
        try:
            path.mkdir()
            return path
        except FileExistsError:
            index += 1


@dataclass(frozen=True)
class RunPaths:
    """The sole definition of project and per-run state paths.

    Without an explicit id, observers follow current. The root remains useful
    for project setup, but observers must not treat it as a run without an id.
    """

    project: Path
    run_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "project", Path(self.project).resolve())
        run_id = self.run_id
        if run_id in (None, "current"):
            try:
                run_id = (self.root / "current").read_text().strip()
            except FileNotFoundError:
                run_id = None
        if run_id is not None and not _ID.fullmatch(run_id):
            raise RuntimeError(f"Invalid run id: {run_id!r}")
        object.__setattr__(self, "run_id", run_id)

    @property
    def root(self): return self.project / ".autohelix"
    @property
    def runs(self): return self.root / "runs"
    @property
    def run_dir(self): return self.runs / self.run_id if self.run_id else self.root
    @property
    def identity(self): return self.run_dir / "run.json"
    @property
    def history(self): return self.run_dir / "history.jsonl"
    @property
    def notes(self): return self.run_dir / "notes"
    @property
    def logs(self): return self.run_dir / "logs"
    @property
    def observations(self): return self.run_dir / "observations"
    @property
    def hints(self): return self.run_dir / "hints.md"
    @property
    def output(self): return self.run_dir / "output"
    @property
    def runtime(self): return self.run_dir / "runtime"
    @property
    def status(self): return self.runtime / "status.json"
    @property
    def project_runtime(self): return self.root / "runtime"
    @property
    def copies_record(self): return self.run_dir / "copies"  # the copies base the last segment used
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

    def copies_under(self, base: Path) -> Path:
        return base / project_key(self.project) / self.run_id
    @property
    def ref(self): return f"refs/autohelix/{self.run_id}"
    @property
    def pin(self): return f"refs/autohelix-tmp/{self.run_id}"  # the frozen candidate tree
    @property
    def lock(self): return self.project_runtime / "locks" / f"{self.run_id}.lock"

    def call_dir(self, iteration: int, role: str, index: int = 1) -> Path:
        return self.logs / f"iter-{iteration}" / f"call-{index:03d}-{role}"

    def relative_log(self, value) -> str | None:
        """Normalize untrusted log metadata, retaining usage without unsafe logs."""
        if not self.run_id or not isinstance(value, (str, Path)) or not value:
            return None
        try:
            path = Path(value)
            path = (path if path.is_absolute() else self.run_dir / path).resolve()
            root = self.run_dir.resolve()
            logs = self.logs.resolve()
            if not logs.is_relative_to(root) or not path.is_relative_to(logs):
                return None
            return str(path.relative_to(root))
        except (OSError, ValueError, RuntimeError):
            return None

    def read_identity(self) -> dict:
        try:
            data = json.loads(self.identity.read_text())
        except (OSError, ValueError) as e:
            raise RuntimeError(f"Could not read run {self.run_id}: {e}") from e
        if data.get("schema") != SCHEMA or data.get("id") != self.run_id:
            raise RuntimeError(f"Unsupported or inconsistent run identity: {self.identity}")
        return data


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
                lock = RunLock(RunPaths(project, entry.name).lock)
            except RuntimeError:  # live
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


class RunLock:
    """Nonblocking, process-held ownership; the persistent inode is never removed."""

    def __init__(self, path: Path, *, blocking=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a+")
        try:
            fcntl.flock(self._file, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            self._file.close()
            raise RuntimeError(f"Run {short_id(path.stem)} is running.") from None

    def close(self):
        if not self._file.closed:
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
    """Defer Ctrl-C until the commit, ref update, and durable record agree."""
    pending = []
    main = threading.current_thread() is threading.main_thread()
    previous = signal.getsignal(signal.SIGINT) if main else None
    if main:
        signal.signal(signal.SIGINT, lambda *args: pending.append(args))
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        if main:
            signal.signal(signal.SIGINT, previous)
        if pending and not failed:
            if callable(previous):
                previous(*pending[0])
            elif previous != signal.SIG_IGN:
                raise KeyboardInterrupt


def config_relative(project: Path, config: Path) -> str:
    try:
        return str(config.resolve().relative_to(project.resolve()))
    except ValueError:
        return str(config.resolve())


class NoRunToResume(RuntimeError):
    """A bare `resume` found nothing to continue."""


def is_archived(data: dict) -> bool:
    return bool(data.get("archived")) or data.get("lifecycle") == "abandoned"  # pre-archive runs


def is_live(paths: RunPaths) -> bool:
    """Whether a process holds the run's lock (a brief probe: advisory)."""
    try:
        RunLock(paths.lock).close()
    except RuntimeError:
        return True
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


def has_baseline(paths: RunPaths) -> bool:
    from autohelix.history import History

    return any(r.iteration == 0 and r.accepted for r in History(paths.project, paths.run_id).load())


def unlanded(paths: RunPaths, data: dict) -> int:
    """Accepted commits beyond the base that the target branch lacks (all of them
    without a target branch). Squash-merged or cherry-picked work counts."""
    tip = git(paths.project, "rev-parse", "--verify", f"{paths.ref}^{{commit}}", check=False)
    if tip.returncode:
        return 0
    exclude = [f"^{data['base_commit']}"] if data.get("base_commit") else []
    target = data.get("target_branch")
    if target and git(paths.project, "rev-parse", "--verify", f"refs/heads/{target}", check=False).returncode == 0:
        exclude.append(f"^refs/heads/{target}")
    return int(git(paths.project, "rev-list", "--count", tip.stdout.strip(), *exclude).stdout.strip() or 0)


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
                       "part, e.g. k3f9 (names aren't ids); `autohelix dashboard` lists runs.")


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
                     group: str | None = None) -> list[tuple[str, str]]:
    """What `run` says about this config's other runs: it never refuses. Runs of a
    group are meant to run side by side, so a live one isn't worth a warning."""
    notices = []
    runs = project_runs(project, config_path)
    for paths, _ in runs:
        if not group and is_live(paths):
            notices.append(("warn", f"{short_id(paths.run_id)} is also running for this config. Both land on "
                            f"{target or 'their branch'}; whichever finishes second may not fast-forward "
                            f"(its commits stay on autohelix/{paths.run_id})."))
    latest = next(((p, d) for p, d in runs if not is_archived(d)), None)
    if group and latest and latest[1].get("group") == group:
        return notices  # a fellow member: unlanded and side by side is the point of a group
    if latest and not is_live(latest[0]):
        paths, data = latest
        short = short_id(paths.run_id)
        count = unlanded(paths, data)
        if count:
            notices.append(("warn", f"{short} has {count} unlanded commit{'s' * (count != 1)}; this run starts "
                            f"from {target or 'HEAD'} without them. `autohelix resume {short}` to continue it "
                            f"instead, or `autohelix archive {short}` if it was merged another way."))
        elif not notices:
            notices.append(("info", f"Previous run for this config: {short} "
                            f"(`autohelix resume {short}` to continue it)."))
    return notices


def _owned(paths: RunPaths, config_path: str) -> tuple[dict, RunLock, list[tuple[str, str]]]:
    """Take an existing run's lock for a segment."""
    lock = RunLock(paths.lock)
    try:
        data = paths.read_identity()
        if git(paths.project, "rev-parse", "--verify", paths.ref, check=False).returncode:
            raise RuntimeError(f"Run {short_id(paths.run_id)} has no ref left to resume; "
                               f"remove it with `autohelix delete {short_id(paths.run_id)}`.")
        notices = [("info", _resuming(paths, data))]
        if data.get("config_path") != config_path:
            notices.append(("warn", f"Run {short_id(paths.run_id)} was started with {data.get('config_path')}; "
                            f"this segment uses {config_path}."))
        return data, lock, notices
    except BaseException:
        lock.close()
        raise


def select_run(project: Path, config: Path, *, name: str | None = None, new: bool = False,
               run_id: str | None = None, create: bool = True, ready=None,
               group: str | None = None, land_mode: str | None = None,
               ) -> tuple[RunPaths, dict, RunLock, list[tuple[str, str]]]:
    """Select and own a run atomically before creating any per-run state.

    `run_id` resumes that run; `new` creates one (`autohelix run`); otherwise the
    config's most recently active run that isn't archived resumes, preferring
    runs with a baseline (`autohelix resume`), else one is created unless
    `create` is false. A new run records `group` (if any) and `land_mode`.
    Returns notices, `(level, text)`, for the caller to print. `ready` runs the
    caller's readiness checks before a new run is allocated; raising refuses the
    start without leaving an identity, ref or pointer."""
    root = RunPaths(project).root
    if (root / "history.jsonl").exists():
        raise RuntimeError("Legacy AutoHelix state: move or delete .autohelix/, then run git worktree prune.")
    # Serialize selection/creation for this project. The per-run lock remains
    # authoritative throughout the invocation, including final landing.
    with RunLock(root / "runtime" / "locks" / "selection.lock", blocking=True):
        try:
            sweep_copies(project)
        except Exception:  # noqa: BLE001 - a backstop; never blocks a run
            pass
        config_path = config_relative(project, config)
        if run_id:
            paths = RunPaths(project, run_id)
            return (paths, *_owned(paths, config_path))
        if not new:
            # Runs that captured a baseline first: an empty run (`run` typed by mistake and
            # interrupted) must not shadow the real one, but is resumed when it is all there is.
            candidates = [p for p, d in project_runs(project, config_path) if not is_archived(d)]
            candidates.sort(key=has_baseline, reverse=True)  # stable: by activity within each
            if candidates:
                paths = candidates[0]
                if is_live(paths):  # never fall back to an older run behind the user's back
                    raise RuntimeError(f"Run {short_id(paths.run_id)} is running; name another run to resume it.")
                return (paths, *_owned(paths, config_path))
            if not create:
                raise NoRunToResume(f"No run to resume for {config_path}; start one with `autohelix run`.")
        if name is None:
            stem = config.stem
            name = None if stem == "autohelix" else re.sub(r"[^a-z0-9-]", "-", stem.lower()).strip("-")[:32] or None
        if name is not None and not _NAME.fullmatch(name):
            raise RuntimeError("name must use lowercase letters, digits or hyphens, with at most 32 characters.")
        # Refused first starts must not leave an identity, owned ref or pointer.
        dirty = git(project, "status", "--porcelain", "--untracked-files=no").stdout
        if dirty:
            raise RuntimeError("Uncommitted changes in tracked files. Commit them before starting a run:\n" + dirty)
        if ready is not None:
            ready()
        base = git(project, "rev-parse", "HEAD").stdout.strip()
        target = git(project, "symbolic-ref", "--quiet", "HEAD", check=False).stdout.strip().removeprefix("refs/heads/") or None
        notices = _new_run_notices(project, config_path, target, group)
        # Suffixes are unique across the repository (refs of old-format ids included).
        taken = {short_id(ref) for ref in git(project, "for-each-ref", "--format=%(refname:lstrip=2)",
                                              "refs/autohelix/").stdout.split()}
        taken |= {short_id(p.name) for p in RunPaths(project).runs.glob("*")}
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
                    **({"land": land_mode} if land_mode else {})}
            try:
                write_json(paths.identity, data)
            except BaseException:
                lock.close()
                raise
            return paths, data, lock, notices


def repair_history(path: Path) -> None:
    """Repair only a torn final JSON record, under run ownership."""
    if not path.exists():
        return
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    offset = 0
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            if index != len(lines) - 1 or line.endswith(b"\n"):
                raise RuntimeError(f"Corrupt history record {index + 1}: {path}") from None
            with path.open("r+b") as stream:
                stream.truncate(offset)
            return
        if (not isinstance(record, dict) or type(record.get("iteration")) is not int
                or record["iteration"] < 0 or type(record.get("accepted")) is not bool):
            raise RuntimeError(f"Invalid history record {index + 1}: {path}")
        offset += len(line)
    if raw and not raw.endswith(b"\n"):
        with path.open("ab") as stream:
            stream.write(b"\n")


def reconcile(paths: RunPaths, data: dict, history, land_mode: str) -> tuple[str, str | None]:
    """Recover a ref advance without its record, then pick up target commits."""
    from autohelix.history import IterationResult

    repair_history(paths.history)
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
    target = data.get("target_branch")
    baseline_captured = any(row.iteration == 0 and row.accepted for row in history.load())
    branch = git(paths.project, "rev-parse", f"refs/heads/{target}", check=False) if target else None
    target_tip = branch.stdout.strip() if branch and branch.returncode == 0 else tip
    # Nothing to say before the baseline, or when the target is the tip.
    if baseline_captured and target_tip != tip:
        def ancestor(a, b):
            return git(paths.project, "merge-base", "--is-ancestor", a, b, check=False).returncode == 0

        if ancestor(tip, target_tip):
            git(paths.project, "update-ref", "--no-deref", "-m", "AutoHelix resume from target", paths.ref, target_tip, tip)
            tip = target_tip
        elif not ancestor(target_tip, tip):
            warning = (f"Target {target} diverged; continuing run {short_id(paths.run_id)} from its accepted tip. "
                       "Landing requires a branch that can fast-forward.")
        elif land_mode == "auto":  # in manual mode, behind just means not landed yet
            warning = (f"Target {target} is behind run {short_id(paths.run_id)}; auto landing will reapply accepted code. "
                       f"If a reset meant to discard the run, press Ctrl-C and `autohelix archive {short_id(paths.run_id)}`.")
    return tip, warning


def land(paths: RunPaths, data: dict, expected_tip: str, into: str | None = None) -> None:
    """Fast-forward the target to the harness's validated tip, never a moving ref."""
    target = into or data.get("target_branch")
    if not target:
        raise RuntimeError("This run started detached; land requires --into <branch>.")
    ref = f"refs/heads/{target}"
    old = git(paths.project, "rev-parse", "--verify", ref).stdout.strip()
    if git(paths.project, "merge-base", "--is-ancestor", expected_tip, old, check=False).returncode == 0:
        return
    hint = f"Run {paths.run_id} remains available: git merge autohelix/{paths.run_id}"
    if git(paths.project, "merge-base", "--is-ancestor", old, expected_tip, check=False).returncode:
        raise RuntimeError(f"Cannot land: {target} diverged (including commits made during this segment). {hint}")
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


def archive(paths: RunPaths, *, undo: bool = False) -> None:
    """Set a run aside (hidden from the run list, skipped by a bare `resume`), or bring it back."""
    with RunLock(paths.lock):
        data = paths.read_identity()
        data.pop("lifecycle", None)
        if undo:
            data.pop("archived", None)
        else:
            data["archived"] = True
        write_json(paths.identity, data)


def delete(paths: RunPaths, *, force: bool = False, warn=None) -> None:
    """Remove a run's state, ref, pin and copies. Refuses while it runs, and, unless
    forced, while it has unlanded commits (they'd become unreachable)."""
    short = short_id(paths.run_id)
    with RunLock(paths.lock):
        try:
            data = paths.read_identity()
        except RuntimeError:
            if not force:
                raise
            data = {}
        count = unlanded(paths, data) if data else 0
        if count and not force:
            raise RuntimeError(f"Run {short} has {count} unlanded commit{'s' * (count != 1)} on "
                               f"autohelix/{paths.run_id}: land them first (`autohelix land {short}`), "
                               f"or `autohelix delete --force {short}` to discard them.")
        remove_copies(paths.copies, warn or (lambda message: None))
        git(paths.project, "update-ref", "-d", paths.pin, check=False)
        tip = git(paths.project, "rev-parse", "--verify", paths.ref, check=False)
        if tip.returncode == 0:
            git(paths.project, "update-ref", "--no-deref", "-d", paths.ref, tip.stdout.strip())
        shutil.rmtree(paths.run_dir)
    current = paths.root / "current"
    try:
        pointed = current.read_text().strip()
    except OSError:
        return
    if pointed == paths.run_id:  # follow the most recently active remaining run
        remaining = project_runs(paths.project)
        remaining.sort(key=lambda item: is_archived(item[1]))  # stable: activity order within each
        if remaining:
            write_text(current, remaining[0][0].run_id + "\n")
        else:
            current.unlink(missing_ok=True)


def run_tip(paths: RunPaths, data: dict | None = None) -> str:
    """Read accepted code even after an earlier `abandon` removed a fully landed ref."""
    result = git(paths.project, "rev-parse", "--verify",
                 paths.ref if paths.run_id else "HEAD", check=False)
    if result.returncode == 0:
        return result.stdout.strip()
    from autohelix.history import History

    records = History(paths.project, paths.run_id).load()
    commit = next((r.commit for r in reversed(records) if r.accepted and r.commit), None)
    identity = data or paths.read_identity()
    return identity.get("abandoned_tip") or commit or identity["base_commit"]
