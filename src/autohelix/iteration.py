# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""One iteration's copies: agent copy, frozen tree, evaluation copies, accept.

Every phase runs in its own fresh copy (see `autohelix.workspace`). The agent's
copy is snapshotted to a tree T through the user's repository; checks and stop
commands each get a new copy; accepting publishes exactly T.
"""

import filecmp
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

from autohelix.runs import RunPaths, RunState, critical_section, ensure_copies, group_members
from autohelix.workspace import ITERATION_DIR, Copy, Repo, Scope, remove_copy


def _copy_notes(notes: Path, target: Path) -> None:
    """Copy a run's saved notes. The `notes` link is resolved once, so a save switching
    it meanwhile can't mix two versions."""
    try:
        version = notes.resolve(strict=True)
    except OSError:
        return
    if version.is_dir():
        shutil.copytree(version, target, dirs_exist_ok=True)


def prepare_state(paths: RunPaths, copy, *, members=(), prompt: str | None = None) -> None:
    """Seed a copy's `.autohelix-iteration/` with agent-facing material.

    `notes/` round-trips (see `save_notes`); the rest is read-only reference:
    `prompt.md` (this iteration's prompt), `history.jsonl`, `hints.md`,
    `observations/`, `logs/`, and for each other run of the group (`members`, as `group_members` returns them)
    its `run.json`, `history.jsonl` and `notes/` under `runs/<id>/`. Copies are
    anything with a `working_dir`. Project state remains reachable by absolute
    path; this is not access isolation."""
    state = copy.working_dir / ITERATION_DIR
    state.mkdir(parents=True, exist_ok=True)
    if prompt is not None:
        (state / "prompt.md").write_text(prompt)
    if not paths.run_dir.exists():
        return
    for source in (paths.history, paths.hints):
        if source.exists():
            shutil.copy2(source, state / source.name)
    _copy_notes(paths.notes, state / "notes")
    (state / "notes").mkdir(exist_ok=True)
    for name in ("observations", "logs"):
        source = paths.run_dir / name
        if source.is_dir():
            shutil.copytree(source, state / name, dirs_exist_ok=True)
    for member, _ in members:  # another run's state: deleted or unreadable must not stop this run
        target = state / "runs" / member.run_id
        try:
            target.mkdir(parents=True, exist_ok=True)
            for source in (member.identity, member.history):
                if source.is_file():
                    shutil.copy2(source, target / source.name)
            _copy_notes(member.notes, target / "notes")
        except (OSError, shutil.Error):  # e.g. deleted meanwhile
            shutil.rmtree(target, ignore_errors=True)


def _unused(directory: Path, name: str) -> Path:
    """`name`, else `name-2`, `name-3`, ...: the first not taken in `directory`."""
    path, k = directory / name, 1
    while os.path.lexists(path):
        k += 1
        path = directory / f"{name}-{k}"
    return path


