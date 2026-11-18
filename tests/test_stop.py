"""`autohelix stop`: a request file the running segment reads between iterations, and
--now, which also interrupts it (SIGINT, as Ctrl-C would)."""

import json
import re
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import autohelix
from autohelix import cli as cli_module
from autohelix.harness import Budget, Harness
from autohelix.history import History
from autohelix.live import status_line
from autohelix.run_status import read_segments, write_stop_request
from autohelix.runs import RunLock, RunPaths, critical_section, git, short_id, write_json
from autohelix.viewer import ProjectRuns, RunData
from autohelix import run_table
from tests.helpers import cli, configure, latest_run


def iterations(paths):
    return [r.iteration for r in History(paths.project, paths.run_id).load()]


def status(paths):
    return json.loads(paths.status.read_text())


def stop_gate(at: int, *flags: str) -> str:
    """A gate that runs `autohelix stop` on its own run at iteration `at`, then passes."""
    run = '"$AUTOHELIX_RUN_ID"'
    return (f'if [ "$AUTOHELIX_ITERATION" = {at} ]; then '
            f'autohelix stop {run} -p "$AUTOHELIX_PROJECT" {" ".join(flags)}; fi; true')


def main_tip(project):
    return git(project, "rev-parse", "HEAD").stdout.strip()


# --- the harness: graceful requests ---------------------------------------------------


def test_a_request_during_an_iteration_ends_the_segment_after_it(project):
    configure(project, gates=[stop_gate(1, "--why", "'enough data'")])
    before = main_tip(project)
    harness = Harness(project, new=True)
    paths = harness.paths
    harness.run(Budget(iterations=3), report=False)
    assert iterations(paths) == [0, 1]  # iteration 1 finished (and was accepted), no iteration 2
    assert History(project, paths.run_id).load()[-1].accepted
    reason = status(paths)["stop_reason"]
    assert reason["kind"] == "request" and reason["why"] == "enough data"
    assert reason["detail"] == "requested by autohelix stop: enough data"
    assert isinstance(reason["requested_at"], float)
    assert status(paths)["state"] == "finished"
    assert main_tip(project) != before  # auto landing ran, as at a budget end
    [segment] = read_segments(paths.segments)
    assert segment["stop_reason"] == reason and segment["state"] == "finished" and not segment["open"]
    assert not paths.stop_request.exists()  # removed at the segment's end


def test_a_request_during_the_baseline_stops_after_the_baseline(project):
    configure(project, gates=[stop_gate(0)])
    result = cli(project, "run", "-n", "3", "--no-report")
    assert result.exit_code == 0, result.output
    paths = latest_run(project)
    assert iterations(paths) == [0]
    assert "  Stopped    requested by autohelix stop" in result.output
    reason = status(paths)["stop_reason"]
    assert isinstance(reason.pop("requested_at"), float)
    assert reason == {"kind": "request", "detail": "requested by autohelix stop", "why": None}
    # The gate's own output: what the request said to its caller.
    gate_out = (paths.iteration_dir(0) / "gates" / "1" / "stdout.txt").read_text()
    assert f"Run {short_id(paths.run_id)} stops after the baseline." in gate_out
    # From the gate's folder the advice names the project (relative or absolute, whichever is shorter).
    advice = re.search(rf"See when it has ended: autohelix wait -p (\S+) {short_id(paths.run_id)} "
                       r"\(repeat until it prints ended or idle\)", gate_out)
    assert advice and Path(advice[1]).name == "project"
    # A resume continues as usual: nothing is left of the request (the gate asks only at 0).
    result = cli(project, "resume", "-n", "2", "--no-report")
    assert result.exit_code == 0, result.output
    assert iterations(paths) == [0, 1, 2]


@pytest.mark.parametrize("segment", [1, 7])
def test_a_stale_request_is_removed_at_the_start_and_ignored(project, monkeypatch, segment):
    """Another segment's request (7), or one for the number this segment gets (1) left
    before it started: neither stops it, and neither is there once the status says running."""
    harness = Harness(project, new=True)
    paths = harness.paths
    write_stop_request(paths, segment=segment, why="old", now=False)
    original = harness.run_status.start

    def start(**info):
        assert not paths.stop_request.exists()
        original(**info)

    monkeypatch.setattr(harness.run_status, "start", start)
    harness.run(Budget(iterations=2), report=False)
    assert iterations(paths) == [0, 1, 2]
    assert status(paths)["stop_reason"]["kind"] == "budget"
    assert status(paths)["segment"] == 1


