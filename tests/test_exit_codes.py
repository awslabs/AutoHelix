"""`run`/`resume` exit codes: 0 when the segment ended politely (a budget, a `stop:`
condition, `autohelix stop`), 1 on an error, 130 when interrupted. And a segment starts
as soon as its command holds the run's lock: a start its checks refuse is a recorded
segment that failed."""

import pytest

from autohelix.events import Watcher
from autohelix.harness import Harness
from autohelix.history import History
from autohelix.run_status import read_run_status, read_segments
from autohelix.runs import short_id
from tests.helpers import cli, configure, latest_run


MUTATING = "echo formatted >> code.py"  # a gate that edits files: the baseline fails


def test_polite_endings_exit_0(project):
    assert cli(project, "run", "-n", "1", "--no-report").exit_code == 0  # a budget
    configure(project, stop=["true"])
    result = cli(project, "run", "-n", "3", "--no-report")
    assert result.exit_code == 0 and "stop condition met" in result.output, result.output


def test_a_resume_whose_preflight_fails_exits_1_and_records_a_failed_segment(project, tmp_path, monkeypatch):
    agent = tmp_path / "bin" / "claude"
    agent.parent.mkdir()
    agent.write_text("#!/bin/sh\nexit 1\n")
    agent.chmod(0o755)
    # The baseline fails (a gate that edits files), so a resume runs preflight again.
    monkeypatch.setenv("AUTOHELIX_CLAUDE_CODE_CMD", str(agent))
    configure(project, gates=[MUTATING], agent={"cli": "claude-code"})
    first = cli(project, "run", "-n", "1", "--no-report")
    assert first.exit_code == 1 and "Baseline command modified candidate files" in first.output, first.output
    paths = latest_run(project)
    agent.unlink()  # the agent command gone: preflight fails
    result = cli(project, "resume", short_id(paths.run_id), "--no-report")
    assert result.exit_code == 1, result.output  # was 0
    assert str(agent) in "".join(result.output.split()) and "Preflight checks failed" in result.output
    assert result.output.count("Preflight checks failed") == 1  # said once, in the ending
    first_segment, second = read_segments(paths.segments)
    assert first_segment["state"] == "failed"  # the earlier segment's outcome stays
    assert (second["open"], second["state"], second["error"]) == (
        False, "failed", "AutoHelixRunError: Preflight checks failed. Fix the issues above before running.")
    assert read_run_status(project, paths.run_id)["segment"] == second["segment"] == 2


def test_a_resume_after_the_baseline_checks_the_agents_command_first(project, tmp_path, monkeypatch):
    """A missing agent executable on a later resume (another shell, another machine) is
    refused before an iteration is spent, as on the first start."""
    agent = tmp_path / "bin" / "claude"
    agent.parent.mkdir()
    agent.write_text("#!/bin/sh\nexit 1\n")
    agent.chmod(0o755)
    monkeypatch.setenv("AUTOHELIX_CLAUDE_CODE_CMD", str(agent))
    configure(project, agent={"cli": "claude-code"})
    cli(project, "run", "-n", "1", "--no-report")
    paths = latest_run(project)
    assert [r.iteration for r in History(project, paths.run_id).load()] == [0, 1]
    agent.unlink()
    result = cli(project, "resume", short_id(paths.run_id), "-n", "1", "--no-report")
    assert result.exit_code == 1 and "Preflight checks failed" in result.output, result.output
    assert str(agent) in "".join(result.output.split())
    assert [r.iteration for r in History(project, paths.run_id).load()] == [0, 1]  # nothing spent


def test_an_interrupt_in_the_loop_exits_130(project, monkeypatch):
    def interrupted(self, n):
        raise KeyboardInterrupt

    monkeypatch.setattr(Harness, "run_iteration", interrupted)
    result = cli(project, "run", "-n", "3", "--no-report")
    assert result.exit_code == 130, result.output
    assert "Interrupted" in result.output and "Aborted" not in result.output
    paths = latest_run(project)
    assert read_run_status(project, paths.run_id)["state"] == "interrupted"
    assert [r.iteration for r in History(project, paths.run_id).load()] == [0]


