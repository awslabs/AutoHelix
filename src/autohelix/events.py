# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run events for `autohelix wait`: what happened in a project's runs, one JSON object
per event, read from each run's event log, runs/<id>/.internal/segments.jsonl (a
pure reader: nothing is written, nothing is repaired; the run lock is only probed, for
a segment open on this host, to tell whether its process is gone).

The log (`run_status.SegmentLog`) is append-only: the harness writes a line when a
segment starts, when an iteration's record is final (after its stop commands; the
baseline's too, a failed one included) and when the segment ends. `wait` reports
those lines in order, plus a `crashed` event for a segment that never ended.

Events
------
Every event is one JSON object (one line). The schema is a public contract like
`view --json`: versioned by `schema` (`EVENT_SCHEMA`), fields are only ever added.
Common fields:

- `schema`: `EVENT_SCHEMA` (1).
- `event`: `started`, `iteration`, `ended`, `crashed`, `idle` or `timeout`.
- `run`, `short`: the run's full id and its last part (null for `idle`, `timeout`).
- `time`: when it happened (epoch seconds; for `idle` and `timeout`, when reported).
- `cursor`: an opaque string; `wait --after CURSOR` continues right after this event.

Per event:

- `started`: a segment (one `run` or `resume` command) started; a new run's first
  segment or a resume. `segment` (1, 2, ... in the run), `start_iteration` (0 when it
  begins with the baseline), `resumed` (segment > 1). `time`: the segment's start.
- `iteration`: an iteration was recorded (the baseline is iteration 0; a retried
  baseline is another event for 0). `iteration`, `accepted`, `reason` (as recorded:
  `baseline`, `gate failed: pytest -q`, ...; null for most accepted ones), `summary`
  (the agent's one line), `metrics`, `commit` (null when rejected or an accepted
  no-op), `cost_usd` (the known cost; null when none is known), `cost_incomplete`
  (some of its calls' costs are unknown), `agent_error` (why the agent call failed,
  else null). `time`: when it ended. Reported once its record is final (after its
  stop commands).