def test_a_request_for_another_segment_written_while_running_is_ignored(project, monkeypatch):
    harness = Harness(project, new=True)
    paths = harness.paths
    original = harness.run_iteration

    def iteration(n):
        write_stop_request(paths, segment=harness._segment + 1, why=None, now=False)
        return original(n)

    monkeypatch.setattr(harness, "run_iteration", iteration)
    harness.run(Budget(iterations=2), report=False)
    assert iterations(paths) == [0, 1, 2]


def test_an_unreadable_request_is_no_request(project, monkeypatch):
    harness = Harness(project, new=True)
    paths = harness.paths
    original = harness.run_iteration

    def iteration(n):
        paths.stop_request.write_text("{not json")
        return original(n)

    monkeypatch.setattr(harness, "run_iteration", iteration)
    harness.run(Budget(iterations=2), report=False)
    assert iterations(paths) == [0, 1, 2] and not paths.stop_request.exists()


def test_the_status_carries_the_segment_number(project):
    result = cli(project, "run", "--no-report")
    assert result.exit_code == 0, result.output
    paths = latest_run(project)
    assert status(paths)["segment"] == 1
    assert cli(project, "resume", "--no-report").exit_code == 0
    assert status(paths)["segment"] == 2 == read_segments(paths.segments)[-1]["segment"]


# --- the harness: --now ---------------------------------------------------------------


def test_an_interrupt_with_a_now_request_notes_it_as_the_stop_reason(project, monkeypatch, capsys):
    harness = Harness(project, new=True)
    paths = harness.paths

    def interrupted(n):
        write_stop_request(paths, segment=harness._segment, why="wrong direction", now=True)
        raise KeyboardInterrupt  # what the SIGINT `stop --now` sends raises

    monkeypatch.setattr(harness, "run_iteration", interrupted)
    assert harness.run(Budget(iterations=5), report=False) == "interrupted"  # the CLI exits 130
    reason = status(paths)["stop_reason"]
    assert reason["kind"] == "request" and reason["now"] is True and reason["why"] == "wrong direction"
    assert reason["detail"] == "requested by autohelix stop --now: wrong direction"
    assert status(paths)["state"] == "interrupted"
    [segment] = read_segments(paths.segments)
    assert segment["state"] == "interrupted" and segment["stop_reason"] == reason
    assert not paths.stop_request.exists()
    assert "Interrupted by autohelix stop --now at iteration 1" in " ".join(capsys.readouterr().out.split())


def test_ctrl_c_with_a_graceful_request_pending_is_a_plain_interrupt(project, monkeypatch):
    harness = Harness(project, new=True)
    paths = harness.paths

    def interrupted(n):
        write_stop_request(paths, segment=harness._segment, why=None, now=False)
        raise KeyboardInterrupt

    monkeypatch.setattr(harness, "run_iteration", interrupted)
    harness.run(Budget(iterations=5), report=False)
    [segment] = read_segments(paths.segments)
    assert (segment["state"], segment["stop_reason"]) == ("interrupted", None)


def test_a_now_request_during_the_baseline_is_noted_too(project, monkeypatch):
    harness = Harness(project, new=True)
    paths = harness.paths

    def baseline():
        write_stop_request(paths, segment=harness._segment, why=None, now=True)
        raise KeyboardInterrupt

    monkeypatch.setattr(harness, "_capture_baseline", baseline)
    with pytest.raises(KeyboardInterrupt):
        harness.run(Budget(iterations=5), report=False)
    [segment] = read_segments(paths.segments)
    assert segment["state"] == "interrupted"
    assert segment["stop_reason"]["kind"] == "request" and segment["stop_reason"]["now"] is True


# --- the CLI ---------------------------------------------------------------------------