def test_an_interrupt_in_the_baseline_exits_130(project, monkeypatch):
    def interrupted(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(Harness, "_capture_baseline", interrupted)
    result = cli(project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 130, result.output
    assert "Aborted" not in result.output  # not click's exit 1
    paths = latest_run(project)
    [segment] = read_segments(paths.segments)
    assert segment["state"] == "interrupted"


def test_an_interrupted_baseline_is_recorded_as_interrupted(project, monkeypatch):
    """Not "baseline failed: " with nothing after it (str of a KeyboardInterrupt)."""
    def interrupted(self, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(Harness, "_evaluate", interrupted)
    result = cli(project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 130, result.output
    [record] = History(project, latest_run(project).run_id).load()
    assert (record.iteration, record.accepted, record.reason) == (0, False, "baseline interrupted")


def test_no_run_to_resume_exits_1(project):
    result = cli(project, "resume")
    assert result.exit_code == 1 and "No run to resume for autohelix/demo/config.yaml" in result.output


def test_a_segment_starts_when_the_lock_is_held_and_stop_reaches_it(project, monkeypatch):
    """Before its checks: `autohelix stop` accepts the request (it used to say the run
    wasn't running) and the loop honours it at its first pass; `wait` sees the start."""
    assert cli(project, "run", "-n", "1", "--no-report").exit_code == 0
    paths = latest_run(project)
    seen = {}
    original = Harness._print_header

    def header(self, *args):
        status = read_run_status(project, paths.run_id)
        seen["status"] = (status["state"], status["phase"], status["segment"])
        seen["open"] = read_segments(paths.segments)[-1]["open"]
        seen["wait"] = Watcher(project, run_ids=[paths.run_id]).idle()
        seen["stop"] = cli(project, "stop", short_id(paths.run_id))
        return original(self, *args)

    monkeypatch.setattr(Harness, "_print_header", header)
    result = cli(project, "resume", short_id(paths.run_id), "-n", "3", "--no-report")
    assert result.exit_code == 0, result.output  # a requested stop is polite
    assert seen["status"] == ("running", "starting", 2) and seen["open"] is True
    assert seen["wait"] is None  # running: not idle
    assert seen["stop"].exit_code == 0, seen["stop"].output
    assert [r.iteration for r in History(project, paths.run_id).load()] == [0, 1]  # nothing more ran
    assert read_run_status(project, paths.run_id)["stop_reason"]["kind"] == "request"


def test_a_start_refused_by_its_checks_lands_nothing(project):
    """A refused start (here: a dirty checkout before a baseline) is a failed segment
    whose ending lands nothing."""
    configure(project, gates=[MUTATING])
    assert cli(project, "run", "-n", "1", "--no-report").exit_code == 1  # no baseline
    paths = latest_run(project)
    (project / "code.py").write_text("dirty\n")
    result = cli(project, "resume", short_id(paths.run_id), "--no-report")
    assert result.exit_code == 1, result.output
    assert "Uncommitted changes" in result.output
    last = read_segments(paths.segments)[-1]
    assert last["state"] == "failed" and "Uncommitted changes" in last["error"]
    assert "landing" not in result.output.split("Error", 1)[1]


@pytest.mark.parametrize("command", ["run", "resume"])
def test_an_interrupt_before_the_segment_exits_130(project, monkeypatch, command):
    """Ctrl-C during the checks before a segment (a new run's preflight runs agent
    commands) exits 130 too, not click's "Aborted!" (1)."""
    if command == "resume":
        assert cli(project, "run", "-n", "1", "--no-report").exit_code == 0

    def interrupted(self, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(Harness, "__init__", interrupted)
    result = cli(project, command, "--no-report")
    assert result.exit_code == 130, result.output
    assert "Aborted" not in result.output