- `ended`: a segment ended. `segment`, `state` (`finished`, `interrupted`, `failed`),
  `stop_reason` (why it stopped, as in `view --json`: an object with `kind` and
  `detail`, or null), `error` (a failed segment's error, else null),
  `last_iteration` (the run's last iteration), `iterations` (records this segment
  added). `time`: when it ended.
- `crashed`: a segment never ended and its process is gone (nothing holds the run's
  lock, or a later segment started). `segment`, `last_iteration` (the last iteration
  reported before it crashed; null if none was). Reported once per segment. `time`:
  its last event (its start or its last iteration). An iteration whose process died
  between its record and its event line is in the history but not reported. Only a
  run on this host can be checked: one that crashed on another host looks running
  until it is resumed; only `--timeout` ends a wait on it.
- `idle`: only for `wait` with run ids, without `--follow`: none of them is running
  and nothing is pending. `runs`: `[{run, short, state}]`, state `finished`,
  `interrupted`, `failed` (how its last segment ended), `crashed`, `new` (nothing
  started yet) or `gone` (its folder was deleted while waiting). A segment starts
  as soon as its `run` or `resume` holds the run's lock, before its checks; until
  then (the command's first second or so) the run is idle: wait for the `started`
  event of a run you launch (`wait` without ids, or `--after` a cursor taken before).
- `timeout`: `--timeout` passed without an event. `timeout_seconds`.

Order and the cursor
--------------------
Events of a run come in the order of its log (a crash just before the next segment's
start, or after everything when its process is gone). Among the runs' next events the
oldest `time` comes first; ties go started < iteration < ended < crashed, then by run
id. A line can land after its time (the harness timestamps first), so an event of one
run may come after a newer one of another run.

The cursor records, per run, how many lines of its log were reported and the last
segment reported crashed. A cursor is `2.` + base64url (unpadded) of zlib-compressed
compact JSON `{"r": {run_id: [lines, crashed]}}` (runs with nothing reported left
out; the format may change with its version prefix). A fresh `wait` (no `--after`)
starts from every run's current state, all of the project's runs whichever are
watched, so `--after` with other run ids or none reports only what happened since. A
run missing from a cursor is new: everything it has is pending.

Only complete, newline-terminated lines count; one that isn't an event is skipped (a
torn line counts once it is complete). The log is opened without following symlinks,
only as a regular file, and up to `LOG_CAP` bytes; one that fails these checks reads
as empty.
"""

import base64
import bisect
import json
import os
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from autohelix.run_status import holder_gone
from autohelix.runs import _ID, RunPaths, short_id
from autohelix.safe_open import open_file

EVENT_SCHEMA = 1
CURSOR_VERSION = "2"
CURSOR_CAP = 64 << 20  # bytes of cursor JSON, both ways: never print one `--after` refuses
POLL_SECONDS = 0.5
LOG_CAP = 1 << 30  # bytes of one run's segments.jsonl
_RANK = {"started": 0, "iteration": 1, "ended": 2, "crashed": 3}
_NOT_A_CURSOR = "not a cursor printed by `autohelix wait`"
# An iteration event's fields, as the harness writes them into the log (`iteration_fields`).
ITERATION_FIELDS = ("iteration", "accepted", "reason", "summary", "metrics", "commit", "cost_usd",
                    "cost_incomplete", "agent_error")
_ENDED_FIELDS = ("state", "stop_reason", "error", "last_iteration", "iterations")


def iteration_fields(result) -> dict:
    """The `iteration` event's fields of a final record (`history.IterationResult`)."""
    cost = result.cost
    return {"iteration": result.iteration, "accepted": result.accepted, "reason": result.reason,
            "summary": result.summary, "metrics": dict(result.metrics or {}), "commit": result.commit,
            "cost_usd": cost.cost_usd, "cost_incomplete": bool(cost.incomplete), "agent_error": result.agent_error}


@dataclass(frozen=True, order=True)
class Marks:
    """What a cursor says was reported for one run (compared in this order)."""

    lines: int = 0  # lines of the log reported
    crashed: int = 0  # the last segment reported crashed (segments count from 1)


def encode_cursor(marks: dict[str, Marks]) -> str:
    data = {"r": {run: [m.lines, m.crashed] for run, m in sorted(marks.items()) if m != Marks()}}
    raw = json.dumps(data, separators=(",", ":")).encode()
    if len(raw) > CURSOR_CAP:
        raise RuntimeError(f"Too many runs for a cursor ({len(data['r'])})")
    return f"{CURSOR_VERSION}." + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Marks]:
    """The marks a cursor holds; ValueError if it isn't one."""
    version, _, body = cursor.strip().partition(".")
    if version != CURSOR_VERSION or not body:
        raise ValueError(_NOT_A_CURSOR)
    try:
        packed = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        inflate = zlib.decompressobj()
        raw = inflate.decompress(packed, CURSOR_CAP)
        if inflate.unconsumed_tail or not inflate.eof:
            raise ValueError("truncated or too large")
        data = json.loads(raw)
    except (ValueError, zlib.error) as error:  # binascii.Error and UnicodeDecodeError are ValueErrors
        raise ValueError(_NOT_A_CURSOR) from error
    runs = data.get("r") if isinstance(data, dict) else None
    if not isinstance(runs, dict):
        raise ValueError(_NOT_A_CURSOR)
    marks = {}
    for run, values in runs.items():
        if (not isinstance(run, str) or not _ID.fullmatch(run) or not isinstance(values, list)
                or len(values) != 2 or any(type(v) is not int or v < 0 for v in values)):
            raise ValueError(_NOT_A_CURSOR)
        marks[run] = Marks(*values)
    return marks