@pytest.fixture
def running(project):
    """A run whose status says it is running here (this process, which holds its lock),
    in segment 2."""
    result = cli(project, "run", "--no-report")
    assert result.exit_code == 0, result.output
    paths = latest_run(project)
    data = status(paths)
    data.update(state="running", pid=os.getpid(), host=socket.gethostname(), segment=2, iteration=3,
                phase="agent")
    paths.status.write_text(json.dumps(data))
    with RunLock(paths.lock):
        yield paths


def set_status(paths, **fields):
    data = status(paths)
    data.update(fields)
    paths.status.write_text(json.dumps(data))


def test_stop_writes_a_request_for_the_running_segment(project, running):
    short = short_id(running.run_id)
    result = cli(project, "stop", short, "--why", "done for today")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [f"Run {short} stops after its current iteration (3).",
                                          f"See when it has ended: autohelix wait -p {project} {short} "
                                          "(repeat until it prints ended or idle)"]
    request = json.loads(running.stop_request.read_text())
    assert {k: request[k] for k in ("why", "now", "segment", "host", "pid")} == {
        "why": "done for today", "now": False, "segment": 2, "host": socket.gethostname(), "pid": os.getpid()}
    assert isinstance(request["requested_at"], float)
    assert sorted(p.name for p in running.internal.iterdir() if "stop" in p.name) == ["stop.json"]  # no tmp left
    # Again: the request is replaced (a new why), the full id works too.
    assert cli(project, "stop", running.run_id).exit_code == 0
    assert json.loads(running.stop_request.read_text())["why"] is None


@pytest.mark.parametrize("fields, message", [
    ({"iteration": 0, "phase": "baseline"}, "stops after the baseline."),
    ({"iteration": 4, "phase": "between iterations"}, "stops after its current iteration (4)."),
    ({"iteration": None, "phase": "starting"}, "stops after its current iteration."),
])
def test_stop_says_what_happens_next(project, running, fields, message):
    set_status(running, **fields)
    result = cli(project, "stop", short_id(running.run_id))
    assert result.exit_code == 0 and f"Run {short_id(running.run_id)} {message}" in result.output


def test_stop_refuses_runs_that_are_not_running(project):
    result = cli(project, "run", "--no-report")
    paths = latest_run(project)
    short = short_id(paths.run_id)
    result = cli(project, "stop", short)
    assert result.exit_code == 1 and f"Run {short} is not running (finished)." in result.output
    assert not paths.stop_request.exists()
    set_status(paths, state="running", pid=2 ** 22 + 12345, host=socket.gethostname())  # a dead process
    result = cli(project, "stop", short)
    assert result.exit_code == 1 and "is not running (its process is gone; it stopped unexpectedly)." in result.output
    assert not paths.stop_request.exists()


def test_stop_refuses_a_running_status_whose_lock_is_free(project, monkeypatch):
    """A process killed before it could update its status (SIGKILL), its pid taken by
    another autohelix process: the status says running, but nobody holds the run's lock."""
    result = cli(project, "run", "--no-report")
    paths = latest_run(project)
    set_status(paths, state="running", pid=os.getpid(), host=socket.gethostname(), segment=1, iteration=1)
    monkeypatch.setattr(cli_module, "_process_command", lambda pid: "python -m autohelix run")
    kills = []
    real_kill = os.kill
    monkeypatch.setattr(os, "kill", lambda pid, sig: real_kill(pid, 0) if sig == 0 else kills.append((pid, sig)))
    for flags in ([], ["--now"]):
        result = cli(project, "stop", short_id(paths.run_id), *flags)
        assert result.exit_code == 1 and "is not running (its process is gone" in result.output
    assert not paths.stop_request.exists() and not kills
    RunLock(paths.lock).close()  # the probe let it go


def test_a_status_without_a_segment_is_not_running(project, running):
    data = status(running)
    del data["segment"]  # no released version writes one: no special message for it
    running.status.write_text(json.dumps(data))
    result = cli(project, "stop", short_id(running.run_id))
    assert result.exit_code == 1 and f"Run {short_id(running.run_id)} is not running." in result.output
    assert not running.stop_request.exists()


