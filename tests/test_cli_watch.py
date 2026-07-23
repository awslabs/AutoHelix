"""Tests for the `autohelix watch` CLI command."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from autohelix.cli import main


@pytest.fixture
def runner():
    return CliRunner()


class TestWatchCommand:
    def test_no_logs_dir_exits(self, runner, tmp_path):
        result = runner.invoke(main, ["watch", str(tmp_path)])
        assert result.exit_code == 1
        assert "No agent logs found" in result.output

    @pytest.mark.slow
    def test_no_log_files_exits(self, runner, tmp_path):
        (tmp_path / ".autohelix" / "agent_logs").mkdir(parents=True)
        result = runner.invoke(main, ["watch", str(tmp_path)])
        assert result.exit_code == 1
