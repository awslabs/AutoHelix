"""The live terminal views: `view` follows the log the run status points at; `dashboard` redraws."""

import io
import json
import time

import pytest
from rich.console import Console

import autohelix.live as W
from autohelix.run_status import RunStatus


def _status(**info) -> dict:
    now = time.time()
    return {"state": "running", "iteration": 2, "phase": "agent", "phase_started_at": now - 5,
            "iteration_started_at": now - 65,
            "calls": [{"role": "agent", "log": "iterations/002/agent.log"}], **info}


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
    agent = run_paths.agent_log(2)
    assert W.log_for(tmp_path, _status(), None)[:2] == (agent, "Iteration 2 · agent")
    assert W.log_for(tmp_path, _status(phase="report"), agent)[:2] == (logs / "report.log", "Summary")
    # Between phases the panel stays put; a watcher started then shows the agent log.
    shown = run_paths.gate_dir(2, 1) / "ask.log"
    assert W.log_for(tmp_path, _status(phase="accept"), shown) is None
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


def _stop_after(monkeypatch, polls=1):
    """Make the live loops stop (Ctrl-C) at their `polls`-th sleep."""
    calls = iter(range(polls - 1))
    monkeypatch.setattr(W.time, "sleep", lambda s: next(calls, None) is None and (_ for _ in ()).throw(KeyboardInterrupt))


def _watch_once(tmp_path, monkeypatch, polls=1) -> str:
    """Run the live view for `polls` polls and return what it printed."""
    _stop_after(monkeypatch, polls)
    out = io.StringIO()
    with pytest.raises(KeyboardInterrupt):
        W.view(tmp_path, "claude", Console(file=out, width=100, force_terminal=False))
    return out.getvalue()


def test_view_shows_the_running_agent_log(tmp_path, monkeypatch, run_paths):
    status = RunStatus(tmp_path)
    status.start()
    status.phase("agent", 1)
    log = run_paths.agent_log(1)
    log.parent.mkdir(parents=True)
    status.note(log=str(log), calls=[{"role": "agent", "log": str(log.relative_to(run_paths.run_dir))}])
    log.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Profiling the sort first"}]}}) + "\n")
    out = _watch_once(tmp_path, monkeypatch)
    assert "Iteration 1 · agent" in out
    assert "Profiling the sort first" in out
    assert "Running · iteration 1 · agent" in out


def test_view_without_a_run_waits(tmp_path, monkeypatch):
    assert "No run recorded yet" in _watch_once(tmp_path, monkeypatch)


def test_baseline_has_no_agent_panel(tmp_path):
    assert W.log_for(tmp_path, _status(iteration=0, phase="baseline"), None) is None
    assert W.log_for(tmp_path, {"state": "finished", "iteration": 0, "phase": None}, None) is None


def _cli(*args):
    from click.testing import CliRunner

    from autohelix.cli import main

    return CliRunner().invoke(main, list(map(str, args)))


def test_live_view_exits_with_hints_instead_of_waiting(tmp_path):
    def hint():
        result = _cli("view", "-p", tmp_path)
        assert result.exit_code == 1
        return " ".join(result.output.split())

    assert "Write a config with `autohelix setup`, then `autohelix run`." in hint()
    (tmp_path / "autohelix").mkdir()
    (tmp_path / "autohelix" / "perf.yaml").write_text("goal: x\n")
    assert "Start one with `autohelix run perf`." in hint()
    (tmp_path / "autohelix.yaml").write_text("goal: x\n")
    assert ("Start one with `autohelix run [CONFIG]` (configs: autohelix.yaml, autohelix/perf.yaml)."
            in hint())


@pytest.mark.parametrize("command", ["view", "dashboard"])
@pytest.mark.parametrize("flags", [["--json", "--once"], ["--json", "--web"], ["--once", "--web"],
                                   ["--port", "9000"], ["--open"], ["--once", "--port", "0"], ["--json", "--open"]])
def test_one_mode_at_a_time(tmp_path, command, flags):
    result = _cli(command, "-p", tmp_path, *flags)
    assert result.exit_code == 2, result.output
    assert "--port and --open apply to --web" in result.output or "Use one of --json, --once and --web" in result.output


@pytest.mark.parametrize("flags", [["--json"], ["--web"]])
def test_dashboard_metric_goes_with_the_table(tmp_path, flags):
    result = _cli("dashboard", "-p", tmp_path, "--metric", "speed", *flags)
    assert result.exit_code == 2 and "--metric applies to the table" in result.output


