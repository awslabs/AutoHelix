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
from tests.helpers import latest_run


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def project(tmp_path, run_paths):
    """Project with config and some history."""
    config = textwrap.dedent("""\
        goal: Make it faster
        gates:
          - pytest tests/
        observe:
          - name: bench
            command: python bench.py
            metrics:
              speed: higher
    """)
    (tmp_path / "autohelix.yaml").write_text(config)
    run_paths.config.write_text(config)  # the run's stored copy
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
    history = History(tmp_path, latest_run(tmp_path).run_id)
    history.append(IterationResult(iteration=0, accepted=True, metrics={"speed": 100}))
    history.append(IterationResult(iteration=1, accepted=True, metrics={"speed": 200}, commit="abc123"))
    history.append(IterationResult(iteration=2, accepted=False, metrics={}, reason="constraints failed"))
    history.append(IterationResult(iteration=3, accepted=True, metrics={"speed": 350}))
    return tmp_path


class TestReportCommand:
    def test_no_history_exits(self, runner, tmp_path):
        (tmp_path / "autohelix.yaml").write_text("goal: test\n")
        (tmp_path / ".autohelix").mkdir()

        result = runner.invoke(main, ["report", "-p", str(tmp_path)])
        assert result.exit_code == 1
        assert "No runs in" in result.output  # nothing to report on

    def test_a_run_without_iterations_exits(self, runner, tmp_path):
        (tmp_path / "autohelix.yaml").write_text("goal: test\n")
        paths = RunPaths(tmp_path, "test-1234")
        write_json(paths.identity, {"schema": 2, "id": paths.run_id})
        result = runner.invoke(main, ["report", "-p", str(tmp_path)])
        assert result.exit_code == 1
        assert "Run 1234 has no iterations yet. Nothing to report." in result.output

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
            result = runner.invoke(main, ["report", "-p", str(project)])
        assert result.exit_code == 0, result.output
        assert (latest_run(project).report).read_text() == "# Summary\nGood run."
        info = read_report_info(project, latest_run(project).run_id)
        assert info["last_iteration"] == 3 and info["cost_usd"] == 0.28 and info["seconds"] >= 0
        assert not (latest_run(project).report_draft).exists()

    def test_errors_when_agent_changes_files(self, runner, project):
        def change(**kwargs):
            (kwargs["copy_path"] / "autohelix.yaml").write_text("goal: changed\n")
        touch = change
        with patch("autohelix.agents.create_agent", return_value=self._agent("ok", also=touch)):
            result = runner.invoke(main, ["report", "-p", str(project)])
        assert "changed accepted code files" in result.output
        assert "autohelix.yaml" in result.output

    def test_timeout_is_capped(self, runner, project):
        config = latest_run(project).config
        config.write_text(config.read_text() + "agent:\n  timeout: 10h\n")
        with patch("autohelix.agents.create_agent", return_value=self._agent("ok")) as create:
            runner.invoke(main, ["report", "-p", str(project)])
        assert create.call_args[0][0].timeout == 15 * 60

    def test_reads_the_stored_config_and_refuses_without_one(self, runner, project):
        (project / "autohelix.yaml").write_text("goal: edited\nagent:\n  cli: codex\n")
        with patch("autohelix.agents.create_agent", return_value=self._agent("ok")) as create:
            result = runner.invoke(main, ["report", "-p", str(project)])
        assert result.exit_code == 0, result.output
        assert create.call_args[0][0].cli == "claude-code"
        assert "Make it faster" in self._agent_prompt(create)
        latest_run(project).config.unlink()
        result = runner.invoke(main, ["report", "-p", str(project)])
        assert result.exit_code == 1
        assert "Run test has no stored config: it was made by a development version of AutoHelix." in (
            " ".join(result.output.split()))

    @staticmethod
    def _agent_prompt(create):
        return create.return_value.run.call_args.kwargs["prompt"]

    def test_keeps_old_report_when_agent_writes_nothing(self, runner, project):
        report = latest_run(project).report
        report.write_text("old")
        with patch("autohelix.agents.create_agent", return_value=self._agent(None)):
            result = runner.invoke(main, ["report", "-p", str(project)])
        assert result.exit_code == 1
        assert report.read_text() == "old"


class TestBuildReportPrompt:
    def _prompt(self, project):
        config = load_config(project)
        return build_report_prompt(config, project, Path("/tmp/draft.md"), latest_run(project).run_id)

    def test_goal_coverage_and_output(self, project):
        prompt = self._prompt(project)
        assert "Make it faster" in prompt
        assert "iterations 1–3" in prompt
        assert prompt.rstrip().endswith("Markdown to /tmp/draft.md.")
        assert "don't reproduce them" in prompt and "No title" in prompt

    def test_points_at_files_and_baseline(self, project):
        prompt = self._prompt(project)
        for path in ("history.jsonl", "notes/", "agent.log", "config.yaml"):
            assert path in prompt
        assert f"`{latest_run(project).read_identity()['base_commit']}`" in prompt

    def test_where_to_look_matches_the_layout(self, project):
        prompt = self._prompt(project)
        for text in ("iterations/NNN/", "record.json", "prompt.md and agent.log", "gates/<k>/ and stop/<k>/",
                     "observe/<name>/", "review.log", "rejected.patch", "iterations/NNN/notes/",
                     ".internal/notes/<N>/", ".internal/attempts/NNN-<k>/", "logs/"):
            assert text in prompt, text
        paths = latest_run(project)  # the names it uses are RunPaths'
        run, folder = paths.run_dir, paths.iteration_dir(1)
        assert [p.relative_to(folder).as_posix() for p in (
            paths.record(1), paths.prompt(1), paths.agent_log(1), paths.gate_dir(1, 2), paths.stop_dir(1, 2),
            paths.observe_entry(1, "x"), paths.rejected_patch(1), paths.iteration_notes(1))] == [
            "record.json", "prompt.md", "agent.log", "gates/2", "stop/2", "observe/x", "rejected.patch", "notes"]
        assert [p.relative_to(run).as_posix() for p in (paths.notes_versions, paths.attempt_dir(1, 2), paths.logs)] == [
            ".internal/notes", ".internal/attempts/001-2", "logs"]

    def test_no_accepted_iterations(self, project):
        history = History(project, latest_run(project).run_id)
        history.history_path.write_text("")
        history.append(IterationResult(iteration=1, accepted=False, metrics={}))
        assert "No iteration was accepted" in self._prompt(project)
