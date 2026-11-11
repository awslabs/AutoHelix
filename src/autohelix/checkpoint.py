# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Immutable content checkpoints, scoped writes, and pinned Git plumbing."""

import fnmatch
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PinnedGit:
    """A captured Git context that never rediscovers a repository from cwd."""

    git_dir: Path
    root: Path

    @classmethod
    def discover(cls, cwd: Path, *, inherit=True):
        inherited = os.environ.get("AUTOHELIX_GIT_DIR")
        root = os.environ.get("AUTOHELIX_WORK_TREE")
        if inherit and inherited and root and Path(cwd).resolve().is_relative_to(Path(root).resolve()):
            return cls(Path(inherited).resolve(), Path(root).resolve())
        result = subprocess.run(["git", "rev-parse", "--absolute-git-dir", "--show-toplevel"],
                                cwd=cwd, capture_output=True, text=True)
        if result.returncode:
            return None
        git_dir, root = result.stdout.strip().splitlines()
        return cls(Path(git_dir).resolve(), Path(root).resolve())

    def run(self, *args, cwd=None, check=True, index=None, input=None, text=True):
        self.validate()
        env = os.environ.copy()
        # Do not let a command's inherited Git environment override this context.
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            env.pop(key, None)
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        result = subprocess.run(
            ["git", "--git-dir", str(self.git_dir), "--work-tree", str(self.root),
             "-c", "gc.auto=0", *args],
            cwd=cwd or self.root, env=env, capture_output=True, text=text,
            input=input, start_new_session=True,
        )
        if check and result.returncode:
            detail = result.stderr or result.stdout
            if not text:
                detail = os.fsdecode(detail)
            raise RuntimeError(f"git {' '.join(args)} failed: {detail.strip()}")
        return result

    def validate(self):
        if (not self.root.is_dir() or self.root.resolve() != self.root
                or not self.git_dir.is_dir() or self.git_dir.resolve() != self.git_dir):
            raise RuntimeError("Saved worktree or Git directory was removed or replaced")

    @contextmanager
    def index(self, tree):
        with tempfile.TemporaryDirectory(prefix="autohelix-index-") as directory:
            index = Path(directory) / "index"
            self.run("read-tree", tree, index=index)
            yield index

    def env(self):
        return {"AUTOHELIX_GIT_DIR": str(self.git_dir), "AUTOHELIX_WORK_TREE": str(self.root)}


