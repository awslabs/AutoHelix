"""The live terminal views: `view` follows the log the run status points at; `dashboard` redraws."""

import io
import json
import time

import pytest
from rich.console import Console

import autohelix.live as W
from autohelix.run_status import RunStatus
from autohelix.runs import RunLock
from tests.helpers import latest_run


def _status(**info) -> dict:
    now = time.time()
    return {"state": "running", "iteration": 2, "phase": "agent", "phase_started_at": now - 5,
            "iteration_started_at": now - 65,
            "calls": [{"role": "agent", "log": "iterations/002/agent.log"}], **info}


def test_status_line():
    now = time.time()
    assert W.status_line(None).startswith("No run recorded yet")
    assert W.status_line(_status(phase="checks", phase_started_at=now - 20), now) == "Running · iteration 2 · gates · 20s"
    assert W.status_line(_status(iteration=0, phase="baseline", phase_started_at=now), now) == "Running · measuring the baseline · 0s"
    assert W.status_line(_status(phase="report", phase_started_at=now - 75), now) == "Running · writing the summary · 1m 15s"
    finished = W.status_line({"state": "finished", "stopped_at": now})
    assert finished.startswith("Finished at ") and finished.endswith("Waiting for the next run…")


def test_log_for_follows_phases(tmp_path, run_paths):
    logs = run_paths.logs
    agent = run_paths.agent_log(2)
    assert W.log_for(tmp_path, _status(), None, "test")[:2] == (agent, "Iteration 2 · agent")
    assert W.log_for(tmp_path, _status(phase="report"), agent, "test")[:2] == (logs / "report.log", "Summary")
    # Between phases the panel stays put; a watcher started then shows the agent log.
    shown = run_paths.gate_dir(2, 1) / "ask.log"
    assert W.log_for(tmp_path, _status(phase="accept"), shown, "test") is None
    assert W.log_for(tmp_path, _status(phase="checks"), None, "test")[0] == agent
    # Agent panel time counts from the iteration's start, not the phase's.
    assert W.log_for(tmp_path, _status(phase="checks"), None, "test")[2] == pytest.approx(time.time() - 65, abs=2)
    assert W.log_for(tmp_path, _status(iteration=None, phase="starting"), None, "test") is None


def test_run_status_records_iteration_start(tmp_path, run_paths):
    status = RunStatus(tmp_path, latest_run(tmp_path).run_id)
    status.start()
    status.phase("agent", 1)
    first = status.data["iteration_started_at"]
    status.phase("checks")
    assert status.data["iteration_started_at"] == first
    status.phase("agent", 2)
    assert status.data["iteration_started_at"] >= first and status.data["iteration"] == 2


class _Clock:
    """`time` for live.py with its own `sleep`. Patching `time.sleep` itself also counts sleeps
    elsewhere in the process (subprocess's timed wait for a slow git), which could stop the view
    before its first frame."""

    def __init__(self, sleep):
        self.sleep = sleep

    def __getattr__(self, name):
        return getattr(time, name)


def _stop_after(monkeypatch, polls=1):
    """Make the live loops stop (Ctrl-C) at their `polls`-th sleep."""
    calls = iter(range(polls - 1))

    def sleep(_):
        if next(calls, None) is None:
            raise KeyboardInterrupt

    monkeypatch.setattr(W, "time", _Clock(sleep))


def _watch_once(tmp_path, monkeypatch, polls=1) -> str:
    """Run the live view for `polls` polls and return what it printed."""
    _stop_after(monkeypatch, polls)
    out = io.StringIO()
    with pytest.raises(KeyboardInterrupt):
        W.view(tmp_path, "claude", Console(file=out, width=100, force_terminal=False), run_id="test")
    return out.getvalue()


def test_view_shows_the_running_agent_log(tmp_path, monkeypatch, run_paths):
    status = RunStatus(tmp_path, latest_run(tmp_path).run_id)
    status.lock = RunLock(latest_run(tmp_path).lock)  # a running segment holds the run's lock
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
    assert W.log_for(tmp_path, _status(iteration=0, phase="baseline"), None, "test") is None
    assert W.log_for(tmp_path, {"state": "finished", "iteration": 0, "phase": None}, None, "test") is None


def _cli(*args):
    from click.testing import CliRunner

    from autohelix.cli import main

    return CliRunner().invoke(main, list(map(str, args)))


