# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Git worktree management for isolated iterations."""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from autohelix.runs import RunPaths
from autohelix.checkpoint import ContentCheckpoint, PinnedGit, Scope, path_in_scope


def build_worktree_env(project_path: Path, worktree_path: Path) -> dict[str, str]:
    """Build environment with PYTHONPATH remapped from project to worktree.

    When using editable installs (pip install -e), .pth files add the project's
    source directories to sys.path. In worktrees, we need to remap those paths
    so Python imports from the worktree, not the main repo.
    """
    env = os.environ.copy()
    project_str = str(project_path)
    worktree_str = str(worktree_path)

    # The inherited PWD points at wherever `autohelix run` was launched (the main
    # repo). Some agents (e.g. opencode) resolve their working directory from
    # $PWD rather than the process cwd, so they would operate in the main repo
    # instead of the worktree — editing the wrong files and failing the merge.
    # Pin PWD to the worktree to match the subprocess cwd.
    env["PWD"] = worktree_str

    # Find sys.path entries under the project directory and remap to worktree
    remapped_paths = []
    for p in sys.path:
        if p.startswith(project_str) and "/.autohelix/" not in p and not p.endswith("/.autohelix"):
            relative = p[len(project_str):]
            remapped = worktree_str + relative
            remapped_paths.append(remapped)

    if remapped_paths:
        existing = env.get("PYTHONPATH", "")
        new_pythonpath = ":".join(remapped_paths)
        env["PYTHONPATH"] = f"{new_pythonpath}:{existing}" if existing else new_pythonpath

    return env


@dataclass
class Worktree:
    """Represents a git worktree for an iteration."""

    path: Path  # Root of the worktree
    branch: str
    iteration: int
    working_dir: Path  # Project directory within worktree (may differ if subdirectory)
    base_commit: str  # Commit the iteration branched from
    git: PinnedGit
    checkpoint: ContentCheckpoint


