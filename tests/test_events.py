"""`autohelix wait` and its event source (`autohelix.events`): hand-written event logs
(runs/<id>/.internal/segments.jsonl) for each event kind, ordering and the cursor; then
real harness runs."""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from autohelix.cli import main
from autohelix.events import EVENT_SCHEMA, Marks, Watcher, decode_cursor, encode_cursor
from autohelix.runs import SCHEMA, RunLock, RunPaths, held_here, short_id, write_json
from tests.helpers import latest_run

HOST = socket.gethostname()
T0 = 1_700_000_000.0
EMPTY = encode_cursor({})  # before anything: every event is pending


def make_run(project: Path, run_id: str, created_at: float = T0) -> RunPaths:
    paths = RunPaths(project, run_id)
    write_json(paths.identity, {"schema": SCHEMA, "id": run_id, "config_path": "autohelix.yaml",
                                "created_at": created_at, "base_commit": "0" * 40})
    return paths


def append(path: Path, record: dict, end: str = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as stream:
        stream.write(json.dumps(record) + end)


def record(paths, iteration, at, accepted=True, segment=1, **fields):
    """The harness's line for a final iteration record (`SegmentLog.iteration`)."""
    data = {"segment": segment, "event": "iteration", "ended_at": at, "iteration": iteration,
            "accepted": accepted, "metrics": {}, "commit": None, "reason": None, "summary": None,
            "agent_error": None, "cost_usd": 0.5, "cost_incomplete": False, **fields}
    append(paths.segments, data)
    return data


@pytest.fixture(autouse=True)
def _held():
    """The locks of runs this process "runs" (alive while held), released after each test."""
    yield _HELD
    while _HELD:
        _HELD.pop().close()


_HELD: list[RunLock] = []


def hold(paths) -> None:
    """Hold the run's lock, as the process running it does: a segment with this
    process's pid is alive."""
    if not held_here(paths.lock):
        _HELD.append(RunLock(paths.lock))


def start(paths, segment, at, pid=None, host=HOST, start_iteration=0):
    if pid is None:
        hold(paths)
    append(paths.segments, {"segment": segment, "event": "start", "started_at": at, "pid": pid or os.getpid(),
                            "host": host, "budget": {}, "start_iteration": start_iteration})


def end(paths, segment, at, state="finished", stop_reason=None, last_iteration=1, iterations=2):
    append(paths.segments, {"segment": segment, "event": "end", "ended_at": at, "state": state,
                            "stop_reason": stop_reason, "last_iteration": last_iteration,
                            "iterations": iterations, "error": None})


def dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def drain(watcher: Watcher) -> list[dict]:
    events = []
    while (event := watcher.poll()) is not None:
        events.append(event)
    return events


def brief(events):
    return [(e["event"], e["run"] and short_id(e["run"]), e.get("iteration", e.get("segment"))) for e in events]


def finished_run(project, run_id="20261114-1000-a-aaaa", t=T0):
    """Segment 1: the baseline and iteration 1, then the end."""
    paths = make_run(project, run_id)
    start(paths, 1, t)
    record(paths, 0, t + 2, reason="baseline")
    record(paths, 1, t + 4, commit="c1", metrics={"speed": 2.0}, summary="faster")
    end(paths, 1, t + 5, stop_reason={"kind": "budget", "budget": "iterations", "detail": "iteration budget reached (1)"})
    return paths


# --- each event kind ------------------------------------------------------------------


def test_a_segment_reports_started_iterations_and_ended_in_order(tmp_path):
    paths = finished_run(tmp_path)
    events = drain(Watcher(tmp_path, after=EMPTY))
    assert brief(events) == [("started", "aaaa", 1), ("iteration", "aaaa", 0), ("iteration", "aaaa", 1),
                             ("ended", "aaaa", 1)]
    started, baseline, first, ended = events
    for event in events:
        assert event["schema"] == EVENT_SCHEMA and event["run"] == paths.run_id and event["short"] == "aaaa"
    assert (started["time"], started["start_iteration"], started["resumed"]) == (T0, 0, False)
    assert (baseline["reason"], baseline["accepted"], baseline["time"]) == ("baseline", True, T0 + 2)
    assert {k: first[k] for k in ("accepted", "commit", "metrics", "summary", "cost_usd", "cost_incomplete",
                                  "agent_error")} == {
        "accepted": True, "commit": "c1", "metrics": {"speed": 2.0}, "summary": "faster", "cost_usd": 0.5,
        "cost_incomplete": False, "agent_error": None}
    assert (ended["state"], ended["stop_reason"]["kind"], ended["error"], ended["last_iteration"],
            ended["iterations"], ended["time"]) == ("finished", "budget", None, 1, 2, T0 + 5)
    assert list(ended)[-1] == "cursor"


def test_a_resume_is_a_started_event_with_resumed(tmp_path):
    paths = finished_run(tmp_path)
    watcher = Watcher(tmp_path)  # from now: nothing pending
    assert watcher.poll() is None
    start(paths, 2, T0 + 10, start_iteration=2)
    [event] = drain(watcher)
    assert (event["event"], event["segment"], event["start_iteration"], event["resumed"]) == ("started", 2, 2, True)


def test_a_rejected_iteration_carries_its_reason_and_unknown_cost(tmp_path):
    paths = make_run(tmp_path, "20261114-1000-a-aaaa")
    start(paths, 1, T0)
    record(paths, 3, T0 + 1, accepted=False, reason="gate failed: pytest -q", cost_usd=None, cost_incomplete=True)
    record(paths, 4, T0 + 2, accepted=False, reason="Iteration 4 agent failed: boom", agent_error="boom",
           cost_usd=0.25)
    _, rejected, failed = drain(Watcher(tmp_path, after=EMPTY))
    assert (rejected["accepted"], rejected["reason"], rejected["cost_usd"], rejected["cost_incomplete"]) == (
        False, "gate failed: pytest -q", None, True)
    assert (failed["agent_error"], failed["cost_usd"]) == ("boom", 0.25)


# --- ordering ---------------------------------------------------------------------------


def test_events_of_several_runs_come_oldest_first(tmp_path):
    a = make_run(tmp_path, "20261114-1000-a-aaaa")
    b = make_run(tmp_path, "20261114-1000-b-bbbb")
    start(a, 1, T0)
    start(b, 1, T0 + 1)
    record(b, 0, T0 + 2)
    record(a, 0, T0 + 3)
    record(a, 1, T0 + 4)  # same time as b's end: an iteration before an end
    end(b, 1, T0 + 4)
    events = drain(Watcher(tmp_path, after=EMPTY))
    assert brief(events) == [("started", "aaaa", 1), ("started", "bbbb", 1), ("iteration", "bbbb", 0),
                             ("iteration", "aaaa", 0), ("iteration", "aaaa", 1), ("ended", "bbbb", 1)]
    assert [e["time"] for e in events] == sorted(e["time"] for e in events)


def test_inverted_times_within_a_run_keep_the_log_order(tmp_path):
    """A clock step backwards loses nothing and reorders nothing within a run: its events
    come in the order of its log."""
    paths = make_run(tmp_path, "20261114-1000-a-aaaa")
    start(paths, 1, T0 + 100)
    record(paths, 0, T0 + 50)
    end(paths, 1, T0 + 40)
    start(paths, 2, T0 + 200)
    end(paths, 2, T0 + 30)
    events = drain(Watcher(tmp_path, after=EMPTY))
    assert brief(events) == [("started", "aaaa", 1), ("iteration", "aaaa", 0), ("ended", "aaaa", 1),
                             ("started", "aaaa", 2), ("ended", "aaaa", 2)]


# --- the cursor ---------------------------------------------------------------------------


def test_wait_after_cursor_misses_nothing_and_repeats_nothing(tmp_path):
    paths = make_run(tmp_path, "20261114-1000-a-aaaa")
    cursor = Watcher(tmp_path).cursor  # before anything happened
    start(paths, 1, T0)
    record(paths, 0, T0 + 1)
    seen = []
    for step in range(6):
        event = Watcher(tmp_path, after=cursor).poll()
        if event is None:
            break
        seen.append(event)
        cursor = event["cursor"]
        if step == 1:  # more happens between two waits
            record(paths, 1, T0 + 2)
            end(paths, 1, T0 + 3)
    assert brief(seen) == [("started", "aaaa", 1), ("iteration", "aaaa", 0), ("iteration", "aaaa", 1),
                           ("ended", "aaaa", 1)]
    assert Watcher(tmp_path, after=cursor).poll() is None


def test_a_fresh_wait_starts_from_now_for_every_run(tmp_path):
    finished_run(tmp_path, "20261114-1000-a-aaaa")
    b = finished_run(tmp_path, "20261114-1000-b-bbbb")
    # Taken with one run's id, it still covers the other: without ids, only what is new.
    cursor = Watcher(tmp_path, run_ids=["20261114-1000-a-aaaa"]).cursor
    assert Watcher(tmp_path, after=cursor).poll() is None
    start(b, 2, T0 + 10)
    assert brief([Watcher(tmp_path, after=cursor).poll()]) == [("started", "bbbb", 2)]


def test_cursors_round_trip_and_bad_ones_are_refused(tmp_path):
    marks = {"20261114-1000-a-aaaa": Marks(12, 1), "20261114-1000-b-bbbb": Marks(),
             "20261114-1000-c-cccc": Marks(5)}
    assert decode_cursor(encode_cursor(marks)) == {"20261114-1000-a-aaaa": Marks(12, 1),
                                                   "20261114-1000-c-cccc": Marks(5, 0)}
    good = encode_cursor(marks)
    assert good.startswith("2.")
    for bad in ["", "nonsense", "2.", "2.!!!", good[:-3], "2." + "A" * 40]:
        with pytest.raises(ValueError, match="not a cursor"):
            decode_cursor(bad)
    with pytest.raises(ValueError, match="older AutoHelix.*no --after"):
        decode_cursor("1." + good[2:])  # the previous format's: says so
    import base64
    import zlib
    for payload in [b"[]", b'{"r": []}', b'{"r": {"x/y": [1, 2]}}', b'{"r": {"a": [1, 2, 3]}}', b'{"r": {"a": [1]}}',
                    b'{"r": {"a": [1, true]}}', b'{"r": {"a": [-1, 0]}}']:
        cursor = "2." + base64.urlsafe_b64encode(zlib.compress(payload)).decode().rstrip("=")
        with pytest.raises(ValueError, match="not a cursor"):
            decode_cursor(cursor)


def test_every_cursor_printed_is_accepted(monkeypatch):
    from autohelix import events

    many = {f"20261114-1000-r-{i:04d}": Marks(300_000, 9) for i in range(2000)}
    assert decode_cursor(encode_cursor(many)) == many
    monkeypatch.setattr(events, "CURSOR_CAP", 1000)
    with pytest.raises(RuntimeError, match="Too many runs"):
        encode_cursor(many)


def test_a_new_run_after_wait_began_is_watched_without_ids(tmp_path):
    finished_run(tmp_path)
    watcher = Watcher(tmp_path)
    before = watcher.cursor
    assert watcher.poll() is None
    paths = make_run(tmp_path, "20261114-1100-new-nnnn", created_at=time.time())
    assert watcher.poll() is None  # created, no segment yet
    start(paths, 1, T0 + 100)
    [event] = drain(watcher)
    assert (event["event"], event["short"], event["resumed"]) == ("started", "nnnn", False)
    # A cursor taken before it existed: everything it has is new.
    record(paths, 0, T0 + 101)
    assert brief(drain(Watcher(tmp_path, after=before))) == [("started", "nnnn", 1), ("iteration", "nnnn", 0)]


def test_deleted_runs_leave_the_cursor(tmp_path):
    paths = finished_run(tmp_path)
    cursor = Watcher(tmp_path).cursor
    assert paths.run_id in decode_cursor(cursor)
    other = make_run(tmp_path, "20261114-1000-b-bbbb")
    start(other, 1, T0 + 20)
    import shutil
    shutil.rmtree(paths.run_dir)
    event = Watcher(tmp_path, after=cursor).poll()
    assert set(decode_cursor(event["cursor"])) == {other.run_id}


# --- robustness ---------------------------------------------------------------------------


def test_a_torn_final_line_counts_once_it_is_complete(tmp_path):
    paths = make_run(tmp_path, "20261114-1000-a-aaaa")
    start(paths, 1, T0)
    record(paths, 0, T0)
    watcher = Watcher(tmp_path)
    line = json.dumps({"segment": 1, "event": "iteration", "iteration": 1, "accepted": False, "ended_at": T0 + 2})
    with open(paths.segments, "a") as stream:
        stream.write(line[:20])
    assert watcher.poll() is None
    with open(paths.segments, "a") as stream:
        stream.write(line[20:])  # complete JSON, but no newline yet
    assert watcher.poll() is None
    with open(paths.segments, "a") as stream:
        stream.write("\n")
    assert brief(drain(watcher)) == [("iteration", "aaaa", 1)]
    # A damaged line in the middle is skipped; what follows still counts.
    with open(paths.segments, "a") as stream:
        stream.write("{not json\n" + json.dumps({"segment": "x", "event": "iteration"}) + "\n"
                     + json.dumps({"segment": 1, "event": "other"}) + "\n")
    record(paths, 2, T0 + 3)
    assert brief(drain(watcher)) == [("iteration", "aaaa", 2)]
    assert decode_cursor(watcher.cursor)[paths.run_id] == Marks(7, 0)  # every complete line, damaged ones too


def test_a_retried_baseline_is_a_new_event(tmp_path):
    """Each final record is its own line: a retried baseline (record 0 again) too."""
    paths = make_run(tmp_path, "20261114-1000-a-aaaa")
    start(paths, 1, T0, pid=dead_pid())
    record(paths, 0, T0 + 1, accepted=False, reason="baseline failed: boom")
    end(paths, 1, T0 + 2, state="failed")
    watcher = Watcher(tmp_path, after=EMPTY)
    assert brief(drain(watcher)) == [("started", "aaaa", 1), ("iteration", "aaaa", 0), ("ended", "aaaa", 1)]
    start(paths, 2, T0 + 10)
    record(paths, 0, T0 + 12, segment=2, reason="baseline")
    events = drain(watcher)
    assert brief(events) == [("started", "aaaa", 2), ("iteration", "aaaa", 0)]
    assert (events[1]["accepted"], events[1]["reason"]) == (True, "baseline")


def test_a_dead_process_is_a_crash_after_its_last_event(tmp_path):
    paths = make_run(tmp_path, "20261114-1000-a-aaaa")
    start(paths, 1, T0, pid=dead_pid())
    record(paths, 1, T0 + 2, commit="c1")
    events = drain(Watcher(tmp_path, after=EMPTY))
    assert brief(events) == [("started", "aaaa", 1), ("iteration", "aaaa", 1), ("crashed", "aaaa", 1)]
    assert events[2]["time"] == T0 + 2 and events[2]["last_iteration"] == 1


def test_a_crash_is_reported_once(tmp_path):
    paths = make_run(tmp_path, "20261114-1000-a-aaaa")
    start(paths, 1, T0, pid=dead_pid())
    record(paths, 0, T0 + 1)
    watcher = Watcher(tmp_path, after=EMPTY)
    events = drain(watcher)
    assert brief(events) == [("started", "aaaa", 1), ("iteration", "aaaa", 0), ("crashed", "aaaa", 1)]
    assert (events[-1]["last_iteration"], events[-1]["time"]) == (0, T0 + 1)
    assert watcher.poll() is None
    assert Watcher(tmp_path, after=events[-1]["cursor"]).poll() is None
    assert Watcher(tmp_path).poll() is None  # a fresh wait: already crashed, not news
    # Resumed: the crashed segment stays open; the new one is reported, not another crash.
    start(paths, 2, T0 + 10, start_iteration=1)
    assert brief(drain(watcher)) == [("started", "aaaa", 2)]
    assert brief(drain(Watcher(tmp_path, after=events[-1]["cursor"]))) == [("started", "aaaa", 2)]


def test_lines_after_a_reported_crash_dont_report_it_again(tmp_path):
    """A record its process died before logging is appended to the dead segment at the
    next start (`SegmentLog.catch_up`), like any line written after a confirmed crash:
    reported late, the crash not again."""
    paths = make_run(tmp_path, "20261114-1000-a-aaaa")
    start(paths, 1, T0, pid=dead_pid())
    record(paths, 0, T0 + 1)
    watcher = Watcher(tmp_path, after=EMPTY)
    events = drain(watcher)
    assert brief(events)[-1] == ("crashed", "aaaa", 1)
    with open(paths.segments, "a") as stream:
        stream.write("not a record\n")
    assert watcher.poll() is None
    record(paths, 1, T0 + 3, commit="c1")  # the caught-up record
    start(paths, 2, T0 + 10, start_iteration=2)
    assert brief(drain(watcher)) == [("iteration", "aaaa", 1), ("started", "aaaa", 2)]
    assert brief(drain(Watcher(tmp_path, after=events[-1]["cursor"]))) == [
        ("iteration", "aaaa", 1), ("started", "aaaa", 2)]
    # Read for the first time, the caught-up record comes before the crash.
    assert brief(drain(Watcher(tmp_path, after=EMPTY))) == [
        ("started", "aaaa", 1), ("iteration", "aaaa", 0), ("iteration", "aaaa", 1), ("crashed", "aaaa", 1),
        ("started", "aaaa", 2)]


def test_the_iteration_fields_are_the_logged_ones():
    from autohelix.events import ITERATION_FIELDS, iteration_fields
    from autohelix.history import IterationResult

    assert tuple(iteration_fields(IterationResult(iteration=1, accepted=True))) == ITERATION_FIELDS


def test_a_crashed_segment_is_probed_once_and_a_live_one_every_look(tmp_path, monkeypatch):
    """The liveness probe takes a free lock for a moment: a crash, once confirmed, isn't
    probed again (until the log grows); a live segment's held lock is never taken."""
    from autohelix import events

    calls = []
    original = events.holder_gone
    monkeypatch.setattr(events, "holder_gone", lambda *args: calls.append(args) or original(*args))
    crashed = make_run(tmp_path, "20261114-1000-a-aaaa")
    start(crashed, 1, T0, pid=dead_pid())
    watcher = Watcher(tmp_path, run_ids=[crashed.run_id])
    for _ in range(5):
        assert watcher.poll() is None
    assert len(calls) == 1
    assert watcher.idle()["runs"][0]["state"] == "crashed" and len(calls) == 1
    live = make_run(tmp_path, "20261114-1000-b-bbbb")
    start(live, 1, T0)
    watcher = Watcher(tmp_path, run_ids=[live.run_id])
    calls.clear()
    for _ in range(3):
        watcher.poll()
    assert len(calls) == 3


def test_an_open_segment_followed_by_another_was_a_crash(tmp_path):
    """Missed while it was live (no wait running): reported before the next start."""
    paths = make_run(tmp_path, "20261114-1000-a-aaaa")
    start(paths, 1, T0)  # our own pid: alive, but a later segment started
    record(paths, 0, T0 + 1)
    start(paths, 2, T0 + 10, start_iteration=1)
    record(paths, 1, T0 + 12)
    assert brief(drain(Watcher(tmp_path, after=EMPTY))) == [
        ("started", "aaaa", 1), ("iteration", "aaaa", 0), ("crashed", "aaaa", 1), ("started", "aaaa", 2),
        ("iteration", "aaaa", 1)]


def test_a_live_segment_is_no_crash_and_an_ending_one_is_no_crash(tmp_path, monkeypatch):
    paths = make_run(tmp_path, "20261114-1000-a-aaaa")
    start(paths, 1, T0)
    watcher = Watcher(tmp_path, after=EMPTY)
    assert brief(drain(watcher)) == [("started", "aaaa", 1)]
    # The process writes its end record, then exits: seen gone before the record is read.
    from autohelix import events

    def gone(paths_, host, pid):
        if '"end"' not in paths.segments.read_text():
            end(paths, 1, T0 + 5)
        return True

    monkeypatch.setattr(events, "holder_gone", gone)
    assert brief(drain(watcher)) == [("ended", "aaaa", 1)]


def test_idle_never_takes_a_run_that_just_ended_for_a_crash(tmp_path, monkeypatch):
    """Its end record written and the process gone after the last look: idle looks again."""
    from autohelix import events

    paths = make_run(tmp_path, "20261114-1000-a-aaaa")
    start(paths, 1, T0)
    watcher = Watcher(tmp_path, run_ids=[paths.run_id])
    assert watcher.poll() is None
    end(paths, 1, T0 + 5)
    monkeypatch.setattr(events, "holder_gone", lambda paths, host, pid: True)
    assert watcher.idle()["event"] == "ended"
    assert watcher.idle()["runs"][0]["state"] == "finished"


def test_a_crash_reported_late_has_its_own_last_iteration(tmp_path):
    paths = make_run(tmp_path, "20261114-1000-a-aaaa")
    start(paths, 1, T0, pid=dead_pid())
    record(paths, 0, T0 + 2)
    start(paths, 2, T0 + 10, start_iteration=1)
    for n in (1, 2, 3):
        record(paths, n, T0 + 10 + n)
    [crash] = [e for e in drain(Watcher(tmp_path, after=EMPTY)) if e["event"] == "crashed"]
    assert (crash["segment"], crash["last_iteration"], crash["time"]) == (1, 0, T0 + 2)


def test_planted_files_read_as_missing(tmp_path):
    """Candidate code can't make wait block (a FIFO) or eat memory (a link to a device)."""
    paths = finished_run(tmp_path)
    good = paths.segments.read_text()
    paths.segments.unlink()
    os.mkfifo(paths.segments)
    assert drain(Watcher(tmp_path, after=EMPTY)) == []
    paths.segments.unlink()
    os.symlink("/dev/zero", paths.segments)
    assert drain(Watcher(tmp_path, after=EMPTY)) == []
    paths.segments.unlink()
    paths.segments.write_text(good)
    assert [e["event"] for e in drain(Watcher(tmp_path, after=EMPTY))] == ["started", "iteration", "iteration",
                                                                            "ended"]


def test_a_log_over_its_cap_reads_as_empty(tmp_path, monkeypatch):
    from autohelix import events

    paths = finished_run(tmp_path)
    monkeypatch.setattr(events, "LOG_CAP", paths.segments.stat().st_size - 1)
    assert drain(Watcher(tmp_path, after=EMPTY)) == []


def test_an_appended_log_is_read_on_and_a_replaced_one_again(tmp_path):
    paths = make_run(tmp_path, "20261114-1000-a-aaaa")
    start(paths, 1, T0)
    watcher = Watcher(tmp_path, after=EMPTY)
    assert brief(drain(watcher)) == [("started", "aaaa", 1)]
    record(paths, 0, T0 + 2)
    assert brief(drain(watcher)) == [("iteration", "aaaa", 0)]
    # Not the harness's doing (the log is only appended to), but read again from the start:
    # a shrunk file, and another file in its place.
    lines = paths.segments.read_text().splitlines(keepends=True)
    paths.segments.write_text(lines[0])
    assert watcher.poll() is None  # one line: nothing past the cursor's two
    tmp = paths.segments.with_suffix(".tmp")
    tmp.write_text("".join(lines) + json.dumps({"segment": 1, "event": "iteration", "iteration": 1,
                                                "accepted": True, "ended_at": T0 + 3}) + "\n")
    tmp.replace(paths.segments)
    assert brief(drain(watcher)) == [("iteration", "aaaa", 1)]


# --- idle and timeout ---------------------------------------------------------------------


def test_idle_with_ids_when_none_is_running(tmp_path):
    a = finished_run(tmp_path, "20261114-1000-a-aaaa")
    b = make_run(tmp_path, "20261114-1000-b-bbbb")  # created long ago, never started
    c = make_run(tmp_path, "20261114-1000-c-cccc")
    start(c, 1, T0, pid=dead_pid())
    watcher = Watcher(tmp_path, run_ids=[a.run_id, b.run_id, c.run_id])
    event = watcher.next(idle=True)
    assert (event["event"], event["run"], event["short"]) == ("idle", None, None)
    assert event["runs"] == [{"run": a.run_id, "short": "aaaa", "state": "finished"},
                             {"run": b.run_id, "short": "bbbb", "state": "new"},
                             {"run": c.run_id, "short": "cccc", "state": "crashed"}]
    # Pending events come first: from a cursor before them, the end is reported, not idle.
    event = Watcher(tmp_path, run_ids=[a.run_id], after=EMPTY).next(idle=True)
    assert event["event"] == "started"


def test_a_running_run_is_not_idle(tmp_path):
    running = make_run(tmp_path, "20261114-1000-a-aaaa")
    start(running, 1, T0)
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    watcher = Watcher(tmp_path, run_ids=[running.run_id], clock=lambda: clock[0], sleep=sleep)
    event = watcher.next(timeout=3, idle=True)
    assert (event["event"], event["timeout_seconds"], event["run"]) == ("timeout", 3, None)
    assert clock[0] >= 3
    assert event["cursor"] == watcher.cursor



def test_a_run_not_started_yet_is_new_however_recent(tmp_path):
    """No clock heuristic: a segment starts as soon as its command holds the run's lock,
    so until then a run (just created, or being resumed) is idle. `wait` reads only the
    log: a status saying running isn't a segment."""
    fresh = make_run(tmp_path, "20261114-1000-b-bbbb", created_at=time.time())
    assert Watcher(tmp_path, run_ids=[fresh.run_id]).next(idle=True)["runs"][0]["state"] == "new"
    hold(fresh)
    fresh.status.parent.mkdir(parents=True, exist_ok=True)
    fresh.status.write_text(json.dumps({"state": "running", "pid": os.getpid(), "host": HOST}))
    assert Watcher(tmp_path, run_ids=[fresh.run_id]).next(idle=True)["runs"][0]["state"] == "new"


def test_a_run_without_segments_is_new(tmp_path):
    """Every segment writes its start record beside its status: a run with history or a
    stopped status but no segments is a development build's, and reads as `new`; its
    history isn't read."""
    paths = make_run(tmp_path, "20261114-1000-a-aaaa")
    append(paths.history, {"iteration": 0, "accepted": True, "started_at": T0, "ended_at": T0 + 1})
    paths.status.parent.mkdir(parents=True, exist_ok=True)
    paths.status.write_text(json.dumps({"state": "finished", "pid": 1, "host": HOST}))
    watcher = Watcher(tmp_path, run_ids=[paths.run_id], after=EMPTY)
    assert watcher.idle()["runs"][0]["state"] == "new"


def test_next_blocks_until_an_event(tmp_path):
    paths = make_run(tmp_path, "20261114-1000-a-aaaa")
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        if len(calls) == 3:
            start(paths, 1, T0)
        if len(calls) == 5:
            record(paths, 0, T0 + 1)

    watcher = Watcher(tmp_path, sleep=sleep)
    assert watcher.next()["event"] == "started" and len(calls) == 3
    assert watcher.next()["event"] == "iteration" and len(calls) == 5


# --- the command ------------------------------------------------------------------------------


def cli(project, *args):
    return CliRunner().invoke(main, ["wait", *args, "-p", str(project)])


def test_wait_command_prints_one_event_line(tmp_path):
    paths = finished_run(tmp_path)
    result = cli(tmp_path, "--after", EMPTY)
    assert result.exit_code == 0, result.output
    [line] = result.output.splitlines()
    event = json.loads(line)
    assert (event["event"], event["short"]) == ("started", "aaaa")
    result = cli(tmp_path, "aaaa", "--after", event["cursor"])  # a short id
    assert json.loads(result.output)["event"] == "iteration"
    result = cli(tmp_path, paths.run_id)  # nothing pending from now, nothing running
    assert result.exit_code == 0 and json.loads(result.output)["event"] == "idle"


def test_wait_command_errors(tmp_path):
    finished_run(tmp_path)
    result = cli(tmp_path, "zzzz")
    assert result.exit_code == 1 and "No run 'zzzz'" in result.output
    result = cli(tmp_path, "--after", "garbage")
    assert result.exit_code == 2 and "not a cursor printed by `autohelix wait`" in result.output
    result = cli(tmp_path, "--follow", "--timeout", "1s")
    assert result.exit_code == 2 and "--timeout doesn't apply with --follow" in result.output
    result = cli(tmp_path, "--timeout", "soon")
    assert result.exit_code == 2 and "is not a duration" in result.output


def test_wait_command_times_out_with_exit_2(tmp_path):
    finished_run(tmp_path)
    started = time.monotonic()
    result = cli(tmp_path, "--timeout", "1s")
    assert result.exit_code == 2, result.output
    event = json.loads(result.output)
    assert (event["event"], event["timeout_seconds"]) == ("timeout", 1)
    assert time.monotonic() - started >= 1
    assert Watcher(tmp_path, after=event["cursor"]).poll() is None


def test_wait_follow_prints_every_event_and_exits_quietly_on_ctrl_c(tmp_path):
    finished_run(tmp_path)
    source = Path(__file__).resolve().parents[1] / "src"
    process = subprocess.Popen(
        [sys.executable, "-m", "autohelix.cli", "wait", "--follow", "--after", EMPTY, "-p", str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": str(source)})
    try:
        import queue

        lines_read = queue.Queue()
        threading.Thread(target=lambda: [lines_read.put(line) for line in process.stdout], daemon=True).start()
        lines = [json.loads(lines_read.get(timeout=30)) for _ in range(4)]
        assert [e["event"] for e in lines] == ["started", "iteration", "iteration", "ended"]
        process.send_signal(signal.SIGINT)
        _, stderr = process.communicate(timeout=30)
    finally:
        process.kill()
    assert process.returncode == 130
    assert "Traceback" not in stderr


# --- real runs ------------------------------------------------------------------------------------


def test_a_real_run_reports_its_whole_sequence(project):
    cursor = Watcher(project).cursor  # before the run
    result = CliRunner().invoke(main, ["run", "-n", "2", "--no-report", "-p", str(project)])
    assert result.exit_code == 0, result.output
    run_id = latest_run(project).run_id
    events = []
    while not events or events[-1]["event"] != "ended":
        result = cli(project, "--after", cursor, "--timeout", "1s")
        assert result.exit_code == 0, result.output
        events.append(json.loads(result.output))
        cursor = events[-1]["cursor"]
    assert Watcher(project, after=cursor).poll() is None
    assert [(e["event"], e.get("iteration")) for e in events] == [
        ("started", None), ("iteration", 0), ("iteration", 1), ("iteration", 2), ("ended", None)]
    assert all(e["run"] == run_id for e in events)
    assert [e["time"] for e in events] == sorted(e["time"] for e in events)
    started, baseline, *iterations, ended = events
    assert (started["segment"], started["start_iteration"], started["resumed"]) == (1, 0, False)
    assert baseline["reason"] == "baseline" and all(e["accepted"] and e["commit"] for e in iterations)
    assert iterations[0]["cost_usd"] == pytest.approx(0.1)
    assert (ended["state"], ended["stop_reason"]["kind"], ended["last_iteration"], ended["iterations"]) == (
        "finished", "budget", 2, 3)
    idle = json.loads(cli(project, short_id(run_id), "--after", cursor).output)
    assert idle["event"] == "idle" and idle["runs"][0]["state"] == "finished"


def test_a_failed_baseline_is_an_iteration_event(project):
    from tests.helpers import configure

    configure(project, gates=["echo formatted >> code.py"])  # a gate that edits files: the baseline fails
    cursor = Watcher(project).cursor
    result = CliRunner().invoke(main, ["run", "-n", "1", "--no-report", "-p", str(project)])
    assert result.exit_code == 1, result.output
    events = drain(Watcher(project, after=cursor))
    assert [(e["event"], e.get("iteration")) for e in events] == [("started", None), ("iteration", 0), ("ended", None)]
    assert not events[1]["accepted"] and events[1]["reason"].startswith("baseline failed: ")
    assert events[2]["state"] == "failed"


def test_wait_follows_a_live_run(project):
    from tests.helpers import configure

    configure(project, stop=["exit 1"])  # stop commands: accepted records are rewritten
    watcher = Watcher(project, poll_seconds=0.01)
    events, failures = [], []

    def follow():
        try:
            while not events or events[-1]["event"] != "ended":
                events.append(watcher.next(timeout=120))
                assert events[-1]["event"] != "timeout"
        except BaseException as error:  # noqa: BLE001 - reported by the test
            failures.append(error)

    thread = threading.Thread(target=follow, daemon=True)
    thread.start()
    result = CliRunner().invoke(main, ["run", "-n", "2", "--no-report", "-p", str(project)])
    assert result.exit_code == 0, result.output
    thread.join(60)
    assert not failures and not thread.is_alive()
    assert [(e["event"], e.get("iteration")) for e in events] == [
        ("started", None), ("iteration", 0), ("iteration", 1), ("iteration", 2), ("ended", None)]
    paths = latest_run(project)
    for event in events:
        if event["event"] == "iteration":  # reported in final form: after the stop commands
            final = json.loads(paths.record(event["iteration"]).read_text())
            assert event["accepted"]
            assert (event["time"], event["cost_usd"]) == (final["ended_at"], final["cost"]["cost_usd"])


def test_a_recovered_record_without_an_end_time_keeps_its_place(project):
    """A record `reconcile` recovers has no `ended_at`: its caught-up line gets the time
    it was logged, so neither it nor its open segment's crash reads as 1970."""
    from tests.helpers import configure

    configure(project, stop=["exit 1"])
    assert CliRunner().invoke(main, ["run", "-n", "1", "--no-report", "--land", "manual",
                                     "-p", str(project)]).exit_code == 0
    paths = latest_run(project)
    lines = paths.segments.read_text().splitlines()
    paths.segments.write_text("\n".join(line for line in lines
                                         if not ('"iteration": 1' in line or '"end"' in line)) + "\n")
    history = [json.loads(line) for line in paths.history.read_text().splitlines()]
    history[-1]["ended_at"] = None  # as recovered
    paths.history.write_text("".join(json.dumps(record) + "\n" for record in history))
    before = time.time()
    assert CliRunner().invoke(main, ["resume", "-n", "1", "--no-report", "-p", str(project)]).exit_code == 0
    caught_up = [json.loads(line) for line in paths.segments.read_text().splitlines()][2]
    assert (caught_up["segment"], caught_up["iteration"]) == (1, 1) and caught_up["ended_at"] >= before
    events = drain(Watcher(project, after=EMPTY))
    assert [e["event"] for e in events][:5] == ["started", "iteration", "iteration", "crashed", "started"]
    assert all(e["time"] > before - 3600 for e in events)


def test_an_iteration_killed_in_its_stop_commands_is_reported_at_the_resume(project):
    """Accepted and published, then the process died before its line (in the stop
    commands): the next segment logs it for the dead one, before its start."""
    from tests.helpers import configure

    configure(project, stop=["exit 1"])
    assert CliRunner().invoke(main, ["run", "-n", "1", "--no-report", "--land", "manual",
                                     "-p", str(project)]).exit_code == 0
    paths = latest_run(project)
    lines = paths.segments.read_text().splitlines()
    killed = [line for line in lines if not ('"iteration": 1' in line or '"end"' in line)]
    paths.segments.write_text("\n".join(killed) + "\n")
    result = CliRunner().invoke(main, ["resume", "-n", "1", "--no-report", "-p", str(project)])
    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in paths.segments.read_text().splitlines()]
    assert [(r["segment"], r["event"], r.get("iteration")) for r in records] == [
        (1, "start", None), (1, "iteration", 0), (1, "iteration", 1), (2, "start", None), (2, "iteration", 2),
        (2, "end", None)]
    assert records[2]["accepted"] and records[2]["commit"]
    assert brief(drain(Watcher(project, after=EMPTY))) == [
        ("started", short_id(paths.run_id), 1), ("iteration", short_id(paths.run_id), 0),
        ("iteration", short_id(paths.run_id), 1), ("crashed", short_id(paths.run_id), 1),
        ("started", short_id(paths.run_id), 2), ("iteration", short_id(paths.run_id), 2),
        ("ended", short_id(paths.run_id), 2)]
