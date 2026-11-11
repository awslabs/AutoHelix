"""Tests for autohelix.sandbox module."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from types import SimpleNamespace

from autohelix.iteration import prepare_state, save_notes
from autohelix.runs import RunPaths
from autohelix.sandbox import build_worktree_env, ensure_editable_files_ready
from autohelix.workspace import Repo, path_in_scope


# ── path_in_scope ───────────────────────────────────────────────────────


class TestPathInScope:
    def test_directory_prefix_matches_on_boundary(self):
        assert path_in_scope("src/eval.py", ["src"])
        assert path_in_scope("src/eval.py", ["src/"])

    def test_sibling_prefix_does_not_match(self):
        # The old startswith() bug let src_vendor/ slip through as in-scope.
        assert not path_in_scope("src_vendor/eval.py", ["src"])

    def test_dot_means_whole_project(self):
        # editable: ["."] used to match nothing -> everything reverted -> no-op loop.
        assert path_in_scope("anything/at/all.py", ["."])
        assert path_in_scope("top.py", ["./"])

    def test_glob_pattern(self):
        assert path_in_scope("src/a.py", ["src/*.py"])
        assert not path_in_scope("docs/a.md", ["src/*.py"])

    def test_no_patterns_matches_nothing(self):
        assert not path_in_scope("src/a.py", [])


# ── build_worktree_env ──────────────────────────────────────────────────


class TestBuildWorktreeEnv:
    def test_remaps_project_paths(self, tmp_path):
        project = tmp_path / "project"
        worktree = tmp_path / "worktree"
        project.mkdir()
        worktree.mkdir()

        fake_sys_path = [
            str(project / "src"),
            str(project / "lib"),
            "/unrelated/path",
        ]
        with patch.object(sys, "path", fake_sys_path):
            env = build_worktree_env(project, worktree)

        parts = env["PYTHONPATH"].split(":")
        assert str(worktree / "src") in parts
        assert str(worktree / "lib") in parts
        assert "/unrelated/path" not in parts

    def test_no_remap_when_no_project_paths(self, tmp_path):
        project = tmp_path / "project"
        worktree = tmp_path / "worktree"
        project.mkdir()
        worktree.mkdir()

        with patch.object(sys, "path", ["/other/path"]):
            env = build_worktree_env(project, worktree)

        # PYTHONPATH should not be set (or unchanged from env)
        assert "PYTHONPATH" not in env or env["PYTHONPATH"] == os.environ.get("PYTHONPATH", "")

    def test_excludes_autohelix_paths(self, tmp_path):
        project = tmp_path / "project"
        worktree = tmp_path / "worktree"
        project.mkdir()
        worktree.mkdir()

        fake_sys_path = [
            str(project / "src"),
            str(project / ".autohelix" / "runtime" / "bin"),
        ]
        with patch.object(sys, "path", fake_sys_path), \
             patch.dict(os.environ, {"PYTHONPATH": ""}, clear=False):
            env = build_worktree_env(project, worktree)

        # Only the src path should be remapped, not the .autohelix path
        remapped_autohelix = str(worktree / ".autohelix")
        parts = [p for p in env.get("PYTHONPATH", "").split(":") if p]
        assert str(worktree / "src") in parts
        assert not any(p.startswith(remapped_autohelix) for p in parts)

    def test_does_not_exclude_autohelix_prefix_in_other_names(self, tmp_path):
        """Paths like .autohelix-backup should NOT be excluded."""
        project = tmp_path / "project"
        worktree = tmp_path / "worktree"
        project.mkdir()
        worktree.mkdir()

        fake_sys_path = [
            str(project / ".autohelix-backup" / "src"),
        ]
        with patch.object(sys, "path", fake_sys_path), \
             patch.dict(os.environ, {"PYTHONPATH": ""}, clear=False):
            env = build_worktree_env(project, worktree)

        parts = [p for p in env.get("PYTHONPATH", "").split(":") if p]
        assert str(worktree / ".autohelix-backup" / "src") in parts

    def test_matches_only_on_a_path_boundary(self, tmp_path):
        project = tmp_path / "project"
        worktree = tmp_path / "worktree"
        with patch.object(sys, "path", [str(tmp_path / "project2" / "src"), str(project)]), \
             patch.dict(os.environ, {"PYTHONPATH": ""}):
            env = build_worktree_env(project, worktree)
        assert env["PYTHONPATH"] == str(worktree)

    def test_skips_installed_packages_and_relative_entries(self, tmp_path):
        project = tmp_path / "project"
        worktree = tmp_path / "worktree"
        fake_sys_path = ["", "src", str(project / ".venv" / "lib" / "python3.12" / "site-packages"),
                         str(project / "usr" / "lib" / "python3" / "dist-packages"), str(project / "src")]
        with patch.object(sys, "path", fake_sys_path), patch.dict(os.environ, {"PYTHONPATH": ""}):
            env = build_worktree_env(project, worktree)
        assert env["PYTHONPATH"] == str(worktree / "src")

    def test_project_inside_another_projects_state_is_remapped(self, tmp_path):
        """A nested project may itself sit under some .autohelix/ folder."""
        project = tmp_path / "outer" / ".autohelix" / "inner"
        worktree = tmp_path / "worktree"
        with patch.object(sys, "path", [str(project / "src"), str(project / ".autohelix" / "x")]), \
             patch.dict(os.environ, {"PYTHONPATH": ""}):
            env = build_worktree_env(project, worktree)
        assert env["PYTHONPATH"] == str(worktree / "src")

    def test_unresolved_entries_of_a_resolved_project(self, tmp_path):
        real = tmp_path / "real"
        (real / "src").mkdir(parents=True)
        (tmp_path / "link").symlink_to(real)
        worktree = tmp_path / "worktree"
        with patch.object(sys, "path", [str(tmp_path / "link" / "src")]), \
             patch.dict(os.environ, {"PYTHONPATH": ""}):
            env = build_worktree_env(real.resolve(), worktree)
        assert env["PYTHONPATH"] == str(worktree / "src")

    def test_preserves_existing_pythonpath(self, tmp_path):
        project = tmp_path / "project"
        worktree = tmp_path / "worktree"
        project.mkdir()
        worktree.mkdir()

        fake_sys_path = [str(project / "src")]
        with patch.object(sys, "path", fake_sys_path), \
             patch.dict(os.environ, {"PYTHONPATH": "/existing"}):
            env = build_worktree_env(project, worktree)

        assert env["PYTHONPATH"].endswith(":/existing")
        assert str(worktree / "src") in env["PYTHONPATH"]


# ── Helper to create a git repo ────────────────────────────────────────


def _init_git_repo(path: Path) -> None:
    """Initialize a git repo with an initial commit."""
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, capture_output=True, check=True)
    # Create an initial file and commit
    (path / "hello.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "-A"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, capture_output=True, check=True)


@pytest.fixture
def git_project(tmp_path):
    """A tmp directory with an initialized git repo."""
    project = tmp_path / "project"
    project.mkdir()
    _init_git_repo(project)
    return project


def _fake_copy(project: Path, iteration: int) -> SimpleNamespace:
    """Anything with a working_dir stands in for an iteration copy."""
    working_dir = project.parent / f"copy-{iteration}"
    working_dir.mkdir(exist_ok=True)
    return SimpleNamespace(working_dir=working_dir)


# ── prepare_state / save_notes ────────────────


class TestPrepareWorktree:
    def test_copies_notes_and_reference_to_worktree(self, git_project):
        wt = _fake_copy(git_project, 1)

        ah = git_project / ".autohelix"
        # notes/ (agent-owned, round-trips)
        notes_dir = ah / "notes"
        notes_dir.mkdir(parents=True)
        (notes_dir / "iter1.md").write_text("some notes\n")
        # hints.md + observations/ (read-only reference)
        (ah / "hints.md").write_text("try harder\n")
        obs = ah / "observations" / "iter-0"
        obs.mkdir(parents=True)
        (obs / "stdout.txt").write_text("baseline\n")
        # A prior review — the LATEST should be materialized as review.md
        reviews = ah / "reviews"
        reviews.mkdir()
        (reviews / "iter-1.md").write_text("old review\n")
        (reviews / "iter-2.md").write_text("latest review\n")

        prepare_state(RunPaths(git_project), wt)

        wt_ah = wt.working_dir / ".autohelix"
        assert (wt_ah / "notes" / "iter1.md").read_text() == "some notes\n"
        assert (wt_ah / "hints.md").read_text() == "try harder\n"
        assert (wt_ah / "observations" / "iter-0" / "stdout.txt").exists()
        # Latest review materialized as transient review.md; archive NOT copied
        assert (wt_ah / "review.md").read_text() == "latest review\n"
        assert not (wt_ah / "reviews").exists()

    def test_prepare_worktree_copies_agent_state_only(self, git_project):
        wt = _fake_copy(git_project, 1)

        ah = git_project / ".autohelix"
        ah.mkdir(parents=True, exist_ok=True)
        # These should be copied
        (ah / "prompt.md").write_text("Goal: {{goal}}\n")
        (ah / "history.jsonl").write_text("{}\n")
        (ah / "notes").mkdir()
        (ah / "notes" / "iter-1.md").write_text("test note\n")
        # logs/ should be copied; runtime/output should NOT
        (ah / "runtime").mkdir(exist_ok=True)
        (ah / "logs").mkdir(exist_ok=True)
        (ah / "logs" / "iter-1").mkdir()
        (ah / "logs" / "iter-1" / "agent.log").write_text("log\n")
        (ah / "output").mkdir(exist_ok=True)

        prepare_state(RunPaths(git_project), wt)

        wt_ah = wt.working_dir / ".autohelix"
        assert (wt_ah / "prompt.md").exists()
        assert (wt_ah / "history.jsonl").exists()
        assert (wt_ah / "notes" / "iter-1.md").exists()
        assert (wt_ah / "logs" / "iter-1" / "agent.log").exists()
        assert not (wt_ah / "runtime").exists()
        assert not (wt_ah / "output").exists()

    def test_prepare_worktree_no_autohelix_dir(self, git_project):
        wt = _fake_copy(git_project, 1)

        # Ensure .autohelix doesn't exist in main (remove if created by sandbox)
        import shutil
        ah = git_project / ".autohelix"
        if ah.exists():
            shutil.rmtree(ah)

        # Should not raise
        prepare_state(RunPaths(git_project), wt)


class TestSaveNotes:
    def test_saves_notes_back(self, git_project):
        wt = _fake_copy(git_project, 1)

        # Create notes in worktree
        wt_notes = wt.working_dir / ".autohelix" / "notes"
        wt_notes.mkdir(parents=True)
        (wt_notes / "iter1.md").write_text("new notes\n")

        save_notes(RunPaths(git_project), wt)

        saved = git_project / ".autohelix" / "notes" / "iter1.md"
        assert saved.exists()
        assert saved.read_text() == "new notes\n"

    def test_save_notes_no_worktree_notes(self, git_project):
        wt = _fake_copy(git_project, 1)

        # No notes in worktree — should not raise
        save_notes(RunPaths(git_project), wt)


# ── Sandbox: ensure_editable_files_ready ─────────────────────────────────


class TestEnsureEditableFilesReady:
    def test_passes_with_clean_editable_files(self, git_project):
        # hello.py exists and is committed
        ensure_editable_files_ready(Repo(git_project), git_project, ["hello.py"])

    def test_raises_on_missing_pattern(self, git_project):
        with pytest.raises(RuntimeError, match="No files found"):
            ensure_editable_files_ready(Repo(git_project), git_project, ["nonexistent*.py"])

    def test_raises_on_uncommitted_changes(self, git_project):
        (git_project / "hello.py").write_text("dirty\n")

        with pytest.raises(RuntimeError, match="Uncommitted changes"):
            ensure_editable_files_ready(Repo(git_project), git_project, ["hello.py"])

    def test_empty_editable_is_noop(self, git_project):
        # Should not raise
        ensure_editable_files_ready(Repo(git_project), git_project, [])


# ── Sandbox: resolve_editable ──────────────────────────────────────────
