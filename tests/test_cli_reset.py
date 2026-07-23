"""Tests for the `autohelix clear` CLI command."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from autohelix.cli import main


@pytest.fixture
def runner():
    return CliRunner()


def _setup_autohelix_dir(tmp_path: Path):
    """Create a minimal .autohelix directory with notes, reviews, history, logs, output."""
    ah = tmp_path / ".autohelix"
    ah.mkdir()

    # notes/ with content
    (ah / "notes").mkdir(parents=True)
    (ah / "notes" / "README.md").write_text("# Notes\n")
    (ah / "notes" / "iter-1.md").write_text("Learned something important.\n")
    (ah / "notes" / "iter-2.md").write_text("Another learning.\n")

    # observations/
    (ah / "observations").mkdir(parents=True)

    # reviews/ archive
    (ah / "reviews").mkdir(parents=True)
    (ah / "reviews" / "iter-1.md").write_text("# Review\n")

    # History (top-level file)
    (ah / "history.jsonl").write_text('{"iteration": 1}\n')

    # Logs
    logs = ah / "logs"
    logs.mkdir()
    (logs / "iter-1.log").write_text("log output\n")

    # Output: dashboard
    (ah / "output").mkdir()
    (ah / "output" / "dashboard.html").write_text("<html>dashboard</html>\n")

    return ah


class TestClearArchive:
    def test_archives_history(self, runner, tmp_path):
        """Clear should move history to archive."""
        ah = _setup_autohelix_dir(tmp_path)

        result = runner.invoke(main, ["clear", str(tmp_path)], input="y\n")
        assert result.exit_code == 0

        # History file should be gone from main location
        assert not (ah / "history.jsonl").exists()

        # Should exist in archive
        archive_dirs = list((ah / "archive").iterdir())
        assert len(archive_dirs) == 1
        assert (archive_dirs[0] / "history.jsonl").exists()
        assert "Archived history" in result.output

    def test_archives_notes(self, runner, tmp_path):
        """Clear should archive notes/ and create a fresh empty notes dir."""
        ah = _setup_autohelix_dir(tmp_path)

        result = runner.invoke(main, ["clear", str(tmp_path)], input="y\n")
        assert result.exit_code == 0

        # Notes dir should exist but be empty
        notes = ah / "notes"
        assert notes.is_dir()
        assert not any(notes.iterdir())

        # Archived notes should have the content
        archive_dirs = list((ah / "archive").iterdir())
        archived_notes = archive_dirs[0] / "notes"
        assert (archived_notes / "iter-1.md").exists()
        assert (archived_notes / "iter-2.md").exists()
        assert "Archived notes" in result.output

    def test_archives_review(self, runner, tmp_path):
        """Clear should archive the reviews/ dir."""
        ah = _setup_autohelix_dir(tmp_path)

        result = runner.invoke(main, ["clear", str(tmp_path)], input="y\n")
        assert result.exit_code == 0

        assert not (ah / "reviews").exists()

        archive_dirs = list((ah / "archive").iterdir())
        assert (archive_dirs[0] / "reviews" / "iter-1.md").exists()
        assert "Archived reviews" in result.output

    def test_archives_logs(self, runner, tmp_path):
        """Clear should archive logs."""
        ah = _setup_autohelix_dir(tmp_path)

        result = runner.invoke(main, ["clear", str(tmp_path)], input="y\n")
        assert result.exit_code == 0

        # Logs dir should be empty
        assert (ah / "logs").is_dir()
        assert not list((ah / "logs").glob("*.log"))

        # Archived logs should have content
        archive_dirs = list((ah / "archive").iterdir())
        assert (archive_dirs[0] / "logs" / "iter-1.log").exists()
        assert "Archived logs" in result.output

    def test_archives_output(self, runner, tmp_path):
        """Clear should archive output/ (including dashboard.html)."""
        ah = _setup_autohelix_dir(tmp_path)

        result = runner.invoke(main, ["clear", str(tmp_path)], input="y\n")
        assert result.exit_code == 0

        # Dashboard should be gone from main location
        assert not (ah / "output" / "dashboard.html").exists()

        # Should exist in archive with original content
        archive_dirs = list((ah / "archive").iterdir())
        archived = archive_dirs[0] / "output" / "dashboard.html"
        assert archived.exists()
        assert archived.read_text() == "<html>dashboard</html>\n"
        assert "Archived output" in result.output

    def test_archive_shows_path(self, runner, tmp_path):
        """Clear should show the archive path."""
        _setup_autohelix_dir(tmp_path)

        result = runner.invoke(main, ["clear", str(tmp_path)], input="y\n")
        assert result.exit_code == 0
        assert ".autohelix/archive/" in result.output

    def test_nothing_to_archive(self, runner, tmp_path):
        """Clear with empty state should not create an archive dir."""
        ah = tmp_path / ".autohelix"
        ah.mkdir()
        (ah / "notes").mkdir(parents=True)

        result = runner.invoke(main, ["clear", str(tmp_path)], input="y\n")
        assert result.exit_code == 0
        assert not (ah / "archive").exists()
        assert "Nothing to archive" in result.output

    def test_abort_on_no(self, runner, tmp_path):
        """Declining confirmation should not clear anything."""
        ah = _setup_autohelix_dir(tmp_path)

        result = runner.invoke(main, ["clear", str(tmp_path)], input="n\n")
        assert result.exit_code == 0
        assert (ah / "history.jsonl").exists()
        assert (ah / "notes" / "iter-1.md").exists()

    def test_multiple_clears_create_separate_archives(self, runner, tmp_path):
        """Each clear should create a separate timestamped archive."""
        import time

        _setup_autohelix_dir(tmp_path)
        runner.invoke(main, ["clear", str(tmp_path)], input="y\n")

        # Add some new state
        time.sleep(1)  # ensure different timestamp
        ah = tmp_path / ".autohelix"
        (ah / "history.jsonl").write_text('{"iteration": 2}\n')

        runner.invoke(main, ["clear", str(tmp_path)], input="y\n")

        archive_dirs = sorted((ah / "archive").iterdir())
        assert len(archive_dirs) == 2

    def test_not_initialized(self, runner, tmp_path):
        """Clear on uninitialized project should show error."""
        result = runner.invoke(main, ["clear", str(tmp_path)], input="y\n")
        assert result.exit_code == 0
        assert "not initialized" in result.output.lower()


class TestClearClaudeignore:
    def test_creates_claudeignore(self, runner, tmp_path):
        """Clear should create .claudeignore with archive entry."""
        _setup_autohelix_dir(tmp_path)

        runner.invoke(main, ["clear", str(tmp_path)], input="y\n")

        claudeignore = tmp_path / ".claudeignore"
        assert claudeignore.exists()
        assert ".autohelix/archive/" in claudeignore.read_text()

    def test_appends_to_existing_claudeignore(self, runner, tmp_path):
        """Clear should append to existing .claudeignore."""
        _setup_autohelix_dir(tmp_path)
        claudeignore = tmp_path / ".claudeignore"
        claudeignore.write_text("some_other_dir/\n")

        runner.invoke(main, ["clear", str(tmp_path)], input="y\n")

        content = claudeignore.read_text()
        assert "some_other_dir/" in content
        assert ".autohelix/archive/" in content

    def test_does_not_duplicate_claudeignore_entry(self, runner, tmp_path):
        """Clear should not add duplicate .claudeignore entries."""
        import time

        _setup_autohelix_dir(tmp_path)
        runner.invoke(main, ["clear", str(tmp_path)], input="y\n")

        # Clear again
        time.sleep(1)
        ah = tmp_path / ".autohelix"
        (ah / "history.jsonl").write_text('{"iteration": 2}\n')
        runner.invoke(main, ["clear", str(tmp_path)], input="y\n")

        content = (tmp_path / ".claudeignore").read_text()
        assert content.count(".autohelix/archive/") == 1
