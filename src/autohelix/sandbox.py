# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Git worktree management for isolated iterations."""

import fnmatch
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


def path_in_scope(path: str, patterns: list[str]) -> bool:
    """Return True if `path` (relative, posix) is covered by any editable pattern.

    Matching is path-boundary aware, not raw string prefix: `src` covers
    `src/eval.py` but NOT `src_vendor/eval.py`. `.` / `./` mean "the whole
    project" (matches everything). Glob patterns (e.g. `src/*.py`) are honored
    via fnmatch.
    """
    for pattern in patterns:
        if pattern in (".", "./"):
            return True
        # Directory/prefix match on path boundaries: normalize the trailing slash
        # and require an exact hit or a `pattern/`-prefixed descendant.
        prefix = pattern.rstrip("/")
        if path == prefix or path.startswith(prefix + "/"):
            return True
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


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


class Sandbox:
    """Manages git worktrees for isolated iterations."""

    def __init__(self, project_path: Path):
        self.project_path = project_path
        # Named "worktrees" to match git's convention (.git/worktrees/).
        self.worktrees_dir = project_path / ".autohelix" / "worktrees"
        # Branch namespace for iteration worktrees. Defaults to "autohelix" so
        # solo runs are byte-identical. The parallel wrapper sets a per-worker
        # value (e.g. "autohelix-w2") so concurrent workers sharing one .git ref
        # store never collide on branch names or clobber each other's branches
        # during the stale-branch cleanup in create_worktree().
        self.branch_prefix = os.environ.get("AUTOHELIX_BRANCH_PREFIX", "autohelix")
        # Get subdirectory path relative to repo root (empty string if at root)
        result = self._run_git("rev-parse", "--show-prefix")
        self.repo_prefix = result.stdout.strip() if result.returncode == 0 else ""

    def _run_git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        """Run a git command."""
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.project_path,
            capture_output=True,
            text=True,
        )

    def _run_git_checked(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        """Run a git command and raise on failure."""
        result = self._run_git(*args, cwd=cwd)
        if result.returncode != 0:
            command = " ".join(["git", *args])
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

        # Blacklist mode: find all files changed by the agent, exclude frozen ones
        target = cwd or self.project_path
        # Get all changed files (staged + unstaged + untracked new files).
        # `--relative` reports paths relative to cwd (project-relative) rather than
        # repo-root-relative, so they match the user's frozen/editable patterns and
        # ls-files' frame even when the project sits in a subdirectory of the repo.
        result = self._run_git("diff", "--relative", "--name-only", "HEAD", cwd=target)
        changed = set(result.stdout.strip().splitlines()) if result.stdout.strip() else set()
        # Also include untracked files (new files the agent created)
        result = self._run_git("ls-files", "--others", "--exclude-standard", cwd=target)
        untracked = set(result.stdout.strip().splitlines()) if result.stdout.strip() else set()
        all_changed = changed | untracked

        # Filter out frozen files. Use path_in_scope (not raw fnmatch) so frozen
        # patterns match with the same directory-prefix semantics as editable —
        # e.g. `frozen: [tests/]` freezes everything under tests/, matching how
        # `editable: [tests/]` behaves. Raw fnmatch would silently freeze nothing
        # for a bare-directory pattern.
        non_frozen = []
        for f in sorted(all_changed):
            if not path_in_scope(f, frozen):
                non_frozen.append(f)
        return non_frozen if non_frozen else []

    def revert_out_of_scope(
        self, worktree: Worktree, effective_editable: list[str],
    ) -> list[str]:
        """Revert agent changes outside the effective editable scope.

        Returns list of reverted file paths (relative to worktree).
        """
        cwd = worktree.working_dir
        # Get all changed files (tracked modifications). `--relative` keeps paths
        # project-relative (matching the editable/frozen patterns and ls-files'
        # frame) so subdirectory-rooted projects classify and revert correctly.
        result = self._run_git("diff", "--relative", "--name-only", "HEAD", cwd=cwd)
        changed = set(result.stdout.strip().splitlines()) if result.stdout.strip() else set()
        # Also include untracked files
        result = self._run_git("ls-files", "--others", "--exclude-standard", cwd=cwd)
        untracked = set(result.stdout.strip().splitlines()) if result.stdout.strip() else set()

        # Determine which files are in scope
        in_scope = set()
        for f in changed | untracked:
            if path_in_scope(f, effective_editable):
                in_scope.add(f)

        # Revert tracked files outside scope. Use the checked variant: a failed
        # revert is safety-relevant (the out-of-scope change would otherwise stay
        # live for constraints/metrics), so it must reject the iteration loudly
        # rather than being reported as reverted when it was not.
        out_of_scope_tracked = sorted(changed - in_scope)
        if out_of_scope_tracked:
            self._run_git_checked("checkout", "HEAD", "--", *out_of_scope_tracked, cwd=cwd)

        # Remove untracked files outside scope
        out_of_scope_untracked = sorted(untracked - in_scope)
        for f in out_of_scope_untracked:
            filepath = cwd / f
            if filepath.exists():
                filepath.unlink()

        return out_of_scope_tracked + out_of_scope_untracked

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

    def create_worktree(self, iteration: int) -> Worktree:
        """Create a new worktree for an iteration."""
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)

        branch = f"{self.branch_prefix}-iter-{iteration}"
        worktree_path = self.worktrees_dir / f"iter-{iteration}"
        base_commit = self._run_git_checked("rev-parse", "HEAD").stdout.strip()

        # Clean up if exists from previous failed run
        if worktree_path.exists():
            self.remove_worktree(worktree_path)

        # Delete branch if it exists (stale from previous run)
        self._run_git("branch", "-D", branch)

        # Create new branch and worktree
        self._run_git("worktree", "add", "-b", branch, str(worktree_path))

        # Compute working directory within worktree
        working_dir = worktree_path / self.repo_prefix if self.repo_prefix else worktree_path

        return Worktree(
            path=worktree_path,
            branch=branch,
            iteration=iteration,
            working_dir=working_dir,
            base_commit=base_commit,
        )

    def uncommit_agent_changes(self, worktree: Worktree) -> int:
        """Turn agent-created commits back into ordinary worktree changes.

        AutoHelix must enforce scope and run validation against the complete
        candidate diff before creating its own iteration commit. An agent that
        commits inside the worktree would otherwise hide those changes from
        `git diff HEAD` and could have its accepted work discarded as a no-op.
        """
        result = self._run_git_checked(
            "rev-list",
            "--count",
            f"{worktree.base_commit}..HEAD",
            cwd=worktree.working_dir,
        )
        commit_count = int(result.stdout.strip())
        if commit_count:
            self._run_git_checked(
                "reset",
                "--mixed",
                worktree.base_commit,
                cwd=worktree.working_dir,
            )
        return commit_count

    def remove_worktree(self, worktree_path: Path) -> None:
        """Remove a worktree and its branch."""
        # Get branch name before removing
        result = self._run_git("worktree", "list", "--porcelain")
        branch = None
        lines = result.stdout.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("worktree ") and str(worktree_path) in line:
                for j in range(i, min(i + 5, len(lines))):
                    if lines[j].startswith("branch "):
                        branch = lines[j].replace("branch refs/heads/", "")
                        break
                break

        # Remove worktree
        self._run_git("worktree", "remove", str(worktree_path), "--force")

        # Clean up directory if still exists
        if worktree_path.exists():
            shutil.rmtree(worktree_path)

        # Remove branch
        if branch:
            self._run_git("branch", "-D", branch)

    def merge_worktree(self, worktree: Worktree, editable: list[str] | None = None, message: str | None = None) -> str | None:
        """Merge worktree changes back to main branch and return commit hash."""
        # Check for uncommitted changes in main repo (could indicate agent modified wrong location)
        result = self._run_git("status", "--porcelain")
        uncommitted = [
            line for line in result.stdout.strip().split("\n")
            if line and not line.strip().startswith("?")  # Ignore untracked files
        ]
        if uncommitted:
            raise RuntimeError(
                "Main repo has uncommitted changes. This can happen if:\n"
                "  - The agent modified files in the main repo instead of the worktree\n"
                "  - Python imports resolved to main repo due to editable install (.pth)\n\n"
                "The iteration's work may be in the main repo, not the worktree.\n"
                "Review with 'git diff' and commit or stash before continuing.\n\n"
                f"Changed files:\n" + "\n".join(f"  {line}" for line in uncommitted[:10])
            )

        # Only add editable files (safety: changes outside scope are discarded)
        if editable:
            self._run_git_checked(
                "add",
                "--",
                *editable,
                cwd=worktree.working_dir,
            )
        else:
            # Fallback: add everything if no editable scope defined
            self._run_git_checked(
                "add",
                "-A",
                cwd=worktree.working_dir,
            )
        if not self._tracked_changes_outside_autohelix(cwd=worktree.working_dir):
            return None
        commit_message = message or f"AutoHelix iteration {worktree.iteration}"
        self._run_git_checked(
            "commit",
            "-m",
            commit_message,
            cwd=worktree.working_dir,
        )

        # Get the commit from the worktree (full hash to avoid ambiguity)
        result = self._run_git_checked("rev-parse", "HEAD", cwd=worktree.working_dir)
        commit = result.stdout.strip()

        # Merge the worktree branch into main. A serial run branches from the
        # current main, so this is normally a trivial fast-forward; a conflict
        # means main diverged unexpectedly. Abort the half-done merge so the main
        # tree is left clean — otherwise MERGE_HEAD + conflict markers would wedge
        # the next `run`, which refuses to start on a dirty tree. The raise is
        # handled upstream as a normal iteration rejection.
        merge = self._run_git("merge", worktree.branch, "--no-edit")
        if merge.returncode != 0:
            self._run_git("merge", "--abort")
            error = (merge.stderr or merge.stdout).strip()
            raise RuntimeError(
                f"Failed to merge iteration {worktree.iteration} into the main branch "
                f"(likely a conflict — the main branch may have diverged): {error}. "
                "The merge was aborted and the main tree left clean; this iteration's "
                "work was not applied."
            )

        return commit

    def discard_worktree(self, worktree: Worktree) -> None:
        """Discard a worktree without merging."""
        self.remove_worktree(worktree.path)

    def prepare_worktree(self, worktree: Worktree) -> None:
        """Seed the worktree with agent-facing state from the main project.

        Copies `notes/` (agent-owned, round-trips back via `save_notes`) plus
        read-only reference material the agent's prompt points at: `observations/`,
        `hints.md`, `logs/`, and the latest review materialized as a transient
        `review.md`. Also copies `prompt.md` and `history.jsonl`.

        Reviews are *not* copied in as an archive — only the single latest review
        is materialized, so the reviewer (which runs in this same worktree) can't
        read prior reviews and be conditioned by them. Skips worktrees, runtime,
        output, and archive.
        """
        src_autohelix = self.project_path / ".autohelix"
        dst_autohelix = worktree.working_dir / ".autohelix"

        if not src_autohelix.exists():
            return

        dst_autohelix.mkdir(parents=True, exist_ok=True)

        # Copy top-level files the agent needs (canonical log + user hints)
        for name in ("prompt.md", "history.jsonl", "hints.md"):
            src = src_autohelix / name
            if src.exists():
                shutil.copy2(src, dst_autohelix / name)

        # Copy directories the agent may read. `notes/` round-trips; the rest are
        # read-only reference (never copied back). `peer_notes/` holds symlinks to
        # peers' shared notes (parallel mode); copytree follows them so the agent
        # sees peer content.
        for dirname in ("notes", "observations", "logs", "peer_notes"):
            src_dir = src_autohelix / dirname
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
        reviews_dir = self.project_path / ".autohelix" / "reviews"
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
        dst_notes = self.project_path / ".autohelix" / "notes"

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

        reviews_dir = self.project_path / ".autohelix" / "reviews"
        reviews_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_review, reviews_dir / f"iter-{iteration}.md")

        return True
