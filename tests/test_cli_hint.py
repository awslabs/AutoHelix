"""Tests for the `autohelix hint` CLI command."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from autohelix.cli import main
from autohelix.runs import SCHEMA, RunPaths, write_json, write_text


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def initialized_project(tmp_path):
    """A project with a selected run."""
    paths = RunPaths(tmp_path, "test-1234")
    paths.run_dir.mkdir(parents=True)
    write_json(paths.identity, {"schema": SCHEMA, "id": paths.run_id})
    write_text(paths.current, paths.run_id + "\n")
    return tmp_path


class TestHintCommand:
    def test_appends_hint(self, runner, initialized_project):
        result = runner.invoke(main, ["hint", "--path", str(initialized_project), "try numpy"])
        assert result.exit_code == 0, result.output
        hints = RunPaths(initialized_project).hints.read_text()
        assert "try numpy" in hints

    def test_multiple_hints_appended(self, runner, initialized_project):
        runner.invoke(main, ["hint", "--path", str(initialized_project), "first"])
        runner.invoke(main, ["hint", "--path", str(initialized_project), "second"])
        hints = RunPaths(initialized_project).hints.read_text()
        assert "first" in hints
        assert "second" in hints
        assert hints.count("\n") == 2

    def test_hint_has_timestamp(self, runner, initialized_project):
        runner.invoke(main, ["hint", "--path", str(initialized_project), "test"])
        hints = RunPaths(initialized_project).hints.read_text()
        # Format: - [YYYY-MM-DD HH:MM] message
        assert hints.startswith("- [")
        assert "] test\n" in hints

    def test_hint_fails_without_init(self, runner, tmp_path):
        result = runner.invoke(main, ["hint", "--path", str(tmp_path), "nope"])
        assert result.exit_code == 1


def test_initialized_without_current_does_not_create_lost_hint(runner, tmp_path):
    (tmp_path / ".autohelix").mkdir()
    result = runner.invoke(main, ["hint", "--path", str(tmp_path), "nope"])
    assert result.exit_code == 1
    assert "No current run" in result.output
    assert not (tmp_path / ".autohelix" / "hints.md").exists()
