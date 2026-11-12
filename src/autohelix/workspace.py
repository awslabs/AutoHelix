# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Candidate copies and tree snapshots.

Every phase of an iteration runs in its own fresh `Copy`: a private folder
holding one fixed tree, with its own small `.git` that borrows the user's
objects through alternates. The harness reads a copy only from outside, through
the user's repository (`Repo`) and a harness-owned index kept next to the copy;
it never runs git inside a copy once a call has used it. `Fingerprint` detects
lasting changes to a copy's files from their raw bytes.
"""

import fnmatch
import hashlib
import logging
import os
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Repository-location and behavior overrides a parent process could leak into
# harness git calls. Identity, transport and config-file variables are kept.
SCRUBBED_GIT_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_PREFIX", "GIT_QUARANTINE_PATH", "GIT_ATTR_SOURCE",
    "GIT_SHALLOW_FILE", "GIT_GRAFT_FILE", "GIT_REPLACE_REF_BASE", "GIT_NO_REPLACE_OBJECTS",
    "GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS",
)
_SAFE = ("-c", "gc.auto=0", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
         "-c", "core.sparseCheckout=false", "-c", "core.splitIndex=false",
         "-c", "core.untrackedCache=false")
# For git on the user's own checkout and index: leave its index format and sparse checkout alone.
_SAFE_USER = ("-c", "gc.auto=0", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
_index_dir: Path | None = None
# Settings a copy needs so files materialize and stage as in the user's repo.
_COPIED_CONFIG = (r"^(core\.(autocrlf|eol|safecrlf|symlinks|ignorecase|precomposeunicode)"
                  r"|filter\..*|lfs\..*|user\.(name|email))$")
ATTR_SOURCE_LIMITATION = (
    "git lacks --attr-source (needs 2.41+): an out-of-scope .gitattributes edit by the agent "
    "can change how in-scope files are stored. Published code still equals checked code.")
GITLINK = "160000"


def diff_args(*args: str) -> tuple[str, ...]:
    """`git diff` arguments for every diff the harness writes (candidate.diff,
    rejected.patch, a review's diff), the same whatever the user's git config: `a/` and
    `b/` prefixes on paths from the repository root (even in a subdirectory, or with
    `diff.noprefix`/`diff.mnemonicPrefix`/`diff.relative`), no color, no external diff
    or textconv, binary changes included, so `git apply` takes them anywhere."""
    return ("-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "-c", "color.diff=never",
            "diff", "--binary", "--no-color", "--no-ext-diff", "--no-textconv",
            "--src-prefix=a/", "--dst-prefix=b/", "--no-relative", *args)
# The harness's per-iteration material in agent and reviewer copies (prompt, notes,
# history, other runs of the group); never part of a candidate.
ITERATION_DIR = ".autohelix-iteration"
# Never frozen: the project's own state folder (only in a copy if an inner run made it)
# and the harness's material. Excluded by pathspec, so their files are never hashed.
_UNFROZEN = (".autohelix", ITERATION_DIR)
# Transient lock contention from a concurrent git process on the same repository
# (packed-refs, reflogs, a ref's own lock); a CAS mismatch is not one.
# Matched by the lock file's name (`<path>.lock'`), which git prints in every language.
_LOCK_ERRORS = (".lock'", "Another git process seems to be running")
_LOCK_RETRIES = (0.05, 0.1, 0.2, 0.4, 0.8)


def git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in SCRUBBED_GIT_ENV}
    env.update(extra or {})
    return env


def is_lock_error(stderr) -> bool:
    text = stderr if isinstance(stderr, str) else os.fsdecode(stderr or b"")
    return any(marker in text for marker in _LOCK_ERRORS)


def run_git(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    """`subprocess.run` for git on the user's repository, retried briefly while
    another git process (a concurrent run) holds one of its lock files."""
    for delay in (*_LOCK_RETRIES, None):
        result = subprocess.run(argv, **kwargs)
        if not result.returncode or delay is None or not is_lock_error(result.stderr):
            return result
        time.sleep(delay)
    return result


class CandidateRejected(RuntimeError):
    """A candidate cannot be snapshotted, e.g. it changed a submodule."""


class Repo:
    """The user's repository; every harness git call goes through it."""

    def __init__(self, project: Path):
        self.project = Path(project).resolve()
        result = run_git(
            ["git", "rev-parse", "--absolute-git-dir", "--git-common-dir", "--show-toplevel",
             "--show-object-format", "--show-prefix"],
            cwd=self.project, env=git_env(), capture_output=True, text=True,
        )
        if result.returncode:
            raise RuntimeError(f"Not a git repository: {self.project}")
        git_dir, common, root, self.object_format, *prefix = result.stdout.split("\n")
        self.git_dir = Path(git_dir).resolve()
        self.objects = (self.project / common).resolve() / "objects"
        self.root = Path(root).resolve()
        self.prefix = prefix[0] if prefix else ""
        self.attr_source = self.git("--attr-source=HEAD", "version", check=False).returncode == 0

    def git(self, *args: str, work_tree: Path | None = None, index: Path | None = None,
            input: bytes | None = None, check: bool = True, cwd: Path | None = None,
            timeout: float | None = None, user_checkout: bool = False) -> subprocess.CompletedProcess:
        argv = ["git", "--git-dir", str(self.git_dir), *(["--work-tree", str(work_tree)] if work_tree else []),
                *(_SAFE_USER if user_checkout else _SAFE), *args]
        result = run_git(
            argv, cwd=cwd or work_tree or self.root, input=input, capture_output=True,
            env=git_env({"GIT_INDEX_FILE": str(index)} if index else None), start_new_session=True,
            timeout=timeout,
        )
        if check and result.returncode:
            detail = os.fsdecode(result.stderr or result.stdout).strip()
            raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
        return result

    def out(self, *args: str, **kwargs) -> str:
        return os.fsdecode(self.git(*args, **kwargs).stdout).strip()

    def unfrozen_excludes(self, work_tree: Path, index: Path, **kwargs) -> list[str]:
        """Pathspecs keeping `.autohelix/` and `.autohelix-iteration/` out of an `add -A`
        in a copy, so their files are never hashed. `add` refuses an exclude naming an
        ignored folder; an ignored folder isn't added anyway, so it needs none."""
        unfrozen = [f"{self.prefix}{name}" for name in _UNFROZEN]
        ignored = set(_records(self.git("check-ignore", "-z", "--stdin", work_tree=work_tree, index=index,
                                        input="\0".join(unfrozen).encode() + b"\0", check=False,
                                        **kwargs).stdout))
        return [f":(top,exclude){name}" for name in unfrozen if name not in ignored]


def path_in_scope(path: str, patterns) -> bool:
    for pattern in patterns:
        if pattern in (".", "./"):
            return True
        prefix = pattern.rstrip("/")
        if path == prefix or path.startswith(prefix + "/") or fnmatch.fnmatch(path, pattern):
            return True
    return False


@dataclass(frozen=True)
class Scope:
    """The run's write rules. Names are relative to the repository root; patterns
    are relative to the project directory at `prefix`."""

    prefix: str = ""
    editable: tuple[str, ...] = ()
    frozen: tuple[str, ...] = ()

    def allows(self, name: str) -> bool:
        if not name.startswith(self.prefix):
            return False
        relative = name[len(self.prefix):]
        return ((not self.editable or path_in_scope(relative, self.editable))
                and not path_in_scope(relative, self.frozen))


def _content(path: Path, mode: int) -> tuple:
    """What a file means to git: its kind, executable bit and raw bytes."""
    if stat.S_ISLNK(mode):
        return ("link", hashlib.blake2b(os.fsencode(os.readlink(path))).hexdigest())
    if stat.S_ISREG(mode):
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "blake2b").hexdigest()
        return ("file", bool(mode & 0o111), digest)
    return ("dir",) if stat.S_ISDIR(mode) else ("other", stat.S_IFMT(mode))


