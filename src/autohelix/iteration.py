# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Worktree lifecycle for one iteration, independent of its agent and checks."""

from autohelix.sandbox import Sandbox
from autohelix.runs import RunState, critical_section, git
from autohelix.checkpoint import Scope


class IterationWorkspace:
    """Coordinate iteration mechanics using the sandbox's git/file primitives."""

    def __init__(self, sandbox: Sandbox, iteration: int, run: RunState, history):
        self._sandbox = sandbox
        self._run = run
        self._history = history
        self.worktree = sandbox.create_worktree(iteration, run.expected_tip)
        self._editable: list[str] | None = None
        self._scope = Scope(self.worktree.working_dir.resolve(), self.worktree.path.resolve())
        self.published = False
        self.recorded = False
        self.record_write_attempted = False
        self.validated = None
        self._artifacts = set()

    def prepare(self) -> None:
        """Seed agent-facing state and ensure the agent's notes directory exists."""
        self._sandbox.prepare_worktree(self.worktree)
        notes_dir = self.worktree.working_dir / ".autohelix" / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)

    def normalize(self) -> tuple[int, bool]:
        """Reopen file changes and restore the harness-owned ref and HEAD."""
        return self._sandbox.normalize(self.worktree, self._run.expected_tip)

    def enforce_scope(self, editable: list[str], frozen: list[str]) -> list[str]:
        """Resolve and retain the acceptance scope, reverting changes outside it."""
        self._scope = Scope(self.worktree.working_dir.resolve(), self.worktree.path.resolve(),
                            tuple(editable), tuple(frozen))
        changed = self.worktree.checkpoint.outside(self._scope)
        self.worktree.checkpoint.restore(changed)
        return changed

    def accept(self, message: str, result) -> str | None:
        """Publish the validated commit and its record as one critical section."""
        run = self._run
        message += f"\n\nAutoHelix-Run: {run.paths.run_id}\nAutoHelix-Iteration: {self.worktree.iteration}"
        with critical_section():
            commit = self.worktree.checkpoint.commit(self._scope, message, validated=self.validated)
            if commit:
                git(run.paths.project, "update-ref", "--no-deref", "-m",
                    f"AutoHelix accept iteration {self.worktree.iteration}",
                    run.paths.ref, commit, run.expected_tip)
                self.published = True
            result.commit = commit
            self.record_write_attempted = True
            self._history.append(result)
            self.recorded = True
            if commit:
                run.expected_tip = commit
        return commit

    def validate(self):
        self.worktree.checkpoint.allowed.update(self._artifacts)
        self.validated = self.worktree.checkpoint.capture()

    def prepare_checks(self, captures):
        """Exempt only new, declared, out-of-scope check artifacts from publication."""
        checkpoint = self.worktree.checkpoint
        inventory = checkpoint.before.keys() | checkpoint.capture().keys()
        for capture in captures:
            path = self.worktree.working_dir / capture
            path = path.parent.resolve() / path.name
            if not path.is_relative_to(checkpoint.root):
                continue
            name = path.relative_to(checkpoint.root).as_posix()
            if name not in inventory and not self._scope.allows(name):
                self._artifacts.add(path)

    def close(self) -> None:
        """Save ungated notes before removing the iteration's worktree."""
        self._sandbox.save_notes(self.worktree)
        self._sandbox.discard_worktree(self.worktree)