def test_live_dashboard_draws_the_table_until_ctrl_c(tmp_path, monkeypatch, run_paths):
    _stop_after(monkeypatch, polls=2)
    reads = []
    real = W.run_table.format_runs
    monkeypatch.setattr(W.run_table, "format_runs", lambda data, **k: reads.append(k) or real(data, **k))
    out = io.StringIO()
    with pytest.raises(KeyboardInterrupt):
        W.dashboard(tmp_path, Console(file=out, width=100, force_terminal=False))
    assert len(reads) == 2  # redrawn after the first sleep
    assert "1 run" in out.getvalue() and "Ctrl-C to stop" in out.getvalue()
    # Through the CLI, Ctrl-C ends it quietly.
    _stop_after(monkeypatch)
    result = _cli("dashboard", "-p", tmp_path)
    assert result.exit_code == 0 and "RUN" in result.output, result.output


def _run_data(iterations: int) -> dict:
    rows = [{"iteration": i, "accepted": True, "metrics": {"speed": 100 + i}, "summary": f"change {i}",
             "cost": 0.1, "cost_incomplete": False, "seconds": 30} for i in range(iterations)]
    run = {"id": "r1", "config_file": "a.yaml", "accepted": 1, "target_branch": None, "landing": None,
           "last_activity": 990, "metrics": None, "goal": "Go faster", "status": {"state": "finished"},
           "progress": {"iterations": iterations - 1, "max_iterations": None, "cost_usd": 1.5,
                        "cost_incomplete": False, "seconds": 60, "max_cost_usd": None, "max_seconds": None},
           "iterations": rows}
    return {"schema": 1, "project": "p", "host": "h", "path": "/x", "path_display": "/x", "branch": None,
            "commit": "abc1234", "run": run}


def _screen_lines(data, log, height) -> list[str]:
    out = io.StringIO()
    console = Console(file=out, width=100, height=height, force_terminal=False)
    console.print(W.view_screen(data, log, {"state": "finished"}, height))
    return out.getvalue().splitlines()


def test_on_a_short_terminal_the_log_shrinks_first(tmp_path):
    log = W.LogFollower(tmp_path, None, Console(file=io.StringIO()))
    log.display = W.LiveDisplay(console=log.console, title="Iteration 3 · agent", verbose=True, max_entries=30)
    for i in range(30):
        log.display._append_entry("Agent", f"step {i}", "white")
    data = _run_data(5)

    tall = _screen_lines(data, log, 60)
    assert tall[0].startswith("r1 · finished") and any("change 4" in line for line in tall)
    assert any("step 29" in line for line in tall) and any("step 5" in line for line in tall)
    assert tall[-1].startswith("Finished")

    short = _screen_lines(data, log, 20)
    assert len(short) <= 19
    assert short[0].startswith("r1 · finished") and sum("change" in line for line in short) == 5  # the table stays
    assert any("step 29" in line for line in short) and not any("step 5" in line for line in short)  # newest output

    tiny = _screen_lines(_run_data(40), log, 20)
    assert len(tiny) <= 19
    assert tiny[0].startswith("r1 · finished") and any("ITER" in line for line in tiny)
    assert any("earlier" in line for line in tiny) and any("change 39" in line for line in tiny)
    assert not any(line.endswith("change 1") for line in tiny)  # the oldest rows went
    assert any("step 29" in line for line in tiny) and tiny[-1].startswith("Finished")

    cramped = _screen_lines(_run_data(40), log, 12)  # no room for the log: the table keeps the screen
    assert len(cramped) <= 11 and cramped[0].startswith("r1 · finished") and cramped[-1].startswith("Finished")
    assert not any("step" in line for line in cramped) and any("change 39" in line for line in cramped)


def test_rerun_of_the_same_iteration_gets_a_fresh_panel(tmp_path, monkeypatch, run_paths):
    """Resuming reruns an unfinished iteration: same log path, new start time."""
    status = RunStatus(tmp_path)
    status.start()
    status.phase("agent", 1)
    log = run_paths.agent_log(1)
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
        W.view(tmp_path, "claude", Console(file=io.StringIO()))
    assert len(created) >= 2 and set(created) == {"Iteration 1 · agent"}