def test_stop_with_an_unknown_id_stops_nothing(project, running):
    short = short_id(running.run_id)
    result = cli(project, "stop", "zzzz", short)
    assert result.exit_code == 1
    assert "No run 'zzzz'" in result.output and "Nothing was done." in result.output
    assert not running.stop_request.exists()  # ids are resolved before anything is asked


def test_stop_handles_every_known_id_and_fails_if_any_failed(project, running):
    short = short_id(running.run_id)
    other = RunPaths(project, "20260101-0000-idle-zzzz")
    write_json(other.identity, {"schema": 2, "id": other.run_id, "created_at": 1.0})
    result = cli(project, "stop", "zzzz", short)
    assert result.exit_code == 1
    assert "Run zzzz is not running." in result.output
    assert f"Run {short} stops after its current iteration (3)." in result.output
    assert (f"See when it has ended: autohelix wait -p {project} {short} (repeat until it prints ended or idle)"
            in result.output)
    assert running.stop_request.exists()


def test_stop_needs_an_id(project):
    result = cli(project, "stop")
    assert result.exit_code == 2 and "Missing argument" in result.output


def test_stop_now_only_on_the_runs_host(project, running, monkeypatch):
    set_status(running, host="elsewhere.example.com")
    kills = []
    monkeypatch.setattr(cli_module.os, "kill", lambda *args: kills.append(args))
    result = cli(project, "stop", short_id(running.run_id), "--now")
    assert result.exit_code == 1
    assert "running on elsewhere.example.com; --now works only there" in result.output
    assert "Without --now it stops after its current iteration" in " ".join(result.output.split())
    assert not running.stop_request.exists() and not kills


def test_stop_now_refuses_a_pid_that_is_not_autohelix(project, running, monkeypatch):
    monkeypatch.setattr(cli_module, "_process_command", lambda pid: "/usr/bin/vim notes.txt")
    real_kill = os.kill
    kills = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: real_kill(pid, 0) if sig == 0 else kills.append((pid, sig)))
    result = cli(project, "stop", short_id(running.run_id), "--now")
    assert result.exit_code == 1 and "is not an autohelix process; not interrupted" in result.output
    assert not running.stop_request.exists() and not kills


def test_stop_now_writes_the_request_then_sends_sigterm(project, running, monkeypatch):
    seen = []
    monkeypatch.setattr(cli_module, "_process_command", lambda pid: "/venv/bin/python /venv/bin/autohelix run")
    real_kill = os.kill

    def kill(pid, sig):
        if sig == 0:
            return real_kill(pid, 0)  # read_run_status's liveness probe
        seen.append((pid, sig, json.loads(running.stop_request.read_text())))

    monkeypatch.setattr(os, "kill", kill)
    short = short_id(running.run_id)
    result = cli(project, "stop", short, "--now", "--why", "wrong direction")
    assert result.exit_code == 0, result.output
    [(pid, sig, request)] = seen
    assert (pid, sig) == (os.getpid(), signal.SIGTERM)
    assert request["now"] is True and request["why"] == "wrong direction" and request["segment"] == 2
    assert f"Interrupting run {short} (iteration 3)." in result.output


def test_a_second_now_sends_nothing(project, running, monkeypatch):
    sent = []
    monkeypatch.setattr(cli_module, "_process_command", lambda pid: "/venv/bin/python /venv/bin/autohelix run")
    real_kill = os.kill
    monkeypatch.setattr(os, "kill", lambda pid, sig: real_kill(pid, 0) if sig == 0 else sent.append(sig))
    write_stop_request(running, segment=2, why=None, now=True)
    short = short_id(running.run_id)
    result = cli(project, "stop", short, "--now")
    assert result.exit_code == 0, result.output
    assert sent == [] and f"Run {short} is already being interrupted." in result.output


def test_a_failed_interrupt_says_it_still_stops_after_its_iteration(project, running, monkeypatch):
    monkeypatch.setattr(cli_module, "_process_command", lambda pid: "python -m autohelix run")
    real_kill = os.kill

    def kill(pid, sig):
        if sig == 0:
            return real_kill(pid, 0)
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(os, "kill", kill)
    result = cli(project, "stop", short_id(running.run_id), "--now")
    assert result.exit_code == 1
    assert "Could not interrupt run" in result.output
    assert "it still stops after its current iteration" in " ".join(result.output.split())
    assert json.loads(running.stop_request.read_text())["now"] is True