def test_live_view_exits_with_hints_instead_of_waiting(tmp_path):
    def hint():
        result = _cli("view", "-p", tmp_path)
        assert result.exit_code == 1
        return " ".join(result.output.split())

    assert "No runs in" in hint() and "write a config with `autohelix setup`, then `autohelix run`." in hint()
    (tmp_path / "autohelix").mkdir()
    (tmp_path / "autohelix" / "perf.yaml").write_text("goal: x\n")
    assert "start one with `autohelix run perf`." in hint()
    (tmp_path / "autohelix.yaml").write_text("goal: x\n")
    assert ("start one with `autohelix run [CONFIG]` (configs: autohelix.yaml, autohelix/perf.yaml)."
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
    real = W.run_table.dashboard_lines
    monkeypatch.setattr(W.run_table, "dashboard_lines", lambda data, *a, **k: reads.append(k) or real(data, *a, **k))
    out = io.StringIO()
    with pytest.raises(KeyboardInterrupt):
        W.dashboard(tmp_path, Console(file=out, width=100, force_terminal=False))
    assert len(reads) == 2  # redrawn after the first sleep
    assert "Runs       1 run" in out.getvalue() and "Ctrl-C to stop" in out.getvalue()
    # Through the CLI, Ctrl-C ends it quietly.
    _stop_after(monkeypatch)
    result = _cli("dashboard", "-p", tmp_path)
    assert result.exit_code == 0 and "dashboard" in result.output, result.output


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


def test_the_time_cap_is_the_running_segments_own():
    """What is left of --time is shown against the segment's clock while it runs,
    "time up" past it, nothing once the run stopped; the run's total time has no cap."""
    data = _run_data(3)
    run = data["run"]
    assert W.run_table.time_cap(run, 1000) == ""  # not running
    run["status"] = {"state": "running", "iteration": 2, "segment_started_at": 1000 - 725,
                     "max_time_seconds": 3600, "phase": "agent", "phase_started_at": 990}
    assert W.run_table.time_cap(run, 1000) == "47m 55s left (--time 1h)"  # the cap as --time spells it
    assert "left" not in W.run_table.format_run(data, W.run_table.Saved(), now=1000)  # the view's live row has the iteration's time
    run["status"].update(segment_started_at=1000 - 6000, max_time_seconds=5400)
    assert W.run_table.time_cap(run, 1000) == "time up (--time 1h30m)"
    listing = W.run_table.format_runs({**data, "runs": [run]}, now=1000)
    assert "spent 1m 00s · $1.50 · time up (--time 1h30m)" in listing
    run["status"]["segment_started_at"] = 1000 + 5  # clock skew
    assert W.run_table.time_cap(run, 1000) == "1h 30m left (--time 1h30m)"
    run["status"].update(state="finished")
    assert W.run_table.time_cap(run, 1000) == ""
    assert "1h30m" not in W.run_table.format_runs({**data, "runs": [run]}, now=1000)


def _screen_lines(data, log, height) -> list[str]:
    out = io.StringIO()
    console = Console(file=out, width=100, height=height, force_terminal=False)
    from autohelix.terminal import Row

    rows = [Row(r["iteration"], r["accepted"], r["metrics"], r["seconds"], r["cost"], commit=f"c{r['iteration']}",
                summary=r["summary"]) for r in data["run"]["iterations"]]
    console.print(W.view_screen((data, W.run_table.Saved(rows=rows)), log, {"state": "finished"}, height))
    return out.getvalue().splitlines()


def test_on_a_short_terminal_the_log_shrinks_first(tmp_path):
    log = W.LogFollower(tmp_path, None, Console(file=io.StringIO()), "test")
    log.display = W.LiveDisplay(console=log.console, title="Iteration 3 · agent", verbose=True, max_entries=30)
    for i in range(30):
        log.display._append_entry("Agent", f"step {i}", "white")
    data = _run_data(5)

    def row(n):  # an iteration's row: `✓ 4   30s   $0.10   104 +4%`
        return lambda line: line.split()[:2] == ["✓", str(n)]

    tall = _screen_lines(data, log, 60)
    assert tall[0].endswith(" · run r1 · p · finished") and any(map(row(4), tall))
    assert any("step 29" in line for line in tall) and any("step 5" in line for line in tall)
    assert tall[-1].startswith("Finished")

    short = _screen_lines(data, log, 30)
    assert len(short) <= 29
    assert short[0].endswith("finished") and all(any(map(row(n), short)) for n in range(1, 5))  # the table stays
    assert any("step 29" in line for line in short) and not any("step 5" in line for line in short)  # newest output

    tiny = _screen_lines(_run_data(40), log, 30)
    assert len(tiny) <= 29
    assert tiny[0].endswith("finished") and any(line.lstrip().startswith("time") for line in tiny)
    assert any("earlier" in line for line in tiny) and any(map(row(39), tiny))
    assert not any(map(row(1), tiny))  # the oldest rows went
    assert any("step 29" in line for line in tiny) and tiny[-1].startswith("Finished")

    cramped = _screen_lines(_run_data(40), log, 21)  # no room for the log: the table keeps the screen
    assert len(cramped) <= 20 and cramped[0].endswith("finished") and cramped[-1].startswith("Finished")
    assert not any("step" in line for line in cramped) and any(map(row(39), cramped))


def test_rerun_of_the_same_iteration_gets_a_fresh_panel(tmp_path, monkeypatch, run_paths):
    """Resuming reruns an unfinished iteration: same log path, new start time."""
    status = RunStatus(tmp_path, latest_run(tmp_path).run_id)
    status.lock = RunLock(latest_run(tmp_path).lock)  # a running segment holds the run's lock
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
    monkeypatch.setattr(W, "time", _Clock(sleep))
    with pytest.raises(KeyboardInterrupt):
        W.view(tmp_path, "claude", Console(file=io.StringIO()), run_id="test")
    assert len(created) >= 2 and set(created) == {"Iteration 1 · agent"}


def _running(data: dict, started: float) -> dict:
    """`data` while iteration 3 runs (its agent phase started at `started`)."""
    data["run"]["status"] = {"state": "running", "phase": "agent", "iteration": 3,
                             "iteration_started_at": started, "phase_started_at": started}
    data["run"]["progress"]["iterations"] = 2
    return data


def test_the_view_times_its_live_row_when_drawn(tmp_path):
    """The live row's clock and spinner come from the time it is drawn, not when the
    screen was built: the screen can stay up between rereads and still move."""
    from autohelix.terminal import LiveRow

    log = W.LogFollower(tmp_path, None, Console(file=io.StringIO()), "test")
    started = time.time() - 72
    status = {"state": "running", "phase": "agent", "iteration": 3}
    screen = W.view_screen((_running(_run_data(3), started), W.run_table.Saved()), log, status, 40)
    row = next(part for part in screen.renderables if isinstance(part, LiveRow))
    now = [started + 72]
    row.clock = lambda: now[0]

    def drawn() -> str:
        out = io.StringIO()
        Console(file=out, width=100, force_terminal=False).print(row)
        return out.getvalue().rstrip()

    first = drawn()
    now[0] += 61.25
    second = drawn()
    assert first.split()[1:] == ["3", "1m", "12s", "agent"]
    assert second.split()[1:] == ["3", "2m", "13s", "agent"] and second[2] != first[2]  # a new spinner frame
    # `--once` is a fixed line at the time given.
    once = W.run_table.format_run(_running(_run_data(3), started), W.run_table.Saved(), now=started + 72)
    assert once.splitlines()[-1].split()[1:] == ["3", "1m", "12s", "agent"]


def test_a_live_view_keeps_a_blank_line_at_the_bottom(tmp_path):
    log = W.LogFollower(tmp_path, None, Console(file=io.StringIO()), "test")
    data = (_running(_run_data(3), time.time()), W.run_table.Saved())
    running = W.view_screen(data, log, {"state": "running", "phase": "agent", "iteration": 3}, 40).renderables
    assert [line.plain for line in running[-2:]] == ["", ""] and running[-3].plain.startswith("Running")
    stopped = W.view_screen((_run_data(3), W.run_table.Saved()), log, {"state": "finished"}, 40).renderables
    assert stopped[-1].plain.startswith("Finished")


def test_the_view_rereads_the_run_only_when_it_changed(tmp_path, monkeypatch, run_paths):
    """Rereading the run (history, config, git) is slow: the view does it when the status
    or the run's files change, or after REREAD_SECONDS, not on every poll."""
    status = RunStatus(tmp_path, latest_run(tmp_path).run_id)
    status.start()
    status.phase("agent", 1)
    reads = []
    monkeypatch.setattr(W, "_read_run", lambda *a: reads.append(1))
    polls = iter(range(8))
    clock = [time.monotonic()]

    def sleep(_):
        clock[0] += 0.5  # 4.5 s in all: under REREAD_SECONDS
        n = next(polls, None)
        if n is None:
            raise KeyboardInterrupt
        if n == 3:
            with open(run_paths.history, "a") as f:  # an iteration ended
                f.write("{}\n")
        if n == 5:
            status.phase("checks", 1)

    fake = _Clock(sleep)
    fake.monotonic = lambda: clock[0]
    monkeypatch.setattr(W, "time", fake)
    with pytest.raises(KeyboardInterrupt):
        W.view(tmp_path, None, Console(file=io.StringIO()), run_id="test")
    assert len(reads) == 3  # the first poll, the history, the status


def test_the_views_live_row_names_the_observe_entry(tmp_path, run_paths):
    """From the status the harness writes, through the `view --json` payload."""
    from autohelix.viewer import ProjectRuns

    status = RunStatus(tmp_path, latest_run(tmp_path).run_id)
    status.lock = RunLock(latest_run(tmp_path).lock)  # a running segment holds the run's lock
    status.start()
    status.phase("observe", 1)
    status.note(entry="bench")
    data = ProjectRuns(tmp_path).run("test")
    assert data["run"]["status"]["entry"] == "bench"
    row = W.run_table.format_run(data, W.run_table.Saved()).splitlines()[-1]
    assert row.split()[1] == "1" and row.split()[-3:] == ["observe", "·", "bench"]