def _stat_key(info: os.stat_result) -> tuple:
    return (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_dev)


@dataclass
class _Entry:
    key: tuple | None
    content: tuple | None
    racy: bool


@dataclass
class Fingerprint:
    """Stat data and a raw-byte hash per file, to detect lasting changes.

    `changed` rehashes only files whose stat data changed, plus "racily clean"
    files whose mtime is within timestamp granularity of when they were hashed.
    A write followed by a restore to identical bytes is not detected.
    """

    RACY_NS = 2_000_000_000  # covers coarse (FAT, network) timestamps

    entries: dict[str, _Entry] = field(default_factory=dict)

    @classmethod
    def take(cls, root: Path, names) -> "Fingerprint":
        names = list(names)
        # Parent directories are entries too, so one replaced by a symlink is a change.
        parents = {"/".join(parts[:depth]) for parts in (name.split("/") for name in names)
                   for depth in range(1, len(parts))}
        return cls({name: _read(Path(root), name) for name in [*sorted(parents), *names]})

    def changed(self, root: Path) -> list[str]:
        """Names whose content, kind or executable bit differs, or that are gone."""
        root = Path(root)
        changed = []
        for name, entry in self.entries.items():
            try:
                if entry.key == _stat_key((root / name).lstat()) and not entry.racy:
                    continue
            except OSError:
                pass
            current = _read(root, name)
            if current.content != entry.content:
                changed.append(name)
            else:
                self.entries[name] = current
        return sorted(changed)