def test_process_command_reads_ps():
    assert "python" in cli_module._process_command(os.getpid()).lower()
    assert cli_module._process_command(2 ** 22 + 12345) == ""


def test_stop_now_ends_a_background_run_that_ignores_sigint(project, tmp_path):
    """A run a script started in the background (`autohelix run ... &`: SIGINT ignored),
    in a slow gate at iteration 1: `stop --now` interrupts it at once, kills the gate's
    processes, and the segment records the request."""
    marks = tmp_path / "marks"
    marks.mkdir()
    gate = (f'if [ "$AUTOHELIX_ITERATION" = 1 ]; then echo $$ > "{marks}/gate.tmp"; '
            f'mv "{marks}/gate.tmp" "{marks}/gate.pid"; sleep 300 & wait; fi')
    configure(project, gates=[gate])
    env = {**os.environ, "PYTHONPATH": str(Path(autohelix.__file__).resolve().parents[1])}
    with (tmp_path / "run.log").open("w") as log:
        process = subprocess.Popen([sys.executable, "-m", "autohelix.cli", "run", "-n", "3", "--no-report"],
                                   cwd=project, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL,
                                   preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN))
    try:
        deadline = time.monotonic() + 120
        while not (marks / "gate.pid").exists():
            assert process.poll() is None, (tmp_path / "run.log").read_text()
            assert time.monotonic() < deadline, (tmp_path / "run.log").read_text()
            time.sleep(0.1)
        group = int((marks / "gate.pid").read_text())
        paths = latest_run(project)
        result = cli(project, "stop", short_id(paths.run_id), "--now", "--why", "enough")
        assert result.exit_code == 0, result.output
        assert process.wait(timeout=60) == 130  # interrupted
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    log_text = (tmp_path / "run.log").read_text()
    assert "Interrupted by autohelix stop --now at iteration 1" in " ".join(log_text.split()), log_text
    data = status(paths)
    assert data["state"] == "interrupted"
    assert data["stop_reason"]["kind"] == "request" and data["stop_reason"]["now"] is True
    assert data["stop_reason"]["why"] == "enough"
    [segment] = read_segments(paths.segments)
    assert segment["stop_reason"] == data["stop_reason"] and segment["state"] == "interrupted"
    records = History(project, paths.run_id).load()
    assert [(r.iteration, r.accepted) for r in records] == [(0, True), (1, False)]
    assert records[-1].reason == "interrupted"
    with pytest.raises(ProcessLookupError):  # nothing left of the gate's process group
        os.killpg(group, 0)
    assert not paths.stop_request.exists()


def test_resume_after_a_stop_says_nothing_extra(project):
    configure(project, gates=[stop_gate(1)])
    result = cli(project, "run", "-n", "3", "--no-report")
    assert result.exit_code == 0, result.output
    result = cli(project, "resume", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert "stopped because" not in result.output and "interrupted segment" not in result.output


# --- dashboard / view -----------------------------------------------------------------


def test_the_run_row_shows_a_pending_request(project, running):
    runs = ProjectRuns(project)
    assert runs.run(running.run_id)["run"]["stop_requested"] is None
    write_stop_request(running, segment=2, why="enough", now=False)
    data = ProjectRuns(project).run(running.run_id)
    asked = data["run"]["stop_requested"]
    assert asked["why"] == "enough" and asked["now"] is False and isinstance(asked["requested_at"], float)
    assert data["run"]["status"]["segment"] == 2
    shown = run_table.format_run(data, run_table.Saved.read(running))
    assert " · running (stopping)\n" in shown  # the title
    assert "agent · stopping" in shown  # the live row
    listing = ProjectRuns(project).runs()
    assert "running (stopping)" in run_table.format_runs(listing, width=200)
    # Another segment's request, or a run that isn't running: nothing pending.
    write_stop_request(running, segment=1, why=None, now=False)
    assert ProjectRuns(project).run(running.run_id)["run"]["stop_requested"] is None
    write_stop_request(running, segment=2, why=None, now=False)
    set_status(running, state="finished")
    data = ProjectRuns(project).run(running.run_id)
    assert data["run"]["stop_requested"] is None
    assert "stopping" not in run_table.format_run(data, run_table.Saved.read(running))
    assert RunData(project, running.run_id).state()["stop_requested"] is None
    set_status(running, state="running")
    assert RunData(project, running.run_id).state()["stop_requested"]["now"] is False  # the run page


def test_an_interrupted_status_shows_its_stop_reason():
    reason = {"kind": "request", "now": True, "detail": "requested by autohelix stop --now: enough"}
    assert "Interrupted · requested by autohelix stop --now: enough." in status_line(
        {"state": "interrupted", "stop_reason": reason})
    assert status_line({"state": "interrupted"}).startswith("Interrupted. Waiting")


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_critical_sections_defer_sigint_and_sigterm(signum):
    seen = []
    previous = signal.signal(signum, lambda *args: seen.append("handled"))
    try:
        with critical_section():
            signal.raise_signal(signum)
            seen.append("section done")
        assert seen == ["section done", "handled"]
    finally:
        signal.signal(signum, previous)


def test_an_ignored_signal_stays_ignored_in_a_critical_section():
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        with critical_section():
            signal.raise_signal(signal.SIGINT)
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGINT, previous)