def save_notes(paths: RunPaths, copy, iteration: int) -> None:
    """Save a copy's `notes/` as a new version, `runs/<id>/notes.<iteration>/`, then
    point the `runs/<id>/notes` link at it in one rename. A note the agent deleted
    stays deleted, and readers (other runs of the group) never see a half-saved mix.
    Every version is kept. Symlinks the agent left in its notes are not saved."""
    state = copy.working_dir / ITERATION_DIR
    if state.is_symlink() or not state.is_dir():  # replaced by the agent: nothing trustworthy to save
        return
    source = state / "notes"
    if source.is_symlink():
        return
    try:
        previous = paths.notes.resolve(strict=True) if paths.notes.is_symlink() else None
    except OSError:
        previous = None
    version = _unused(paths.run_dir, f"notes.{iteration}")

    def link_or_copy(src, dst):
        # Files unchanged since the previous version are hard links to it, so keeping
        # every version costs only what changed.
        old = previous / os.path.relpath(src, source) if previous else None
        try:
            if old and old.is_file() and not old.is_symlink() and filecmp.cmp(src, old, shallow=False):
                os.link(old, dst)
                return dst
        except OSError:
            pass
        return shutil.copy2(src, dst)

    if source.is_dir():
        shutil.copytree(source, version, symlinks=True, copy_function=link_or_copy,
                        ignore=lambda folder, names: [n for n in names if os.path.islink(os.path.join(folder, n))])
    else:  # the agent removed its notes folder: an empty version, not the old notes again
        version.mkdir()
    link = paths.notes
    if link.is_dir() and not link.is_symlink():  # saved before notes had versions
        link.rename(_unused(paths.run_dir, "notes.old"))
    temporary = paths.run_dir / f"notes.{os.getpid()}.link"
    temporary.unlink(missing_ok=True)
    os.symlink(version.name, temporary)
    os.replace(temporary, link)


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
        self.prompt: str | None = None
        # The group as of this iteration's start: what its copies show of other runs.
        self.members = group_members(run.paths, run.data)
        self.refs = (run.paths.ref, *(paths.ref for paths, _ in self.members))
        self._calls = 0  # per-command folders made so far (`call_folder`)
        self._call_folders: list[Path] = []
        self._candidate: tuple[str, str] | None = None  # (T, its tree hash)

    def _copy(self, role: str, files: str, head: str) -> Copy:
        path = ensure_copies(self._run.copies_root) / f"iter-{self.iteration}-{role}"
        copy = Copy.create(self.repo, path, files=files, head=head, refs=self.refs)
        self.copies.append(copy)
        return copy

    def agent_copy(self) -> Copy:
        """A's files with agent-facing material seeded under `.autohelix-iteration/`."""
        self.agent = self._copy("agent", self.base, self.base)
        prepare_state(self._run.paths, self.agent, members=self.members)
        return self.agent

    def write_prompt(self, prompt: str) -> None:
        """Keep this iteration's prompt as `.autohelix-iteration/prompt.md` in the agent's copy."""
        self.prompt = prompt
        (self.agent.working_dir / ITERATION_DIR / "prompt.md").write_text(prompt)

    def freeze(self, scope: Scope, pinned=None) -> list[str]:
        """Snapshot the agent's copy to T, then save its notes and delete it.
        Returns the discarded out-of-scope names; `pinned()` runs once the pin holds T."""
        copy = self.agent
        try:
            tree, discarded = copy.snapshot(scope)
            # One pin keeps the frozen tree alive; the next freeze overwrites it.
            self.repo.git("update-ref", self._run.paths.pin, tree)
            if pinned:
                pinned()
            summary = copy.working_dir / ITERATION_DIR / "commit_summary.txt"
            if summary.is_file() and not summary.is_symlink() and not summary.parent.is_symlink():
                self.summary = summary.read_text(errors="replace").strip().split("\n")[0] or None
        finally:
            self._release_agent()
        self.tree = tree
        return discarded

    def unchanged(self) -> None:
        """Evaluate A itself (the baseline): T is A's tree."""
        self.tree = self.base

    def candidate(self) -> str:
        """T as a tree hash (at the baseline, A's tree): `$AUTOHELIX_CANDIDATE`."""
        if self._candidate is None or self._candidate[0] != self.tree:
            self._candidate = (self.tree, self.repo.out("rev-parse", f"{self.tree}^{{tree}}"))
        return self._candidate[1]

    @contextmanager
    def call_folder(self, **sources: Path | None):
        """A private folder for one command, `iter-N-call-K/` under the run's copies
        folder, removed when the command ends (also on an error or interrupt): an empty
        `scratch/`, plus for each named source a copy of that folder under the same name
        (an empty folder if the source doesn't exist; no entry for None). Yields
        {"scratch": path, <name>: path, ...}. A copy keeps what the command reads from
        run state fixed, whatever the command or the agent writes meanwhile."""
        self._calls += 1
        folder = ensure_copies(self._run.copies_root) / f"iter-{self.iteration}-call-{self._calls}"
        if os.path.lexists(folder):  # left by a crashed process
            remove_copy(folder)
        folder.mkdir(mode=0o700)
        self._call_folders.append(folder)
        try:
            paths = {"scratch": folder / "scratch"}
            paths["scratch"].mkdir()
            for name, source in sources.items():
                if source is None:
                    continue
                target = paths[name] = folder / name
                if source.is_dir() and not source.is_symlink():
                    shutil.copytree(source, target, symlinks=True)
                else:
                    target.mkdir()
            yield paths
        finally:
            self._remove_call_folder(folder)

    def _remove_call_folder(self, folder: Path) -> None:
        try:
            remove_copy(folder)
        except Exception as e:  # noqa: BLE001 - never mask the command's outcome
            self._warn(f"Could not remove {folder}: {e}")
        finally:
            if folder in self._call_folders:
                self._call_folders.remove(folder)

    def eval_copy(self, role: str) -> Copy:
        """A fresh copy of T with HEAD at A, plus the complete change (new files
        included, which plain `git diff` there misses) as
        `.autohelix-iteration/candidate.diff`, and nothing else of the harness's."""
        copy = self._copy(role, self.tree, self.base)
        state = copy.working_dir / ITERATION_DIR
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
            save_notes(self._run.paths, copy, self.iteration)
        except Exception as e:  # noqa: BLE001
            self._warn(f"Could not save notes from {copy.path}: {e}")
        self.remove(copy)

    def close(self) -> None:
        """Save notes if the agent copy is still open, then delete every copy."""
        self._release_agent()
        for copy in list(self.copies):
            self.remove(copy)
        for folder in list(self._call_folders):
            self._remove_call_folder(folder)
