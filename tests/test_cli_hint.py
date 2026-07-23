"""Tests for the `autohelix hint` CLI command."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from autohelix.cli import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def initialized_project(tmp_path):
    """A project with .autohelix/notes/ already set up."""
    notes = tmp_path / ".autohelix" / "notes"
    notes.mkdir(parents=True)
    return tmp_path


class TestHintCommand:
    def test_appends_hint(self, runner, initialized_project):
        result = runner.invoke(main, ["hint", "--path", str(initialized_project), "try numpy"])
        assert result.exit_code == 0, result.output
        hints = (initialized_project / ".autohelix" / "hints.md").read_text()
        assert "try numpy" in hints

    def test_multiple_hints_appended(self, runner, initialized_project):
        runner.invoke(main, ["hint", "--path", str(initialized_project), "first"])
        runner.invoke(main, ["hint", "--path", str(initialized_project), "second"])
        hints = (initialized_project / ".autohelix" / "hints.md").read_text()
        assert "first" in hints
        assert "second" in hints
        assert hints.count("\n") == 2

    def test_hint_has_timestamp(self, runner, initialized_project):
        runner.invoke(main, ["hint", "--path", str(initialized_project), "test"])
        hints = (initialized_project / ".autohelix" / "hints.md").read_text()
        # Format: - [YYYY-MM-DD HH:MM] message
        assert hints.startswith("- [")
        assert "] test\n" in hints

    def test_hint_fails_without_init(self, runner, tmp_path):
        result = runner.invoke(main, ["hint", "--path", str(tmp_path), "nope"])
        assert result.exit_code == 1