def _read(root: Path, name: str) -> _Entry:
    started = time.time_ns()
    path = root / name
    try:
        info = path.lstat()
        content = _content(path, info.st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return _Entry(None, None, False)
    except OSError:  # e.g. made unreadable: no longer what was recorded
        return _Entry(("unreadable",), ("unreadable",), True)
    return _Entry(_stat_key(info), content, info.st_mtime_ns >= started - Fingerprint.RACY_NS)


def _check_root(root: Path) -> None:
    if os.path.realpath(root) != str(root) or not root.is_dir():
        raise RuntimeError(f"Copies folder was moved or replaced by a symlink: {root}")


def _check_copy(path: Path) -> None:
    _check_root(path.parent)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"Copy was removed or replaced: {path}")


def _plain_git(*args: str, cwd: Path, input: bytes | None = None) -> bytes:
    result = subprocess.run(["git", *_SAFE, *args], cwd=cwd, input=input, capture_output=True,
                            env=git_env(), start_new_session=True)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {os.fsdecode(result.stderr).strip()}")
    return result.stdout


def _records(output: bytes) -> list[str]:
    return [os.fsdecode(record) for record in output.split(b"\0") if record]


def _remove_tree(path: Path) -> None:
    """Remove a tree, including read-only or unreadable folders, without following symlinks."""
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    for directory, dirs, _files in os.walk(path):  # never follows symlinks
        for name in dirs:
            target = os.path.join(directory, name)
            if not os.path.islink(target):
                try:
                    os.chmod(target, 0o700)
                except OSError:
                    pass
    shutil.rmtree(path)


def _private_index_dir() -> Path:
    """A per-process folder (mode 0700) for harness indexes, away from any copy."""
    global _index_dir
    if _index_dir is None or not _index_dir.is_dir():
        import tempfile

        import atexit

        _index_dir = Path(tempfile.mkdtemp(prefix="autohelix-index-"))
        atexit.register(shutil.rmtree, _index_dir, ignore_errors=True)
    return _index_dir


