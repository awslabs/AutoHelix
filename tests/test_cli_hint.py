"""Tests for the `autohelix hint` CLI command (and the run a command without an id acts on)."""

import os
import subprocess
import time

import pytest
from click.testing import CliRunner

from autohelix.cli import main
from autohelix.runs import SCHEMA, RunLock, RunPaths, write_json


@pytest.fixture
def runner():
    return CliRunner()


def add_run(project, run_id, *, created_at=None, archived=False):
    paths = RunPaths(project, run_id)
    paths.run_dir.mkdir(parents=True)
    write_json(paths.identity, {"schema": SCHEMA, "id": run_id, "created_at": created_at or time.time(),
                                **({"archived": True} if archived else {})})
    return paths


@pytest.fixture
def initialized_project(tmp_path):
    """A project with one run."""
    add_run(tmp_path, "test-1234")
    return tmp_path


class TestHintCommand:
    def test_appends_hint(self, runner, initialized_project):
        result = runner.invoke(main, ["hint", "--path", str(initialized_project), "try numpy"])
        assert result.exit_code == 0, result.output
        hints = RunPaths(initialized_project, "test-1234").hints.read_text()
        assert "try numpy" in hints

    def test_names_the_run_it_wrote_to(self, runner, initialized_project):
        result = runner.invoke(main, ["hint", "--path", str(initialized_project), "try numpy"])
        assert "Hint added to run 1234 (not running; read by its next `autohelix resume 1234`)." in result.output
        assert "Run 1234 (the most recently active run" in result.output  # the choice is said (stderr)

    def test_multiple_hints_appended(self, runner, initialized_project):
        runner.invoke(main, ["hint", "--path", str(initialized_project), "first"])
        runner.invoke(main, ["hint", "--path", str(initialized_project), "second"])
        hints = RunPaths(initialized_project, "test-1234").hints.read_text()
        assert "first" in hints
        assert "second" in hints
        assert hints.count("\n") == 2

    def test_hint_has_timestamp(self, runner, initialized_project):
        runner.invoke(main, ["hint", "--path", str(initialized_project), "test"])
        hints = RunPaths(initialized_project, "test-1234").hints.read_text()
        # Format: - [YYYY-MM-DD HH:MM] message
        assert hints.startswith("- [")
        assert "] test\n" in hints

    def test_hint_fails_without_init(self, runner, tmp_path):
        result = runner.invoke(main, ["hint", "--path", str(tmp_path), "nope"])
        assert result.exit_code == 1


def test_without_runs_no_hint_is_written(runner, tmp_path):
    (tmp_path / ".autohelix").mkdir()
    result = runner.invoke(main, ["hint", "--path", str(tmp_path), "nope"])
    assert result.exit_code == 1
    assert f"No runs in {tmp_path.resolve()}; write a config with `autohelix setup`" in result.output
    assert not list((tmp_path / ".autohelix").rglob("hints.md"))


def test_an_old_current_pointer_is_not_followed(runner, tmp_path):
    """The archived run an old `.internal/current` names is not the hint's run (it used to be)."""
    add_run(tmp_path, "older-aaaa", created_at=1000)
    add_run(tmp_path, "newer-bbbb", created_at=2000, archived=True)
    pointer = RunPaths(tmp_path).project_internal / "current"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text("newer-bbbb\n")
    result = runner.invoke(main, ["hint", "--path", str(tmp_path), "faster"])
    assert result.exit_code == 0, result.output
    assert RunPaths(tmp_path, "older-aaaa").hints.read_text().endswith("] faster\n")
    assert not RunPaths(tmp_path, "newer-bbbb").hints.exists()


def test_every_run_archived_says_so(runner, tmp_path):
    add_run(tmp_path, "older-aaaa", archived=True)
    result = runner.invoke(main, ["hint", "--path", str(tmp_path), "x"])
    assert result.exit_code == 1
    assert "every run is archived" in result.output


def test_the_running_run_comes_before_a_more_recent_one(runner, tmp_path):
    running = add_run(tmp_path, "older-aaaa", created_at=1000)
    add_run(tmp_path, "newer-bbbb", created_at=2000)
    with RunLock(running.lock):
        result = runner.invoke(main, ["hint", "--path", str(tmp_path), "x"])
    assert result.exit_code == 0, result.output
    assert "Hint added to run aaaa." in result.output and "(the running run" in result.output
    assert running.hints.exists()


def test_several_running_runs_must_be_named(runner, tmp_path):
    first, second = add_run(tmp_path, "one-aaaa"), add_run(tmp_path, "two-bbbb")
    with RunLock(first.lock), RunLock(second.lock):
        result = runner.invoke(main, ["hint", "--path", str(tmp_path), "x"])
        assert result.exit_code == 1
        assert "Several runs are running: " in result.output and "name one" in result.output
        named = runner.invoke(main, ["hint", "--path", str(tmp_path), "--run", "bbbb", "x"])
    assert named.exit_code == 0, named.output
    assert second.hints.exists() and not first.hints.exists()


def test_from_a_subfolder_the_project_above_is_used(runner, tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    add_run(tmp_path, "test-1234")
    (tmp_path / "src" / "deep").mkdir(parents=True)
    before = os.getcwd()
    os.chdir(tmp_path / "src" / "deep")
    try:
        result = runner.invoke(main, ["hint", "x"])
    finally:
        os.chdir(before)
    assert result.exit_code == 0, result.output
    assert "Using project ../.." in result.output
    assert RunPaths(tmp_path, "test-1234").hints.exists()
