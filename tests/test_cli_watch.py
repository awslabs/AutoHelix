"""Tests for `autohelix watch`: it follows the log the run status points at."""

import io
import json
import time

import pytest
from rich.console import Console

import autohelix.watch as W
from autohelix.run_status import RunStatus


def _status(**info) -> dict:
    now = time.time()
    return {"state": "running", "iteration": 2, "phase": "agent", "phase_started_at": now - 5,
            "iteration_started_at": now - 65,
            "calls": [{"role": "agent", "log": "logs/iter-2/call-001-agent/agent.log"},
                      {"role": "reviewer", "log": "logs/iter-2/call-002-reviewer/review.log"}], **info}


def test_status_line():
    now = time.time()
    assert W.status_line(None).startswith("No run recorded yet")
    assert W.status_line(_status(phase="checks", phase_started_at=now - 20), now) == "Running · iteration 2 · checks · 20s"
    assert W.status_line(_status(iteration=0, phase="baseline", phase_started_at=now), now) == "Running · measuring the baseline · 0s"
    assert W.status_line(_status(phase="report", phase_started_at=now - 75), now) == "Running · writing the summary · 1m 15s"
    finished = W.status_line({"state": "finished", "stopped_at": now})
    assert finished.startswith("Finished at ") and finished.endswith("Waiting for the next run…")


def test_log_for_follows_phases(tmp_path, run_paths):
    logs = run_paths.logs
    agent = logs / "iter-2" / "call-001-agent" / "agent.log"
    assert W.log_for(tmp_path, _status(), None)[:2] == (agent, "Iteration 2 · agent")
    assert W.log_for(tmp_path, _status(phase="review"), agent)[:2] == (logs / "iter-2" / "call-002-reviewer" / "review.log", "Iteration 2 · reviewer")
    assert W.log_for(tmp_path, _status(phase="report"), agent)[:2] == (logs / "report.log", "Summary")
    assert W.log_for(tmp_path, _status(iteration=0, phase="review"), None)[1] == "Baseline · reviewer"
    # Between phases the panel stays put; a watcher started then shows the agent log.
    review = logs / "iter-2" / "call-002-reviewer" / "review.log"
    assert W.log_for(tmp_path, _status(phase="accept"), review) is None
    assert W.log_for(tmp_path, _status(phase="checks"), None)[0] == agent
    # Agent panel time counts from the iteration's start, not the phase's.
    assert W.log_for(tmp_path, _status(phase="checks"), None)[2] == pytest.approx(time.time() - 65, abs=2)
    assert W.log_for(tmp_path, _status(iteration=None, phase="starting"), None) is None


def test_run_status_records_iteration_start(tmp_path, run_paths):
    status = RunStatus(tmp_path)
    status.start()
    status.phase("agent", 1)
    first = status.data["iteration_started_at"]
    status.phase("checks")
    assert status.data["iteration_started_at"] == first
    status.phase("agent", 2)
    assert status.data["iteration_started_at"] >= first and status.data["iteration"] == 2


def _watch_once(tmp_path, monkeypatch, polls=1) -> str:
    """Run the watch loop for `polls` polls and return what it printed."""
    calls = iter(range(polls))
    monkeypatch.setattr(W.time, "sleep", lambda s: next(calls, None) is None and (_ for _ in ()).throw(KeyboardInterrupt))
    out = io.StringIO()
    with pytest.raises(KeyboardInterrupt):
        W.watch(tmp_path, "claude", Console(file=out, width=100, force_terminal=False))
    return out.getvalue()


def test_watch_shows_the_running_agent_log(tmp_path, monkeypatch, run_paths):
    status = RunStatus(tmp_path)
    status.start()
    status.phase("agent", 1)
    log = run_paths.call_dir(1, "agent") / "agent.log"
    log.parent.mkdir(parents=True)
    status.note(log=str(log), calls=[{"role": "agent", "log": str(log.relative_to(run_paths.run_dir))}])
    log.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Profiling the sort first"}]}}) + "\n")
    out = _watch_once(tmp_path, monkeypatch)
    assert "Iteration 1 · agent" in out
    assert "Profiling the sort first" in out
    assert "Running · iteration 1 · agent" in out


def test_watch_without_a_run_waits(tmp_path, monkeypatch):
    assert "No run recorded yet" in _watch_once(tmp_path, monkeypatch)


def test_baseline_has_no_agent_panel(tmp_path):
    assert W.log_for(tmp_path, _status(iteration=0, phase="baseline"), None) is None
    assert W.log_for(tmp_path, {"state": "finished", "iteration": 0, "phase": None}, None) is None


def test_watch_exits_with_hints_instead_of_waiting(tmp_path):
    from click.testing import CliRunner

    from autohelix.cli import main

    result = CliRunner().invoke(main, ["watch", "-p", str(tmp_path)])
    assert result.exit_code == 1 and "No .autohelix/" in result.output
    marker = tmp_path / ".autohelix" / "parallel" / "run.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"workers": [{"name": "w1", "worktree": "/tmp/w1"}]}))
    result = CliRunner().invoke(main, ["watch", "-p", str(tmp_path)])
    assert result.exit_code == 1 and "/tmp/w1" in result.output


def test_rerun_of_the_same_iteration_gets_a_fresh_panel(tmp_path, monkeypatch, run_paths):
    """Resuming reruns an unfinished iteration: same log path, new start time."""
    status = RunStatus(tmp_path)
    status.start()
    status.phase("agent", 1)
    log = run_paths.call_dir(1, "agent") / "agent.log"
    log.parent.mkdir(parents=True)
    status.note(log=str(log), calls=[{"role": "agent", "log": str(log.relative_to(run_paths.run_dir))}])
    log.write_text("")
    created = []
    real = W.LiveDisplay
    monkeypatch.setattr(W, "LiveDisplay", lambda *a, **k: created.append(k["title"]) or real(*a, **k))
    polls = iter(range(3))

    def sleep(_):
        if next(polls, None) is None:
            raise KeyboardInterrupt
        status.stop("interrupted")
        status.start()
        status.phase("agent", 1)
        status.note(log=str(log), calls=[{"role": "agent", "log": str(log.relative_to(run_paths.run_dir))}])  # rerun: new iteration_started_at
    monkeypatch.setattr(W.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        W.watch(tmp_path, "claude", Console(file=io.StringIO()))
    assert len(created) >= 2 and set(created) == {"Iteration 1 · agent"}