def test_commands_start_with_default_signals_in_a_run_that_ignores_them(project, tmp_path):
    """A run a script started in the background ignores SIGINT and SIGQUIT; its gate
    commands, and what they start, don't inherit that (a project's own Ctrl-C tests
    run as a gate must pass). The run itself still ignores both afterwards."""
    seen = tmp_path / "seen"
    check = ("import signal, sys; ok = signal.getsignal(signal.SIGINT) is signal.default_int_handler "
             "and signal.getsignal(signal.SIGQUIT) == signal.SIG_DFL; "
             f"open({str(seen)!r}, 'a').write(str(ok) + chr(10)); sys.exit(0 if ok else 1)")
    configure(project, gates=[f'"{sys.executable}" -c "{check}"'])
    previous = {signum: signal.signal(signum, signal.SIG_IGN) for signum in (signal.SIGINT, signal.SIGQUIT)}
    try:
        Harness(project, new=True).run(Budget(iterations=1), report=False)
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
        assert signal.getsignal(signal.SIGQUIT) == signal.SIG_IGN
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    assert seen.read_text().split() == ["True", "True"]  # the baseline and iteration 1
    records = History(project, latest_run(project).run_id).load()
    assert [(r.iteration, r.accepted) for r in records] == [(0, True), (1, True)]


def test_a_segment_takes_sigterm_as_ctrl_c_and_restores_it(project, monkeypatch):
    harness = Harness(project, new=True)

    def terminated(n):
        signal.raise_signal(signal.SIGTERM)  # as `kill PID`
        raise AssertionError("not reached")

    monkeypatch.setattr(harness, "run_iteration", terminated)
    # Another test in this worker (`ask`/`review`'s CLI) may have left its own handler.
    previous = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        harness.run(Budget(iterations=3), report=False)
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
    finally:
        signal.signal(signal.SIGTERM, previous)
    [segment] = read_segments(harness.paths.segments)
    assert segment["state"] == "interrupted" and segment["stop_reason"] is None


def test_an_interrupt_during_the_landing_waits_for_it(project, monkeypatch):
    import autohelix.harness as harness_module

    real_land = harness_module.land

    def land(*args, **kwargs):
        signal.raise_signal(signal.SIGTERM)  # `stop --now` (or a second Ctrl-C) as the ending lands
        return real_land(*args, **kwargs)

    monkeypatch.setattr(harness_module, "land", land)
    before = main_tip(project)
    harness = Harness(project, new=True)
    previous = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        with pytest.raises(KeyboardInterrupt):
            harness.run(Budget(iterations=1), report=False)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert main_tip(project) != before  # landed first, then interrupted
    [segment] = read_segments(harness.paths.segments)
    assert segment["state"] == "interrupted" and segment["stop_reason"]["kind"] == "budget"