class Copy:
    """A fresh private folder holding one fixed tree, with its own small `.git`.

    `files` is the tree materialized in the folder; `head` is the commit its
    private HEAD and index point at, so plain `git diff` there shows the change.
    The copies folder (`path.parent`) is the run's; git discovery should stop
    at it (see `env`). Its `.git` lists branches, tags, remotes and only the
    given `refs` (the run's own, plus its group's).
    """

    def __init__(self, repo: Repo, path: Path, files: str, head: str, refs: tuple[str, ...] = ()):
        self.repo, self.path, self.files, self.head = repo, path, files, head
        self.refs = tuple(refs)
        self.working_dir = path / repo.prefix if repo.prefix else path
        # The harness index lives outside the copies folder, where agents can't plant a
        # symlink that would make git write through it into the user's index.
        self.index = _private_index_dir() / (hashlib.sha256(os.fsencode(path)).hexdigest()[:16] + ".index")
        self.gitlinks: list[str] = []
        self.fingerprint = Fingerprint()

    @property
    def env(self) -> dict[str, str]:
        """Variables for processes run in the copy: if the agent deletes the
        copy's `.git`, discovery must not climb into the user's repository."""
        return {"GIT_CEILING_DIRECTORIES": str(self.path.parent)}

    @classmethod
    def create(cls, repo: Repo, path: Path, *, files: str, head: str, refs=()) -> "Copy":
        path = Path(path)
        _check_root(path.parent)
        if path.exists() or path.is_symlink():  # left over from an interrupted segment
            remove_copy(path)
        files = repo.out("rev-parse", "--verify", files)
        head = repo.out("rev-parse", "--verify", head + "^{commit}")
        copy = cls(repo, path, files, head, refs)
        path.mkdir()
        try:
            copy.index.unlink(missing_ok=True)
            repo.git("read-tree", "-u", "--reset", files, work_tree=path, index=copy.index)
            names = []
            for record in _records(repo.git("ls-files", "-s", "-z", index=copy.index).stdout):
                metadata, name = record.split("\t", 1)
                (copy.gitlinks if metadata.startswith(GITLINK + " ") else names).append(name)
            copy._init_private_git()
            copy.fingerprint = Fingerprint.take(path, names)
        except BaseException:
            try:
                copy.remove()
            except Exception as e:  # noqa: BLE001 - keep the original error
                logger.warning("Could not remove incomplete copy %s: %s", path, e)
            raise
        return copy

    def _init_private_git(self) -> None:
        repo, path = self.repo, self.path
        private = path / ".git"
        _plain_git("-c", "init.defaultRefFormat=files", "init",
                   "-q", "--template=", f"--object-format={repo.object_format}", str(path), cwd=path)
        (private / "objects" / "info" / "alternates").write_text(f"{repo.objects}\n")
        refs = repo.git("for-each-ref", "--format=%(objectname) %(refname)",
                        "refs/heads", "refs/tags", "refs/remotes", *self.refs).stdout
        (private / "packed-refs").write_bytes(refs)
        (private / "HEAD").write_text(f"{self.head}\n")
        listing = repo.git("config", "-z", "--get-regexp", _COPIED_CONFIG, check=False).stdout
        values = dict(record.partition("\n")[::2] for record in _records(listing))  # last wins
        for key, value in values.items():
            _plain_git("config", "--file", str(private / "config"), key, value, cwd=path)
        user = repo.objects.parent / "info" / "exclude"
        (private / "info").mkdir(exist_ok=True)
        (private / "info" / "exclude").write_text(
            (user.read_text() if user.is_file() else "")
            + "".join(f"\n/{repo.prefix}{name}/" for name in _UNFROZEN) + "\n")
        # The private index starts stat-valid for `files`, then points at `head`.
        shutil.copyfile(self.index, private / "index")
        if self.files != self.head:  # entries unchanged between the two keep their stat data
            repo.git("read-tree", "-m", "-i", self.files, self.head, index=private / "index")

    def snapshot(self, scope: Scope) -> tuple[str, list[str]]:
        """The candidate tree: the copy's files with out-of-scope changes reset to
        `files` in the harness index only. Returns (tree, discarded names)."""
        _check_copy(self.path)
        repo = self.repo
        for name in self.gitlinks:  # git ignores writes inside an uninitialized submodule
            directory = self.path / name
            if directory.is_dir() and any(directory.iterdir()):
                raise CandidateRejected(f"submodule changes are not supported: {name}")
        attributes = [f"--attr-source={self.files}"] if repo.attr_source else []
        self._check_index()
        unfrozen = [f"{repo.prefix}{name}" for name in _UNFROZEN]
        try:
            repo.git(*attributes, "add", "-A", "--", ".", *repo.unfrozen_excludes(self.path, self.index),
                     work_tree=self.path, index=self.index)
        except RuntimeError as e:  # e.g. an unreadable file or an embedded repo without commits
            raise CandidateRejected(f"could not snapshot the candidate: {e}") from e
        changes = self._changes()
        # Reset to `files` in the index only; git drops new entries that conflict with a restored path.
        reset = [f"{old_mode} {old_oid}\t{name}" for name, (old_mode, old_oid) in changes.items()
                 if not scope.allows(name) or any(name == d or name.startswith(d + "/") for d in unfrozen)]
        if reset:
            repo.git("update-index", "-z", "--index-info", index=self.index,
                     input=b"".join(os.fsencode(line) + b"\0" for line in reset))
        tree = repo.out("write-tree", index=self.index)
        return tree, sorted(changes.keys() - (self._changes() if reset else changes).keys())

    def _changes(self) -> dict[str, tuple[str, str]]:
        """Index entries that differ from `files`: name -> (mode, oid) in `files`."""
        records = _records(self.repo.git("diff-index", "--cached", "--raw", "-z", self.files,
                                          index=self.index).stdout)
        changes = {}
        for metadata, name in zip(records[::2], records[1::2]):
            old_mode, new_mode, old_oid, _ = metadata.lstrip(":").split(" ", 3)
            if GITLINK in (old_mode, new_mode):
                raise CandidateRejected(f"submodule changes are not supported: {name}")
            changes[name] = (old_mode, old_oid)
        return changes

    def changed(self) -> list[str]:
        """Names of `files` modified or deleted since the copy was created."""
        _check_copy(self.path)
        return self.fingerprint.changed(self.path)

    def _check_index(self) -> None:
        if self.index.is_symlink() or (self.index.exists() and not self.index.is_file()):
            raise RuntimeError(f"Harness index was replaced: {self.index}")

    def remove(self) -> None:
        """Delete the folder and its harness index without following symlinks out."""
        try:
            remove_copy(self.path)
        finally:
            self.index.unlink(missing_ok=True)


def remove_copy(path: Path) -> None:
    """Delete a copy folder and its harness index without following symlinks out."""
    path = Path(path)
    _check_root(path.parent)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        _remove_tree(path)
