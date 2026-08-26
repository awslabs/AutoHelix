"""Tests for autohelix.sandbox module."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from autohelix.sandbox import Sandbox, Worktree, build_worktree_env, path_in_scope


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
            str(project / ".autohelix" / "worktrees" / "iter-1"),
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


# ── Sandbox: create / remove worktree ───────────────────────────────────


class TestCreateWorktree:
    def test_creates_worktree(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(1)

        assert wt.path.exists()
        assert wt.branch == "autohelix-iter-1"
        assert wt.iteration == 1
        # The worktree should contain our initial file
        assert (wt.working_dir / "hello.py").exists()

    def test_creates_worktree_cleans_stale(self, git_project):
        sandbox = Sandbox(git_project)
        wt1 = sandbox.create_worktree(1)
        assert wt1.path.exists()

        # Creating same iteration again should clean up old one
        wt2 = sandbox.create_worktree(1)
        assert wt2.path.exists()
        assert wt2.branch == "autohelix-iter-1"


class TestRemoveWorktree:
    def test_remove_worktree(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(1)
        worktree_path = wt.path

        sandbox.remove_worktree(worktree_path)
        assert not worktree_path.exists()

    def test_discard_worktree(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(2)
        worktree_path = wt.path

        sandbox.discard_worktree(wt)
        assert not worktree_path.exists()


# ── Sandbox: merge worktree ─────────────────────────────────────────────


class TestMergeWorktree:
    def test_agent_commit_is_reopened_before_scope_and_merge(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(1)

        (wt.working_dir / "hello.py").write_text("print('agent commit')\n")
        subprocess.run(["git", "add", "hello.py"], cwd=wt.working_dir, check=True)
        subprocess.run(
            ["git", "commit", "-m", "agent commits early"],
            cwd=wt.working_dir,
            check=True,
            capture_output=True,
        )

        assert sandbox.uncommit_agent_changes(wt) == 1
        assert (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=wt.working_dir,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            == wt.base_commit
        )
        assert "hello.py" in subprocess.run(
            ["git", "status", "--short"],
            cwd=wt.working_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        commit = sandbox.merge_worktree(
            wt,
            editable=["hello.py"],
            message="AutoHelix iteration 1",
        )
        assert commit is not None
        assert (git_project / "hello.py").read_text() == "print('agent commit')\n"
        assert "AutoHelix iteration 1" in subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            cwd=git_project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def test_committed_out_of_scope_change_is_reverted(self, git_project):
        sandbox = Sandbox(git_project)
        (git_project / "other.py").write_text("original\n")
        subprocess.run(["git", "add", "other.py"], cwd=git_project, check=True)
        subprocess.run(
            ["git", "commit", "-m", "add other"],
            cwd=git_project,
            check=True,
            capture_output=True,
        )
        wt = sandbox.create_worktree(1)

        (wt.working_dir / "hello.py").write_text("print('allowed')\n")
        (wt.working_dir / "other.py").write_text("out of scope\n")
        subprocess.run(["git", "add", "-A"], cwd=wt.working_dir, check=True)
        subprocess.run(
            ["git", "commit", "-m", "agent commits everything"],
            cwd=wt.working_dir,
            check=True,
            capture_output=True,
        )

        assert sandbox.uncommit_agent_changes(wt) == 1
        assert sandbox.revert_out_of_scope(wt, ["hello.py"]) == ["other.py"]
        commit = sandbox.merge_worktree(wt, editable=["hello.py"])

        assert commit is not None
        assert (git_project / "hello.py").read_text() == "print('allowed')\n"
        assert (git_project / "other.py").read_text() == "original\n"

    def test_merge_with_changes(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(1)

        # Make a change in the worktree
        (wt.working_dir / "hello.py").write_text("print('modified')\n")

        commit = sandbox.merge_worktree(wt)
        assert commit is not None

        # Verify the change is in the main branch
        content = (git_project / "hello.py").read_text()
        assert content == "print('modified')\n"

    def test_merge_no_changes_returns_none(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(1)

        # No changes made
        commit = sandbox.merge_worktree(wt)
        assert commit is None

    def test_merge_with_editable_scope(self, git_project):
        sandbox = Sandbox(git_project)

        # Create a second file
        (git_project / "other.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=git_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add other"], cwd=git_project, capture_output=True)

        wt = sandbox.create_worktree(1)

        # Modify both files in worktree
        (wt.working_dir / "hello.py").write_text("print('changed')\n")
        (wt.working_dir / "other.py").write_text("x = 2\n")

        # Only hello.py is editable
        commit = sandbox.merge_worktree(wt, editable=["hello.py"])
        assert commit is not None

        # hello.py should be merged, other.py should be unchanged
        assert (git_project / "hello.py").read_text() == "print('changed')\n"
        assert (git_project / "other.py").read_text() == "x = 1\n"

    def test_merge_with_custom_message(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(1)

        (wt.working_dir / "hello.py").write_text("print('v2')\n")

        message = "AutoHelix iteration 1\n\nspeed: 100 -> 150 (+50.0%)"
        commit = sandbox.merge_worktree(wt, message=message)
        assert commit is not None

        # Verify the commit message
        result = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            cwd=git_project,
            capture_output=True,
            text=True,
        )
        assert "speed: 100 -> 150 (+50.0%)" in result.stdout

    def test_merge_raises_on_uncommitted_main_changes(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(1)

        # Create uncommitted change in main repo
        (git_project / "hello.py").write_text("dirty\n")
        subprocess.run(["git", "add", "hello.py"], cwd=git_project, capture_output=True)

        with pytest.raises(RuntimeError, match="uncommitted changes"):
            sandbox.merge_worktree(wt)

    def test_merge_conflict_aborts_and_leaves_clean_tree(self, git_project):
        # A conflicting merge must abort so the main tree is left clean —
        # otherwise MERGE_HEAD + conflict markers would wedge the next run.
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(1)

        # Diverge main after the worktree branched, on the same line.
        (git_project / "hello.py").write_text("main change\n")
        subprocess.run(["git", "commit", "-am", "main diverges"], cwd=git_project, capture_output=True)

        # Worktree edits the same line differently -> conflict on merge.
        (wt.working_dir / "hello.py").write_text("worktree change\n")

        with pytest.raises(RuntimeError, match="Failed to merge"):
            sandbox.merge_worktree(wt)

        # No merge left in progress, and main content is intact (not a marker mess).
        assert not (git_project / ".git" / "MERGE_HEAD").exists()
        assert "<<<<<<<" not in (git_project / "hello.py").read_text()
        # ensure_clean_working_tree must be satisfied: no tracked, modified files.
        sandbox.ensure_clean_working_tree()


# ── Sandbox: iteration_context ──────────────────────────────────────────


# ── Sandbox: prepare_worktree / save_notes / save_review ────────────────


class TestPrepareWorktree:
    def test_copies_notes_and_reference_to_worktree(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(1)

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

        sandbox.prepare_worktree(wt)

        wt_ah = wt.working_dir / ".autohelix"
        assert (wt_ah / "notes" / "iter1.md").read_text() == "some notes\n"
        assert (wt_ah / "hints.md").read_text() == "try harder\n"
        assert (wt_ah / "observations" / "iter-0" / "stdout.txt").exists()
        # Latest review materialized as transient review.md; archive NOT copied
        assert (wt_ah / "review.md").read_text() == "latest review\n"
        assert not (wt_ah / "reviews").exists()

    def test_prepare_worktree_copies_agent_state_only(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(1)

        ah = git_project / ".autohelix"
        ah.mkdir(parents=True, exist_ok=True)
        # These should be copied
        (ah / "prompt.md").write_text("Goal: {{goal}}\n")
        (ah / "history.jsonl").write_text("{}\n")
        (ah / "notes").mkdir()
        (ah / "notes" / "iter-1.md").write_text("test note\n")
        # logs/ should be copied; worktrees/runtime/output should NOT
        (ah / "worktrees").mkdir(exist_ok=True)
        (ah / "runtime").mkdir(exist_ok=True)
        (ah / "logs").mkdir(exist_ok=True)
        (ah / "logs" / "iter-1").mkdir()
        (ah / "logs" / "iter-1" / "agent.log").write_text("log\n")
        (ah / "output").mkdir(exist_ok=True)

        sandbox.prepare_worktree(wt)

        wt_ah = wt.working_dir / ".autohelix"
        assert (wt_ah / "prompt.md").exists()
        assert (wt_ah / "history.jsonl").exists()
        assert (wt_ah / "notes" / "iter-1.md").exists()
        assert (wt_ah / "logs" / "iter-1" / "agent.log").exists()
        assert not (wt_ah / "worktrees").exists()
        assert not (wt_ah / "runtime").exists()
        assert not (wt_ah / "output").exists()

    def test_prepare_worktree_no_autohelix_dir(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(1)

        # Ensure .autohelix doesn't exist in main (remove if created by sandbox)
        import shutil
        ah = git_project / ".autohelix"
        if ah.exists():
            shutil.rmtree(ah)

        # Should not raise
        sandbox.prepare_worktree(wt)


class TestSaveNotes:
    def test_saves_notes_back(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(1)

        # Create notes in worktree
        wt_notes = wt.working_dir / ".autohelix" / "notes"
        wt_notes.mkdir(parents=True)
        (wt_notes / "iter1.md").write_text("new notes\n")

        sandbox.save_notes(wt)

        saved = git_project / ".autohelix" / "notes" / "iter1.md"
        assert saved.exists()
        assert saved.read_text() == "new notes\n"

    def test_save_notes_no_worktree_notes(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(1)

        # No notes in worktree — should not raise
        sandbox.save_notes(wt)


class TestSaveReview:
    def test_saves_and_archives_review(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(3)

        # Reviewer writes a transient review.md in its worktree
        wt_ah = wt.working_dir / ".autohelix"
        wt_ah.mkdir(parents=True, exist_ok=True)
        (wt_ah / "review.md").write_text("# Review\nGood work\n")

        result = sandbox.save_review(wt, iteration=3)
        assert result is True

        # Archived to reviews/iter-N.md; no standing REVIEW.md exists
        archived = git_project / ".autohelix" / "reviews" / "iter-3.md"
        assert archived.read_text() == "# Review\nGood work\n"
        assert not (git_project / ".autohelix" / "reviews" / "REVIEW.md").exists()

    def test_no_review_returns_false(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(1)

        result = sandbox.save_review(wt, iteration=1)
        assert result is False


# ── Sandbox: ensure_editable_files_ready ─────────────────────────────────


class TestEnsureEditableFilesReady:
    def test_passes_with_clean_editable_files(self, git_project):
        sandbox = Sandbox(git_project)
        # hello.py exists and is committed
        sandbox.ensure_editable_files_ready(["hello.py"])

    def test_raises_on_missing_pattern(self, git_project):
        sandbox = Sandbox(git_project)
        with pytest.raises(RuntimeError, match="No files found"):
            sandbox.ensure_editable_files_ready(["nonexistent*.py"])

    def test_raises_on_uncommitted_changes(self, git_project):
        sandbox = Sandbox(git_project)
        (git_project / "hello.py").write_text("dirty\n")

        with pytest.raises(RuntimeError, match="Uncommitted changes"):
            sandbox.ensure_editable_files_ready(["hello.py"])

    def test_empty_editable_is_noop(self, git_project):
        sandbox = Sandbox(git_project)
        # Should not raise
        sandbox.ensure_editable_files_ready([])


# ── Sandbox: resolve_editable ──────────────────────────────────────────


class TestResolveEditable:
    def test_editable_returned_as_is(self, git_project):
        sandbox = Sandbox(git_project)
        result = sandbox.resolve_editable(["src/"], [], cwd=git_project)
        assert result == ["src/"]

    def test_neither_returns_none(self, git_project):
        sandbox = Sandbox(git_project)
        result = sandbox.resolve_editable([], [], cwd=git_project)
        assert result is None

    def test_frozen_excludes_matching_files(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(1)

        # Modify the existing file and create a new file in the worktree
        (wt.working_dir / "hello.py").write_text("modified\n")
        (wt.working_dir / "new_file.py").write_text("new\n")

        result = sandbox.resolve_editable([], ["hello.py"], cwd=wt.working_dir)
        assert "hello.py" not in result
        assert "new_file.py" in result

    def test_frozen_with_glob_pattern(self, git_project):
        sandbox = Sandbox(git_project)

        # Add a test file to the repo
        (git_project / "test_foo.py").write_text("test\n")
        subprocess.run(["git", "add", "-A"], cwd=git_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add test"], cwd=git_project, capture_output=True)

        wt = sandbox.create_worktree(1)
        (wt.working_dir / "hello.py").write_text("modified\n")
        (wt.working_dir / "test_foo.py").write_text("modified test\n")

        result = sandbox.resolve_editable([], ["test_*.py"], cwd=wt.working_dir)
        assert "hello.py" in result
        assert "test_foo.py" not in result

    def test_frozen_with_directory_pattern(self, git_project):
        # A bare-directory frozen pattern (`tests/`) must freeze everything under
        # it, matching how `editable: [tests/]` behaves. Regression guard: raw
        # fnmatch silently froze nothing for this pattern.
        sandbox = Sandbox(git_project)

        tests_dir = git_project / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_foo.py").write_text("test\n")
        subprocess.run(["git", "add", "-A"], cwd=git_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add tests"], cwd=git_project, capture_output=True)

        wt = sandbox.create_worktree(1)
        (wt.working_dir / "hello.py").write_text("modified\n")
        (wt.working_dir / "tests" / "test_foo.py").write_text("modified test\n")

        for pattern in ("tests/", "tests"):
            result = sandbox.resolve_editable([], [pattern], cwd=wt.working_dir)
            assert "hello.py" in result, pattern
            assert "tests/test_foo.py" not in result, pattern

    def test_frozen_includes_new_untracked_files(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(1)

        # Agent creates a brand new file
        (wt.working_dir / "brand_new.py").write_text("new\n")

        result = sandbox.resolve_editable([], ["hello.py"], cwd=wt.working_dir)
        assert "brand_new.py" in result


# ── Sandbox: revert_out_of_scope ───────────────────────────────────────


class TestRevertOutOfScope:
    def test_reverts_tracked_changes_outside_scope(self, git_project):
        sandbox = Sandbox(git_project)

        # Add second file
        (git_project / "other.py").write_text("original\n")
        subprocess.run(["git", "add", "-A"], cwd=git_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add other"], cwd=git_project, capture_output=True)

        wt = sandbox.create_worktree(1)
        (wt.working_dir / "hello.py").write_text("modified\n")
        (wt.working_dir / "other.py").write_text("also modified\n")

        reverted = sandbox.revert_out_of_scope(wt, ["hello.py"])

        assert "other.py" in reverted
        assert "hello.py" not in reverted
        # other.py should be back to original
        assert (wt.working_dir / "other.py").read_text() == "original\n"
        # hello.py should still be modified
        assert (wt.working_dir / "hello.py").read_text() == "modified\n"

    def test_removes_untracked_files_outside_scope(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(1)

        # Agent creates files both in and out of scope
        (wt.working_dir / "hello.py").write_text("modified\n")
        (wt.working_dir / "rogue.py").write_text("should not survive\n")

        reverted = sandbox.revert_out_of_scope(wt, ["hello.py"])

        assert "rogue.py" in reverted
        assert not (wt.working_dir / "rogue.py").exists()
        assert (wt.working_dir / "hello.py").read_text() == "modified\n"

    def test_no_reverts_when_all_in_scope(self, git_project):
        sandbox = Sandbox(git_project)
        wt = sandbox.create_worktree(1)

        (wt.working_dir / "hello.py").write_text("modified\n")

        reverted = sandbox.revert_out_of_scope(wt, ["hello.py"])
        assert reverted == []

    def test_pattern_matching_with_directory_prefix(self, git_project):
        """Editable pattern 'src/' should match files like 'src/foo.py'."""
        sandbox = Sandbox(git_project)

        # Create src directory with a file
        (git_project / "src").mkdir()
        (git_project / "src" / "foo.py").write_text("original\n")
        subprocess.run(["git", "add", "-A"], cwd=git_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add src"], cwd=git_project, capture_output=True)

        wt = sandbox.create_worktree(1)
        (wt.working_dir / "src" / "foo.py").write_text("modified\n")
        (wt.working_dir / "hello.py").write_text("also modified\n")

        reverted = sandbox.revert_out_of_scope(wt, ["src/"])

        assert "hello.py" in reverted
        assert not any("src/" in f or f == "src/foo.py" for f in reverted)

    def test_raises_when_revert_checkout_fails(self, git_project, monkeypatch):
        """A failed revert checkout must raise, not silently report success.

        Finding #2: the revert checkout used to be unchecked, so a failed
        `git checkout` (bad pathspec, locked index, odd filename) would leave the
        out-of-scope change live while the caller printed "Reverted N files".
        """
        sandbox = Sandbox(git_project)

        (git_project / "other.py").write_text("original\n")
        subprocess.run(["git", "add", "-A"], cwd=git_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add other"], cwd=git_project, capture_output=True)

        wt = sandbox.create_worktree(1)
        (wt.working_dir / "other.py").write_text("out of scope\n")

        real_run_git = sandbox._run_git

        def fake_run_git(*args, cwd=None):
            result = real_run_git(*args, cwd=cwd)
            if args and args[0] == "checkout":
                result.returncode = 1
                result.stderr = "simulated checkout failure"
            return result

        monkeypatch.setattr(sandbox, "_run_git", fake_run_git)

        with pytest.raises(RuntimeError, match="checkout"):
            sandbox.revert_out_of_scope(wt, ["hello.py"])


# ── Sandbox: subdirectory-rooted projects (repo_prefix != "") ───────────
#
# When AutoHelix runs from a subdirectory of the git repo, git reports tracked
# changes repo-root-relative while ls-files and the user's editable/frozen
# patterns are project-relative. These tests pin the `--relative` normalization
# that keeps both frames aligned. Root-level projects (every other test here)
# have repo_prefix == "" where the frames coincide.


def _init_subdir_git_repo(root: Path) -> Path:
    """Init a repo whose AutoHelix project lives in a `proj/` subdirectory.

    Returns the project directory (root/proj), committed and clean.
    """
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, capture_output=True, check=True)
    proj = root / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "tests").mkdir(parents=True)
    (proj / "src" / "good.py").write_text("v=1\n")   # in-scope for editable: [src/]
    (proj / "other.py").write_text("keep=1\n")        # out-of-scope, tracked
    (proj / "tests" / "test_it.py").write_text("assert True\n")  # frozen target
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root, capture_output=True, check=True)
    return proj


@pytest.fixture
def subdir_project(tmp_path, monkeypatch):
    """A git repo whose project is a subdirectory; cwd set to that subdir.

    Sandbox reads `git rev-parse --show-prefix` from its cwd at construction, so
    the caller must chdir into the subdir before building the Sandbox — mirroring
    how `autohelix run` launches from the project directory.
    """
    root = tmp_path / "repo"
    root.mkdir()
    proj = _init_subdir_git_repo(root)
    monkeypatch.chdir(proj)
    return proj


class TestSubdirRootedProject:
    def test_repo_prefix_and_working_dir(self, subdir_project):
        sandbox = Sandbox(subdir_project)
        assert sandbox.repo_prefix == "proj/"

        wt = sandbox.create_worktree(1)
        # working_dir is the project inside the worktree, one level below the root
        assert wt.working_dir == wt.path / "proj"
        assert (wt.working_dir / "src" / "good.py").exists()

    def test_revert_out_of_scope_whitelist(self, subdir_project):
        # The bug: tracked out-of-scope files came back repo-root-relative
        # (proj/other.py), failed path_in_scope(..., ["src/"]), and the revert
        # checkout no-oped on the doubled `proj/proj/` pathspec — leaving the
        # out-of-scope edit live and the in-scope file wrongly classed out.
        sandbox = Sandbox(subdir_project)
        wt = sandbox.create_worktree(1)

        (wt.working_dir / "src" / "good.py").write_text("v=2\n")           # in-scope
        (wt.working_dir / "other.py").write_text("keep=1\nHACKED=1\n")     # out, tracked
        (wt.working_dir / "escape.txt").write_text("untracked\n")          # out, untracked

        reverted = sandbox.revert_out_of_scope(wt, ["src/"])

        # in-scope edit preserved, out-of-scope edits actually gone
        assert (wt.working_dir / "src" / "good.py").read_text() == "v=2\n"
        assert (wt.working_dir / "other.py").read_text() == "keep=1\n"
        assert not (wt.working_dir / "escape.txt").exists()
        # returned list reflects reality (project-relative)
        assert set(reverted) == {"other.py", "escape.txt"}

    def test_frozen_mode_merges_and_excludes_frozen(self, subdir_project):
        # The bug: resolve_editable returned repo-root-relative paths in frozen
        # mode, so merge_worktree's `git add` doubled the prefix and crashed
        # every iteration with `fatal: pathspec ... did not match`.
        sandbox = Sandbox(subdir_project)
        wt = sandbox.create_worktree(1)

        (wt.working_dir / "src" / "good.py").write_text("v=2\n")
        (wt.working_dir / "tests" / "test_it.py").write_text("assert True  # weakened\n")

        effective = sandbox.resolve_editable([], ["tests/"], cwd=wt.working_dir)
        # project-relative, and the frozen file is excluded
        assert "src/good.py" in effective
        assert "tests/test_it.py" not in effective

        sandbox.revert_out_of_scope(wt, effective)
        # frozen file reverted in the worktree
        assert (wt.working_dir / "tests" / "test_it.py").read_text() == "assert True\n"

        commit = sandbox.merge_worktree(wt, editable=effective, message="iter1")
        assert commit is not None
        # in-scope change committed to main; frozen file untouched
        assert (subdir_project / "src" / "good.py").read_text() == "v=2\n"
        assert (subdir_project / "tests" / "test_it.py").read_text() == "assert True\n"

    def test_merge_whitelist_scope(self, subdir_project):
        sandbox = Sandbox(subdir_project)
        wt = sandbox.create_worktree(1)

        (wt.working_dir / "src" / "good.py").write_text("v=2\n")
        (wt.working_dir / "other.py").write_text("keep=1\nHACKED=1\n")

        commit = sandbox.merge_worktree(wt, editable=["src/"], message="iter1")
        assert commit is not None
        # only the in-scope file reached main
        assert (subdir_project / "src" / "good.py").read_text() == "v=2\n"
        assert (subdir_project / "other.py").read_text() == "keep=1\n"
