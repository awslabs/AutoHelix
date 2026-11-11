# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""One iteration's copies: agent copy, frozen tree, evaluation copies, accept.

Every phase runs in its own fresh copy (see `autohelix.workspace`). The agent's
copy is snapshotted to a tree T through the user's repository; checks, the
reviewer and stop commands each get a new copy; accepting publishes exactly T.
"""

import shutil
from pathlib import Path

from autohelix.runs import RunPaths, RunState, critical_section, ensure_copies
from autohelix.workspace import Copy, Repo, Scope


def latest_review(paths: RunPaths) -> Path | None:
    """The highest-numbered reviews/iter-N.md, or None."""
    best: tuple[int, Path] | None = None
    for path in paths.reviews.glob("iter-*.md") if paths.reviews.exists() else ():
        try:
            n = int(path.stem.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        if best is None or n > best[0]:
            best = (n, path)
    return best[1] if best else None


def prepare_state(paths: RunPaths, copy, *, review: bool = True) -> None:
    """Seed a copy's `.autohelix/` with agent-facing state.

    `notes/` round-trips (see `save_notes`); the rest is read-only reference:
    `prompt.md`, `history.jsonl`, `hints.md`, `observations/`, `logs/`,
    `peer_notes/` and, with `review`, the latest review as `review.md`. Copies are
    anything with a `working_dir`. Shared project state remains reachable by
    absolute path; this is not access isolation."""
    if not paths.run_dir.exists():
        return
    state = copy.working_dir / ".autohelix"
    state.mkdir(parents=True, exist_ok=True)
    for name in ("prompt.md", "history.jsonl", "hints.md"):
        source = paths.prompt if name == "prompt.md" else paths.run_dir / name
        if source.exists():
            shutil.copy2(source, state / name)
    # peer_notes/ holds symlinks to peers' shared notes; copytree follows them.
    for name in ("notes", "observations", "logs", "peer_notes"):
        source = paths.peer_notes if name == "peer_notes" else paths.run_dir / name
        if source.exists():
            shutil.copytree(source, state / name, dirs_exist_ok=True)
    if review and (latest := latest_review(paths)):
        shutil.copy2(latest, state / "review.md")


def save_notes(paths: RunPaths, copy) -> None:
    """Copy `notes/` from a copy back to the run, the only state that round-trips."""
    source = copy.working_dir / ".autohelix" / "notes"
    if not source.is_dir():
        return
    paths.notes.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        target = paths.notes / item.name
        if item.is_file():
            shutil.copy2(item, target)
        elif item.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target)


class IterationWorkspace:
    """Create, snapshot, evaluate and publish one iteration's candidate."""

    def __init__(self, iteration: int, run: RunState, history, repo: Repo, warn=None):
        self._run = run
        self._history = history
        self._warn = warn or (lambda message: None)
        self.repo = repo
        self.iteration = iteration
        self.base = run.expected_tip  # A
        self.tree: str | None = None  # T
        self.summary: str | None = None
        self.copies: list[Copy] = []
        self.agent: Copy | None = None
        self.result = None  # the accepted result, once recorded
        self.recorded = False
        self.record_write_attempted = False

    def _copy(self, role: str, files: str, head: str) -> Copy:
        path = ensure_copies(self._run.copies_root) / f"iter-{self.iteration}-{role}"
        copy = Copy.create(self.repo, path, files=files, head=head)
        self.copies.append(copy)
        return copy

    def agent_copy(self) -> Copy:
        """A's files with agent-facing state seeded under `.autohelix/`."""
        self.agent = self._copy("agent", self.base, self.base)
        prepare_state(self._run.paths, self.agent)
        (self.agent.working_dir / ".autohelix" / "notes").mkdir(parents=True, exist_ok=True)
        return self.agent

    def freeze(self, scope: Scope) -> list[str]:
        """Snapshot the agent's copy to T, then save its notes and delete it.
        Returns the discarded out-of-scope names."""
        copy = self.agent
        try:
            tree, discarded = copy.snapshot(scope)
            # One pin keeps the frozen tree alive; the next freeze overwrites it.
            self.repo.git("update-ref", self._run.paths.pin, tree)
            summary = copy.working_dir / ".autohelix" / "commit_summary.txt"
            if summary.is_file() and not summary.is_symlink():
                self.summary = summary.read_text(errors="replace").strip().split("\n")[0] or None
        finally:
            self._release_agent()
        self.tree = tree
        return discarded

    def unchanged(self) -> None:
        """Evaluate A itself (the baseline): T is A's tree."""
        self.tree = self.base

    def eval_copy(self, role: str) -> Copy:
        """A fresh copy of T with HEAD at A, plus the complete change as a diff."""
        copy = self._copy(role, self.tree, self.base)
        state = copy.working_dir / ".autohelix"
        if role != "checks":
            prepare_state(self._run.paths, copy, review=False)
        state.mkdir(parents=True, exist_ok=True)
        diff = self.repo.git("diff", "--no-ext-diff", "--no-textconv", self.base, self.tree)
        (state / "candidate.diff").write_bytes(diff.stdout)
        return copy

    def stop_copy(self) -> Copy:
        """Files and HEAD at the accepted tip (A for a no-op)."""
        tip = self._run.expected_tip
        return self._copy("stop", tip, tip)

    def accept(self, message: str, result) -> str | None:
        """Publish T as a commit on A, then record it, as one critical section."""
        run = self._run
        message += f"\n\nAutoHelix-Run: {run.paths.run_id}\nAutoHelix-Iteration: {self.iteration}\n"
        original = self.repo.out("rev-parse", f"{self.base}^{{tree}}")
        commit = None
        if self.tree != original:
            commit = self.repo.out("commit-tree", self.tree, "-p", self.base, "-F", "-",
                                   input=message.encode())
        with critical_section():
            if commit:
                self.repo.git("update-ref", "--no-deref", "-m",
                              f"AutoHelix accept iteration {self.iteration}",
                              run.paths.ref, commit, self.base)
            result.commit = commit
            self.record_write_attempted = True
            self._history.append(result)
            self.recorded, self.result = True, result
            if commit:
                run.expected_tip = commit
        return commit

    def record(self, result) -> None:
        """Write the final record: update an accepted one, else append it once. A
        failed append may have torn the history's tail; the next owner repairs it."""
        if self.recorded:
            self._history.update(result)
        elif not self.record_write_attempted:
            self._history.append(result)

    def remove(self, copy: Copy) -> None:
        """Delete a copy; a failure is logged and retried at the next segment."""
        try:
            copy.remove()
        except Exception as e:  # noqa: BLE001 - never mask the iteration's outcome
            self._warn(f"Could not remove {copy.path}: {e}")
        finally:
            if copy in self.copies:
                self.copies.remove(copy)

    def _release_agent(self) -> None:
        copy, self.agent = self.agent, None
        if copy is None:
            return
        try:
            save_notes(self._run.paths, copy)
        except Exception as e:  # noqa: BLE001
            self._warn(f"Could not save notes from {copy.path}: {e}")
        self.remove(copy)

    def close(self) -> None:
        """Save notes if the agent copy is still open, then delete every copy."""
        self._release_agent()
        for copy in list(self.copies):
            self.remove(copy)
