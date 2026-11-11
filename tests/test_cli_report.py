"""Tests for the `autohelix report` CLI command."""

import json
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from autohelix.cli import main
from autohelix.config import load_config
from autohelix.history import History, IterationResult
from autohelix.report import WRITE_TO, build_report_prompt, read_report_info
from autohelix.runs import RunPaths, git, write_json


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def project(tmp_path, run_paths):
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
    run_paths.notes.mkdir()
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / ".gitignore").write_text(".autohelix/\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "baseline")
    tip = git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    identity = run_paths.read_identity()
    identity["base_commit"] = tip
    write_json(run_paths.identity, identity)
    git(tmp_path, "update-ref", "--create-reflog", run_paths.ref, tip)

    # Add some history
    history = History(tmp_path)
    history.append(IterationResult(iteration=0, accepted=True, metrics={"speed": 100}))
    history.append(IterationResult(iteration=1, accepted=True, metrics={"speed": 200}, commit="abc123"))
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

    @staticmethod
    def _agent(writes: str | None, also=None):
        """A fake agent that writes `writes` to the draft path named in its prompt."""
        def run(**kwargs):
            if writes is not None:
                draft = kwargs["prompt"].rsplit(WRITE_TO, 1)[1].strip().removesuffix(".")
                Path(draft).write_text(writes)
            if also:
                also(**kwargs)
            return MagicMock(error=None, usage={"cost_usd": 0.28})
        agent = MagicMock(display_name="mock")
        agent.run.side_effect = run
        return agent

    def test_writes_report(self, runner, project):
        with patch("autohelix.agents.create_agent", return_value=self._agent("# Summary\nGood run.")):
            result = runner.invoke(main, ["report", str(project)])
        assert result.exit_code == 0, result.output
        assert (RunPaths(project).output / "report.md").read_text() == "# Summary\nGood run."
        info = read_report_info(project)
        assert info["last_iteration"] == 3 and info["cost_usd"] == 0.28 and info["seconds"] >= 0
        assert not (RunPaths(project).output / "report.draft.md").exists()

    def test_warns_when_agent_changes_files(self, runner, project):
        def change(**kwargs):
            (kwargs["worktree_path"] / "autohelix.yaml").write_text("goal: changed\n")
        touch = change
        with patch("autohelix.agents.create_agent", return_value=self._agent("ok", also=touch)):
            result = runner.invoke(main, ["report", str(project)])
        assert "changed candidate files" in result.output
        assert "autohelix.yaml" in result.output

    def test_timeout_is_capped(self, runner, project):
        config = project / "autohelix.yaml"
        config.write_text(config.read_text() + "agent:\n  timeout_seconds: 36000\n")
        with patch("autohelix.agents.create_agent", return_value=self._agent("ok")) as create:
            runner.invoke(main, ["report", str(project)])
        assert create.call_args[0][0].timeout_seconds == 15 * 60

    def test_keeps_old_report_when_agent_writes_nothing(self, runner, project):
        out = RunPaths(project).output
        out.mkdir(parents=True)
        (out / "report.md").write_text("old")
        with patch("autohelix.agents.create_agent", return_value=self._agent(None)):
            result = runner.invoke(main, ["report", str(project)])
        assert result.exit_code == 1
        assert (out / "report.md").read_text() == "old"


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
    def _prompt(self, project):
        config, _ = load_config(project)
        return build_report_prompt(config, project, project / "autohelix.yaml", Path("/tmp/draft.md"))

    def test_goal_coverage_and_output(self, project):
        prompt = self._prompt(project)
        assert "Make it faster" in prompt
        assert "iterations 1–3" in prompt
        assert prompt.rstrip().endswith("Markdown to /tmp/draft.md.")
        assert "don't reproduce them" in prompt and "No title" in prompt

    def test_points_at_files_and_baseline(self, project):
        prompt = self._prompt(project)
        for path in ("history.jsonl", "notes/", "reviews/", "agent.log", "autohelix.yaml"):
            assert path in prompt
        assert f"`{RunPaths(project).read_identity()['base_commit']}`" in prompt

    def test_no_accepted_iterations(self, project):
        history = History(project)
        history.history_path.write_text("")
        history.append(IterationResult(iteration=1, accepted=False, metrics={}))
        assert "No iteration was accepted" in self._prompt(project)
