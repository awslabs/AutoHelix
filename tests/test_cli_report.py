"""Tests for the `autohelix report` CLI command."""

import json
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from autohelix.cli import main, _build_report_prompt
from autohelix.config import load_config
from autohelix.history import History, IterationResult


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def project(tmp_path):
    """Project with config and some history."""
    config = textwrap.dedent("""\
        goal: Make it faster
        constraints:
          - pytest tests/
        observables:
          - command: python bench.py
            values:
              speed: higher
    """)
    (tmp_path / "autohelix.yaml").write_text(config)
    (tmp_path / ".autohelix").mkdir()
    (tmp_path / ".autohelix" / "notes").mkdir()

    # Add some history
    history = History(tmp_path)
    history.append(IterationResult(iteration=0, accepted=True, metrics={"speed": 100}))
    history.append(IterationResult(iteration=1, accepted=True, metrics={"speed": 200}))
    history.append(IterationResult(iteration=2, accepted=False, metrics={}, reason="constraints failed"))
    history.append(IterationResult(iteration=3, accepted=True, metrics={"speed": 350}))
    return tmp_path


class TestReportCommand:
    def test_no_history_exits(self, runner, tmp_path):
        (tmp_path / "autohelix.yaml").write_text("goal: test\n")
        (tmp_path / ".autohelix").mkdir()

        result = runner.invoke(main, ["report", str(tmp_path)])
        assert result.exit_code == 1
        assert "No iterations" in result.output

    def test_no_config_exits(self, runner, tmp_path):
        result = runner.invoke(main, ["report", str(tmp_path)])
        assert result.exit_code == 1
        assert "Config not found" in result.output

    def test_invokes_agent(self, runner, project):
        mock_agent = MagicMock()
        mock_agent.run.return_value = MagicMock(error=None)
        mock_agent.display_name = "mock"

        with patch("autohelix.agents.create_agent", return_value=mock_agent):
            result = runner.invoke(main, ["report", str(project)])

        assert result.exit_code == 0, result.output
        mock_agent.run.assert_called_once()
        call_kwargs = mock_agent.run.call_args[1]
        assert "prompt" in call_kwargs
        assert "Make it faster" in call_kwargs["prompt"]


class TestParallelGuard:
    def _write_marker(self, tmp_path, workers):
        marker = tmp_path / ".autohelix" / "parallel" / "run.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"workers": workers}))

    def test_report_warns_when_parallel_marker_present(self, runner, tmp_path):
        (tmp_path / "autohelix.yaml").write_text("goal: test\n")
        (tmp_path / ".autohelix").mkdir()
        self._write_marker(tmp_path, [
            {"name": "worker-1", "worktree": "/tmp/proj-parallel/worker-1", "branch": "parallel/worker-1"},
            {"name": "worker-2", "worktree": "/tmp/proj-parallel/worker-2", "branch": "parallel/worker-2"},
        ])

        result = runner.invoke(main, ["report", str(tmp_path)])
        assert result.exit_code == 1
        assert "run with `autohelix parallel`" in result.output
        assert "/tmp/proj-parallel/worker-1" in result.output
        assert "/tmp/proj-parallel/worker-2" in result.output
        # Still falls through to the normal empty-state message.
        assert "No iterations" in result.output

    def test_report_no_warning_without_marker(self, runner, tmp_path):
        (tmp_path / "autohelix.yaml").write_text("goal: test\n")
        (tmp_path / ".autohelix").mkdir()

        result = runner.invoke(main, ["report", str(tmp_path)])
        assert result.exit_code == 1
        assert "autohelix parallel" not in result.output

    def test_malformed_marker_is_ignored(self, runner, tmp_path):
        (tmp_path / "autohelix.yaml").write_text("goal: test\n")
        (tmp_path / ".autohelix").mkdir()
        marker = tmp_path / ".autohelix" / "parallel" / "run.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{not valid json")

        result = runner.invoke(main, ["report", str(tmp_path)])
        assert result.exit_code == 1
        # No crash, no parallel warning — just the normal empty-state message.
        assert "No iterations" in result.output
        assert "autohelix parallel" not in result.output


class TestBuildReportPrompt:
    def test_includes_goal(self, project):
        config, _ = load_config(project)
        history = History(project)
        prompt = _build_report_prompt(config, history, project)
        assert "Make it faster" in prompt

    def test_includes_iteration_count(self, project):
        config, _ = load_config(project)
        history = History(project)
        prompt = _build_report_prompt(config, history, project)
        assert "Iterations: 3" in prompt

    def test_includes_accepted_rejected(self, project):
        config, _ = load_config(project)
        history = History(project)
        prompt = _build_report_prompt(config, history, project)
        assert "Accepted: 2" in prompt
        assert "Rejected: 1" in prompt

    def test_includes_baseline_metrics(self, project):
        config, _ = load_config(project)
        history = History(project)
        prompt = _build_report_prompt(config, history, project)
        assert "speed: 100" in prompt

    def test_includes_best_metrics(self, project):
        config, _ = load_config(project)
        history = History(project)
        prompt = _build_report_prompt(config, history, project)
        assert "speed: 350" in prompt

    def test_detects_in_progress(self, project):
        config, _ = load_config(project)
        history = History(project)
        prompt = _build_report_prompt(config, history, project)
        # 3 iterations out of 5 budget = still in progress
        assert "in progress" in prompt

    def test_includes_notes(self, project):
        (project / ".autohelix" / "notes" / "iter-1.md").write_text("Tried quicksort")
        config, _ = load_config(project)
        history = History(project)
        prompt = _build_report_prompt(config, history, project)
        assert "Tried quicksort" in prompt

    def test_includes_review(self, project):
        reviews = project / ".autohelix" / "reviews"
        reviews.mkdir(parents=True)
        (reviews / "iter-2.md").write_text("Good progress on sorting")
        config, _ = load_config(project)
        history = History(project)
        prompt = _build_report_prompt(config, history, project)
        assert "Good progress on sorting" in prompt