def _time(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _line(raw: bytes) -> dict | None:
    """A log line's record: an object with an int `segment` and a known `event`."""
    try:
        record = json.loads(raw)
    except ValueError:  # UnicodeDecodeError included
        return None
    if (isinstance(record, dict) and type(record.get("segment")) is int
            and record.get("event") in ("start", "iteration", "end")):
        return record
    return None


@dataclass
class _Event:
    time: float
    kind: str
    run: str
    fields: dict
    marks: Marks  # the run's marks once this event is reported

    def key(self):
        return self.time, _RANK[self.kind], self.run


class RunSource:
    """One run's event log, read on from where the last read ended (it only grows)."""

    def __init__(self, runs: Path, run_id: str):
        self.runs = runs  # the project's runs folder: the log is opened below it
        self.run = run_id
        self.paths = RunPaths(runs.parent.parent, run_id)
        self.lines: list[dict | None] = []  # each complete line's record (None: not one)
        self._file: tuple | None = None  # (device, inode) of the file read
        self._offset = 0  # bytes of complete lines read
        self.dead: int | None = None  # the last segment, open, its process seen gone
        self.events: list[_Event] = []
        self._marks: list[Marks] = []  # each event's marks (sorted)

    def _read(self) -> bool:
        """Read the lines appended since the last read; True if any were. A replaced or
        shrunk file (not the harness's doing) is read again from the start."""
        fd = open_file(self.runs / self.run, [".internal", "segments.jsonl"])
        if fd is None:
            changed = bool(self.lines)
            self.lines, self._file, self._offset = [], None, 0
            return changed
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            changed = False
            if (info.st_dev, info.st_ino) != self._file or info.st_size < self._offset or info.st_size > LOG_CAP:
                changed = bool(self.lines)
                self.lines, self._file, self._offset = [], (info.st_dev, info.st_ino), 0
                if info.st_size > LOG_CAP:
                    return changed
            if info.st_size == self._offset:
                return changed
            stream.seek(self._offset)
            data = stream.read(LOG_CAP - self._offset)
        complete = data[:data.rfind(b"\n") + 1]  # an unterminated last line may still be being written
        if not complete:
            return changed
        self._offset += len(complete)
        self.lines += [_line(raw) for raw in complete.split(b"\n")[:-1]]
        return True

    def _last_start(self) -> dict | None:
        """The last segment's start record, if it has no end record (open)."""
        for record in reversed(self.lines):
            if record is not None and record["event"] in ("start", "end"):
                return record if record["event"] == "start" else None
        return None

    def refresh(self) -> None:
        """Read what was appended; then confirm a crash: an open last segment whose
        process is gone is dead only if a reread of the log (after its process was seen
        gone: an ending process writes its end record first) still shows it open. Once
        dead it stays so (not probed again) until the log grows."""
        changed = self._read()
        last = self._last_start()
        if last is None or (self.dead is not None and self.dead != last["segment"]):
            changed |= self.dead is not None
            self.dead = None
        if last is not None and self.dead is None and holder_gone(self.paths, last.get("host"), last.get("pid")):
            changed |= self._read()
            if self._last_start() is last:
                self.dead, changed = last["segment"], True
        if changed:
            self.events = self._derive()
            self._marks = [event.marks for event in self.events]

    def _derive(self) -> list[_Event]:
        """Every event the log holds, in order, each with the marks past it."""
        events, run = [], self.run
        crashed, last_iteration = 0, None
        lives: dict[int, float] = {}  # open segments: their last sign of life

        def crash(segment: int, lines: int) -> None:
            nonlocal crashed
            crashed = max(crashed, segment)
            events.append(_Event(lives.pop(segment), "crashed", run,
                                 {"segment": segment, "last_iteration": last_iteration}, Marks(lines, crashed)))

        for i, record in enumerate(self.lines):
            if record is None:
                continue
            n, kind = record["segment"], record["event"]
            if kind == "start":
                for open_segment in sorted(lives):  # never ended, and a later one started
                    crash(open_segment, i)
                lives[n] = _time(record.get("started_at"))
                events.append(_Event(lives[n], "started", run, {
                    "segment": n, "start_iteration": record.get("start_iteration"), "resumed": n > 1,
                }, Marks(i + 1, crashed)))
            elif kind == "iteration":
                at = _time(record.get("ended_at"))
                if n in lives:
                    lives[n] = max(lives[n], at)
                if type(record.get("iteration")) is int:
                    last_iteration = record["iteration"]
                events.append(_Event(at, "iteration", run, {k: record.get(k) for k in ITERATION_FIELDS},
                                     Marks(i + 1, crashed)))
            else:
                lives.pop(n, None)
                events.append(_Event(_time(record.get("ended_at")), "ended", run, {
                    "segment": n, **{k: record.get(k) for k in _ENDED_FIELDS}}, Marks(i + 1, crashed)))
        if self.dead is not None and self.dead in lives:
            crash(self.dead, len(self.lines))
        return events

    def state(self) -> str:
        """`running`, or how it stands idle (see the module docstring)."""
        for record in reversed(self.lines):
            if record is not None and record["event"] == "start":
                return "crashed" if record["segment"] == self.dead else "running"
            if record is not None and record["event"] == "end":
                return record.get("state") if isinstance(record.get("state"), str) else "failed"
        return "new"

    def pending(self, marks: Marks) -> _Event | None:
        """The first event past `marks`."""
        i = bisect.bisect_right(self._marks, marks)
        return self.events[i] if i < len(self.events) else None

    def current(self) -> Marks:
        """Marks past everything reportable now (a fresh `wait`'s starting point)."""
        return Marks(len(self.lines), self.events[-1].marks.crashed if self.events else 0)


@dataclass
class Watcher:
    """Watches a project's runs (`run_ids`, else all of them, new ones included) from a
    cursor (`after`; None: from now). `next()` returns the next event as a dict and
    advances the cursor past it."""

    project: Path
    run_ids: list[str] | None = None
    after: str | None = None
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time
    poll_seconds: float = POLL_SECONDS
    marks: dict[str, Marks] = field(init=False)
    _sources: dict[str, RunSource] = field(init=False, default_factory=dict)

    def __post_init__(self):
        self.project = Path(self.project).resolve()
        self.runs = RunPaths(self.project).runs
        self.run_ids = list(self.run_ids) if self.run_ids else None
        present = self._present()
        if self.after is not None:
            self.marks = decode_cursor(self.after)
        else:
            self.marks = {source.run: source.current() for source in self._scan(present, watched_only=False)}
        self._prune(present)

    @property
    def cursor(self) -> str:
        return encode_cursor(self.marks)

    def _present(self) -> set[str]:
        try:
            with os.scandir(self.runs) as entries:
                return {e.name for e in entries if _ID.fullmatch(e.name) and e.is_dir(follow_symlinks=False)}
        except OSError:
            return set()

    def _scan(self, present: set[str], watched_only: bool = True) -> list[RunSource]:
        runs = sorted(present) if not watched_only or self.run_ids is None else [
            run for run in self.run_ids if run in present]
        for run in runs:
            if run not in self._sources:
                self._sources[run] = RunSource(self.runs, run)
        sources = [self._sources[run] for run in runs]
        for source in sources:
            source.refresh()
        return sources

    def _prune(self, present: set[str]) -> None:
        """Drop deleted runs (a cursor names only runs that exist)."""
        self.marks = {run: m for run, m in self.marks.items() if run in present}
        for run in [run for run in self._sources if run not in present]:
            del self._sources[run]

    def _event(self, kind: str, run: str | None, at: float, fields: dict) -> dict:
        return {"schema": EVENT_SCHEMA, "event": kind, "run": run, "short": short_id(run) if run else None,
                "time": at, **fields, "cursor": self.cursor}

    def poll(self) -> dict | None:
        """One look: the oldest pending event (the cursor advanced past it), else None."""
        present = self._present()
        pending = [event for source in self._scan(present)
                   if (event := source.pending(self.marks.get(source.run, Marks()))) is not None]
        self._prune(present)
        if not pending:
            return None
        event = min(pending, key=_Event.key)
        self.marks[event.run] = event.marks
        return self._event(event.kind, event.run, event.time, dict(event.fields))

    def idle(self) -> dict | None:
        """With run ids: one more look, its event if it found one; else the `idle` event
        if none of the runs is running (states from that same look)."""
        if self.run_ids is None:
            return None
        if (event := self.poll()) is not None:
            return event
        now = self.now()
        runs = []
        for run in self.run_ids:
            source = self._sources.get(run)
            state = source.state() if source is not None else "gone"
            if state == "running":
                return None
            runs.append({"run": run, "short": short_id(run), "state": state})
        return self._event("idle", None, now, {"runs": runs})

    def next(self, timeout: float | None = None, idle: bool = False) -> dict:
        """The next event: blocks until one is pending (or, with `idle`, the watched runs
        are idle; or `timeout` seconds pass: the `timeout` event)."""
        deadline = None if timeout is None else self.clock() + timeout
        while True:
            if (event := self.poll()) is not None:
                return event
            if idle and (event := self.idle()) is not None:
                return event
            if deadline is not None:
                left = deadline - self.clock()
                if left <= 0:
                    return self._event("timeout", None, self.now(), {"timeout_seconds": timeout})
                self.sleep(min(self.poll_seconds, left))
            else:
                self.sleep(self.poll_seconds)