class Sandbox:
    """Manages git worktrees for isolated iterations."""

    def __init__(self, project_path: Path, paths: RunPaths | None = None):
        self.project_path = project_path
        self.paths = paths or RunPaths(project_path)
        self.git = PinnedGit.discover(project_path, inherit=False)
        self._contexts = {}
        self.worktrees_dir = self.paths.worktrees
        # Get subdirectory path relative to repo root (empty string if at root)
        result = self._run_git("rev-parse", "--show-prefix")
        self.repo_prefix = result.stdout.strip() if result.returncode == 0 else ""

    def _run_git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        """Run a git command."""
        context = self.context(cwd or self.project_path)
        return context.run(*args, cwd=cwd or self.project_path, check=False)

    def context(self, cwd: Path) -> PinnedGit:
        # Select the saved context lexically. Resolving an agent-replaced
        # worktree symlink here could select the user's checkout instead.
        cwd = Path(cwd).absolute()
        for root, context in self._contexts.items():
            if cwd.is_relative_to(root):
                return context
        if cwd.is_relative_to(self.worktrees_dir.resolve()):
            raise RuntimeError(f"No saved Git context for worktree {cwd}")
        return self.git

    def _run_git_checked(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        """Run a git command and raise on failure."""
        result = self._run_git(*args, cwd=cwd)
        if result.returncode != 0:
            command = " ".join(["git", "-c", "gc.auto=0", *args])
            error = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"{command} failed: {error}")
        return result

    def _tracked_changes_outside_autohelix(self, cwd: Path | None = None) -> list[str]:
        """Return tracked changes outside .autohelix for the given worktree."""
        result = self._run_git("status", "--short", "--untracked-files=no", cwd=cwd)
        changes: list[str] = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            path = line[3:] if len(line) >= 4 else line
            if path == ".autohelix" or path.startswith(".autohelix/"):
                continue
            changes.append(line)
        return changes

    def resolve_editable(
        self, editable: list[str], frozen: list[str], cwd: Path | None = None,
    ) -> list[str] | None:
        """Resolve the effective editable file list.

        - If editable is set: return it as-is (whitelist mode).
        - If frozen is set: return all changed files that don't match frozen patterns
          (blacklist mode). Uses the worktree cwd to find changed files.
        - If neither is set: return None (no scope restriction).
        """
        if editable:
            return editable
        if not frozen:
            return None

        target = cwd or self.project_path
        checkpoint = self._checkpoint(target)
        scope = Scope(Path(target).resolve(), checkpoint.root, frozen=tuple(frozen))
        return [str((checkpoint.root / name).relative_to(target))
                for name in checkpoint.changed() if scope.allows(name)]

    def _checkpoint(self, cwd):
        context = self.context(cwd)
        for checkpoint in getattr(self, "_checkpoints", {}).values():
            if checkpoint.git == context:
                return checkpoint
        return ContentCheckpoint(cwd, git=context)

    def revert_out_of_scope(
        self, worktree: Worktree, effective_editable: list[str],
    ) -> list[str]:
        """Revert agent changes outside the effective editable scope.

        Returns list of reverted file paths (relative to worktree).
        """
        scope = Scope(worktree.working_dir.resolve(), worktree.path.resolve(),
                      editable=tuple(effective_editable))
        # An explicitly empty editable list means no writes (resolved frozen scope).
        names = (worktree.checkpoint.changed() if not effective_editable
                 else worktree.checkpoint.outside(scope))
        worktree.checkpoint.restore(names)
        return [str((worktree.path / name).relative_to(worktree.working_dir))
                for name in names]

    def ensure_clean_working_tree(self) -> None:
        """Ensure the main repo has no uncommitted changes to tracked files."""
        result = self._run_git("status", "--porcelain")
        dirty = []
        for line in result.stdout.strip().split("\n"):
            if not line or line.strip().startswith("?"):
                continue
            path = line[3:] if len(line) >= 4 else line
            if path == ".autohelix" or path.startswith(".autohelix/"):
                continue
            dirty.append(line)
        if dirty:
            raise RuntimeError(
                "Uncommitted changes in the repo:\n"
                + "\n".join(f"  {line}" for line in dirty[:10])
                + "\n\nCommit or stash them before running AutoHelix."
            )

    def ensure_editable_files_ready(self, editable: list[str]) -> None:
        """Ensure editable files exist and have no uncommitted changes."""
        if not editable:
            return

        # Check files exist
        for pattern in editable:
            if pattern in (".", "./"):
                continue
            matches = list(self.project_path.glob(pattern))
            if not matches:
                raise RuntimeError(
                    f"No files found matching editable pattern '{pattern}'. "
                    "Check your autohelix.yaml scope.editable config."
                )

        # Check for uncommitted changes in editable files only
        result = self._run_git("status", "--porcelain", "--", *editable)
        if result.stdout.strip():
            raise RuntimeError(
                "Uncommitted changes in editable files. "
                "Commit them before running AutoHelix:\n"
                f"{result.stdout.strip()}"
            )

    def create_worktree(self, iteration: int, expected_tip: str | None = None) -> Worktree:
        """Create a detached iteration worktree at the harness's expected tip."""
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        worktree_path = self.worktrees_dir / f"iter-{iteration}"
        base_commit = expected_tip or self._run_git_checked(
            "rev-parse", self.paths.ref if self.paths.run_id else "HEAD",
        ).stdout.strip()
        registered = self._run_git_checked("worktree", "list", "--porcelain").stdout
        if (worktree_path.exists() or worktree_path.is_symlink()
                or f"worktree {worktree_path}\n" in registered):
            self.remove_worktree(worktree_path)
        self._run_git_checked("worktree", "add", "--detach", str(worktree_path), base_commit)
        working_dir = worktree_path / self.repo_prefix if self.repo_prefix else worktree_path
        context = PinnedGit.discover(worktree_path, inherit=False)
        self._contexts[worktree_path.absolute()] = context
        try:
            checkpoint = ContentCheckpoint(working_dir, git=context, tip=base_commit)
        except BaseException:
            self.remove_worktree(worktree_path)
            raise
        if not hasattr(self, "_checkpoints"):
            self._checkpoints = {}
        self._checkpoints[worktree_path.absolute()] = checkpoint
        return Worktree(worktree_path, self.paths.ref if self.paths.run_id else "", iteration,
                        working_dir, base_commit, context, checkpoint)

    def normalize(self, worktree: Worktree, expected_tip: str) -> tuple[int, bool]:
        """Detach, restore the owned ref, and reopen all file changes unconditionally."""
        restored = False
        if self.paths.run_id:
            lookup = self._run_git("rev-parse", "--verify", self.paths.ref)
            current = lookup.stdout.strip() if lookup.returncode == 0 else "0" * len(expected_tip)
            if current != expected_tip or self._run_git("symbolic-ref", "--quiet", self.paths.ref).returncode == 0:
                self._run_git_checked("update-ref", "--no-deref", "--create-reflog", "-m", "AutoHelix restore expected tip",
                                      self.paths.ref, expected_tip, current)
                restored = True
        cwd = worktree.working_dir
        head = self._run_git("rev-parse", "--verify", "HEAD", cwd=cwd)
        count = (self._run_git_checked("rev-list", "--count", f"{expected_tip}..{head.stdout.strip()}", cwd=cwd)
                 if head.returncode == 0 else None)
        self._run_git_checked("update-ref", "--no-deref", "HEAD", expected_tip, cwd=cwd)
        self._run_git_checked("reset", "--mixed", expected_tip, cwd=cwd)
        return int(count.stdout.strip()) if count else 0, restored

    def remove_worktree(self, worktree_path: Path) -> None:
        for parent in worktree_path.parents:
            if parent == self.project_path:
                break
            if parent.is_symlink():
                raise RuntimeError(f"Cannot remove worktree through changed parent symlink: {parent}")
        result = self._run_git("worktree", "remove", str(worktree_path), "--force", "--force")
        if result.returncode:
            if worktree_path.is_symlink():
                worktree_path.unlink()
            elif worktree_path.exists():
                shutil.rmtree(worktree_path)
            self._run_git_checked("worktree", "prune", "--expire", "now")
        self._contexts.pop(worktree_path.absolute(), None)
        getattr(self, "_checkpoints", {}).pop(worktree_path.absolute(), None)

    def commit_worktree(self, worktree: Worktree, editable: list[str] | None = None,
                        message: str | None = None, expected_tip: str | None = None) -> str | None:
        """Commit candidate files, without changing either the run or target ref."""
        expected_tip = expected_tip or worktree.base_commit
        if expected_tip != worktree.checkpoint.tip:
            raise RuntimeError("Candidate checkpoint does not match expected parent")
        scope = Scope(worktree.working_dir.resolve(), worktree.path.resolve(),
                      editable=tuple(editable or ()))
        if editable == [] and worktree.checkpoint.changed():
            raise RuntimeError("Changes outside write scope after validation")
        return worktree.checkpoint.commit(scope, message or f"AutoHelix iteration {worktree.iteration}")

    def discard_worktree(self, worktree: Worktree) -> None:
        """Discard a worktree without merging."""
        self.remove_worktree(worktree.path)

    def prepare_worktree(self, worktree: Worktree) -> None:
        """Seed the worktree with agent-facing state from the main project.

        Copies `notes/` (agent-owned, round-trips back via `save_notes`) plus
        read-only reference material the agent's prompt points at: `observations/`,
        `hints.md`, `logs/`, and the latest review materialized as a transient
        `review.md`. Also copies `prompt.md` and `history.jsonl`.

        Only the latest review is materialized in the intended reference view.
        Shared Git metadata and project paths remain accessible; this layout
        does not isolate a reviewer from prior state. Skips worktrees, runtime,
        output, and archive.
        """
        src_autohelix = self.paths.run_dir
        dst_autohelix = worktree.working_dir / ".autohelix"

        if not src_autohelix.exists():
            return

        dst_autohelix.mkdir(parents=True, exist_ok=True)

        # Copy top-level files the agent needs (canonical log + user hints)
        for name in ("prompt.md", "history.jsonl", "hints.md"):
            src = self.paths.prompt if name == "prompt.md" else src_autohelix / name
            if src.exists():
                shutil.copy2(src, dst_autohelix / name)

        # Copy directories the agent may read. `notes/` round-trips; the rest are
        # read-only reference (never copied back). `peer_notes/` holds symlinks to
        # peers' shared notes (parallel mode); copytree follows them so the agent
        # sees peer content.
        for dirname in ("notes", "observations", "logs", "peer_notes"):
            src_dir = self.paths.peer_notes if dirname == "peer_notes" else src_autohelix / dirname
            if src_dir.exists():
                dst_dir = dst_autohelix / dirname
                if dst_dir.exists():
                    shutil.rmtree(dst_dir)
                shutil.copytree(src_dir, dst_dir)

        # Materialize the latest review as a transient review.md the agent reads.
        latest = self._latest_review()
        dst_review = dst_autohelix / "review.md"
        if latest is not None:
            shutil.copy2(latest, dst_review)
        elif dst_review.exists():
            dst_review.unlink()

    def _latest_review(self) -> Path | None:
        """Return the highest-numbered reviews/iter-N.md, or None."""
        reviews_dir = self.paths.reviews
        if not reviews_dir.exists():
            return None
        best: tuple[int, Path] | None = None
        for p in reviews_dir.glob("iter-*.md"):
            try:
                n = int(p.stem.split("-", 1)[1])
            except (IndexError, ValueError):
                continue
            if best is None or n > best[0]:
                best = (n, p)
        return best[1] if best else None

    def save_notes(self, worktree: Worktree) -> None:
        """Copy the whole notes/ dir from the worktree back to the main project.

        `notes/` is the sole agent-owned region and the only thing that
        round-trips. Everything else the agent sees (observations, hints, the
        latest review) is read-only reference and is deliberately not copied back.
        """
        src_notes = worktree.working_dir / ".autohelix" / "notes"
        dst_notes = self.paths.notes

        if src_notes.exists():
            dst_notes.mkdir(parents=True, exist_ok=True)
            for item in src_notes.iterdir():
                dst_item = dst_notes / item.name
                if item.is_file():
                    shutil.copy2(item, dst_item)
                elif item.is_dir():
                    if dst_item.exists():
                        shutil.rmtree(dst_item)
                    shutil.copytree(item, dst_item)

    def save_review(self, worktree: Worktree, iteration: int) -> bool:
        """Archive the reviewer's output to reviews/iter-N.md.

        The reviewer writes a transient review.md in its worktree; this files it
        into the main project's reviews/ archive. There is no standing "latest
        review" file — the latest is derived from the highest iter-N.md.

        Returns True if a review was saved, False otherwise.
        """
        src_review = worktree.working_dir / ".autohelix" / "review.md"
        if not src_review.exists():
            return False

        reviews_dir = self.paths.reviews
        reviews_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_review, reviews_dir / f"iter-{iteration}.md")

        return True