def path_in_scope(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern in (".", "./"):
            return True
        prefix = pattern.rstrip("/")
        if path == prefix or path.startswith(prefix + "/") or fnmatch.fnmatch(path, pattern):
            return True
    return False


@dataclass(frozen=True)
class Scope:
    """Write paths relative to a project, within its repository boundary."""

    project: Path
    root: Path
    editable: tuple[str, ...] = ()
    frozen: tuple[str, ...] = ()
    read_only: bool = False

    def allows(self, name):
        if self.read_only:
            return False
        path = self.root / name
        if not path.is_relative_to(self.project):
            return False
        relative = path.relative_to(self.project).as_posix()
        return ((not self.editable or path_in_scope(relative, list(self.editable)))
                and not path_in_scope(relative, list(self.frozen)))


class CandidateChanged(RuntimeError):
    """A judge's verdict no longer applies to the candidate it inspected."""


@dataclass(frozen=True)
class Content:
    mode: int
    digest: str
    value: bytes


class ContentCheckpoint:
    """Capture raw content against a fixed inventory, independent of HEAD/index.

    Git's immutable tree supplies tracked paths; a private index discovers new
    nonignored paths. Raw fingerprints retain changes hidden by clean filters.
    Submodules retain both their gitlink/HEAD and nested dirty contents.
    """

    def __init__(self, cwd, *, git=None, tip=None, allowed=(), include_untracked=True):
        self.git = git if git is not None else PinnedGit.discover(cwd)
        self.root = self.git.root if self.git else Path(cwd).resolve()
        self.tip = tip or (
            os.environ.get("AUTOHELIX_CANDIDATE_TIP")
            if self.git and os.environ.get("AUTOHELIX_WORK_TREE") == str(self.root) else None
        )
        if self.git and not self.tip:
            head = self.git.run("rev-parse", "--verify", "HEAD", check=False)
            # A standalone judge may inspect a freshly initialized repository.
            # Freeze its initial cached inventory as a tree, including staged
            # ignored files, without requiring a commit.
            self.tip = (head.stdout.strip() if head.returncode == 0
                        else self.git.run("write-tree").stdout.strip())
        self.allowed = {Path(p).parent.resolve() / Path(p).name for p in allowed}
        self.include_untracked = include_untracked
        self.submodules = {}
        self.before = self.capture()

    def _names(self):
        if self.git:
            with self.git.index(self.tip) as index:
                result = self.git.run("ls-files", "-z", "--cached",
                                      *(["--others", "--exclude-standard"] if self.include_untracked else []),
                                      index=index, text=False)
            return {os.fsdecode(name) for name in result.stdout.split(b"\0") if name}
        names = set()
        for directory, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if d not in (".git", ".autohelix")]
            for name in files:
                names.add(str((Path(directory) / name).relative_to(self.root)))
            for name in dirs[:]:
                path = Path(directory) / name
                if path.is_symlink():
                    names.add(str(path.relative_to(self.root)))
                    dirs.remove(name)
        return names

    def capture(self):
        contents = {}
        for name in self._names():
            path = self.root / name
            if ".autohelix" in Path(name).parts or path.parent.resolve() / path.name in self.allowed:
                continue
            try:
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    value = os.fsencode(os.readlink(path))
                elif stat.S_ISREG(mode):
                    value = path.read_bytes()
                elif stat.S_ISDIR(mode) and self.git:
                    # Capture a child context once, before it can lose its marker.
                    if name not in self.submodules:
                        child = PinnedGit.discover(path, inherit=False) if (path / ".git").exists() else None
                        self.submodules[name] = (
                            ContentCheckpoint(path, git=child, allowed=self.allowed)
                            if child and child.root == path.resolve() else None)
                    child = self.submodules[name]
                    if child is None and (path / ".git").exists():
                        context = PinnedGit.discover(path, inherit=False)
                        if not context or context.root != path.resolve():
                            raise RuntimeError(f"Could not verify initialized submodule: {name}")
                        child = ContentCheckpoint(path, git=context, allowed=self.allowed)
                        self.submodules[name] = child
                    link = self.git.run("ls-tree", self.tip, "--", name, text=False).stdout
                    if child:
                        head = child.git.run("rev-parse", "--verify", "HEAD", check=False).stdout
                        nested = child.capture()
                        value = link + head.encode() + repr(
                            [(p, c.mode if c else None, c.digest if c else None)
                             for p, c in sorted(nested.items())]).encode()
                    else:
                        value = link
                else:
                    raise RuntimeError(f"Unsupported candidate file: {path}")
                contents[name] = Content(mode, hashlib.sha256(value).hexdigest(), value)
            except FileNotFoundError:
                contents[name] = None
        return contents

    def changed(self, after=None):
        after = self.capture() if after is None else after
        return sorted(name for name in self.before.keys() | after.keys()
                      if self.before.get(name) != after.get(name))

    def outside(self, scope):
        return [name for name in self.changed() if not scope.allows(name)]

    def restore(self, names):
        if self.git:
            self.git.validate()
        elif self.root.resolve() != self.root:
            raise RuntimeError("Candidate directory was replaced")
        for name in names:
            path = self.root / name
            for parent in path.parents:
                if parent == self.root:
                    break
                if parent.is_symlink():
                    raise RuntimeError(f"Cannot restore through changed parent symlink: {parent}")
            content = self.before.get(name)
            if content and stat.S_ISDIR(content.mode):
                raise RuntimeError(f"Cannot restore protected submodule change: {name}")
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            if content is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                if stat.S_ISLNK(content.mode):
                    path.symlink_to(os.fsdecode(content.value))
                else:
                    path.write_bytes(content.value)
                    path.chmod(stat.S_IMODE(content.mode))

    def verify_tree(self, tree, contents, scope=None):
        """Verify that checking out this tree recreates validated candidate bytes.

        Clean filters may hide raw edits in an otherwise unchanged tree. Apply
        the corresponding checkout filters when comparing, preserving valid
        roundtrips while rejecting filtered dirt and incomplete gitlinks.
        """
        entries = {}
        for record in self.git.run("ls-tree", "-rz", tree, text=False).stdout.split(b"\0"):
            if record:
                metadata, name = record.split(b"\t", 1)
                mode, _, oid = metadata.split()
                entries[os.fsdecode(name)] = (mode, oid.decode())
        names = entries.keys() | contents.keys()
        for name in names:
            path = self.root / name
            if (".autohelix" in Path(name).parts or path.parent.resolve() / path.name in self.allowed
                    or scope and not scope.allows(name)):
                continue
            entry, content = entries.get(name), contents.get(name)
            if not entry and content is None:
                continue
            if not entry or content is None:
                raise RuntimeError(f"Accepted tree cannot recreate candidate file {name}")
            mode, oid = entry
            if mode == b"160000":
                child = self.submodules.get(name)
                if child:
                    head = child.git.run("rev-parse", "HEAD").stdout.strip()
                    if head != oid:
                        raise RuntimeError(f"Cannot accept dirty submodule {name}; commit its candidate contents first")
                    committed = ContentCheckpoint(child.root, git=child.git, tip=head, allowed=self.allowed)
                    try:
                        committed.verify_tree(head, committed.before)
                    except RuntimeError as e:
                        raise RuntimeError(f"Cannot accept dirty submodule {name}: {e}") from e
                continue
            symlink = mode == b"120000"
            if symlink != stat.S_ISLNK(content.mode) or (
                    not symlink and (not stat.S_ISREG(content.mode)
                                     or bool(content.mode & 0o111) != (mode == b"100755"))):
                raise RuntimeError(f"Accepted tree cannot recreate candidate mode for {name}")
            expected = (self.git.run("cat-file", "blob", oid, text=False).stdout if symlink
                        else self.git.run("cat-file", "--filters", f"{tree}:{name}", text=False).stdout)
            if expected != content.value:
                raise RuntimeError(f"Accepted tree cannot recreate validated candidate bytes for {name}")

    def commit(self, scope, message, *, validated=None):
        """Build the accepted tree from files, bypassing HEAD, index and hooks."""
        outside = self.outside(scope)
        if outside:
            raise RuntimeError("Changes outside write scope after validation: " + ", ".join(outside[:10]))
        validated = self.capture() if validated is None else validated
        if self.capture() != validated:
            raise RuntimeError("Candidate changed after validation")
        with self.git.index(self.tip) as index:
            # Stage only the candidate paths, never an agent's staged extras.
            names = [name for name in self._names() if scope.allows(name)
                     and ".autohelix" not in Path(name).parts]
            if names:
                self.git.run("add", "-A", "--", *(f":(literal){name}" for name in names), index=index)
            tree = self.git.run("write-tree", index=index).stdout.strip()
            self.verify_tree(tree, validated, scope)
            if self.capture() != validated:
                raise RuntimeError("Candidate changed while building accepted tree")
            original = self.git.run("rev-parse", f"{self.tip}^{{tree}}").stdout.strip()
            if tree == original:
                return None
            return self.git.run("commit-tree", tree, "-p", self.tip, "-m", message).stdout.strip()


class ContentGuard:
    """One checkpoint and read-only scope per actual judge call."""

    def __init__(self, cwd, *, allowed=(), git=None, tip=None, scope=None):
        self.checkpoint = ContentCheckpoint(cwd, git=git, tip=tip, allowed=allowed)
        self.scope = scope or Scope(self.checkpoint.root, self.checkpoint.root, read_only=True)
        self.changed = []

    def __enter__(self): return self

    def __exit__(self, _type, error, _traceback):
        try:
            self.changed = self.checkpoint.outside(self.scope)
            detail = ("Judge changed candidate files: " + ", ".join(self.changed[:10])) if self.changed else None
        except Exception as e:
            detail = f"Could not verify candidate after judge: {e}"
        if detail:
            if isinstance(error, KeyboardInterrupt):
                error.candidate_changed = True
                return False
            raise CandidateChanged(detail)
