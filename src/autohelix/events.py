# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run events for `autohelix wait`: what happened in a project's runs, one JSON object
per event, read from the files the harness writes (a pure reader: nothing is written,
no run lock is taken, nothing is repaired).

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
- `iteration`: an iteration was recorded (the baseline is iteration 0). `iteration`,
  `accepted`, `reason` (as recorded: `baseline`, `gate failed: pytest -q`, ...; null
  for most accepted ones), `summary` (the agent's one line), `metrics`, `commit`
  (null when rejected or an accepted no-op), `cost_usd` (the known cost; null when
  none is known), `cost_incomplete` (some of its calls' costs are unknown),
  `agent_error` (why the agent call failed, else null). `time`: when it ended. An
  accepted iteration is reported once its record is final (after its stop commands).
- `ended`: a segment ended. `segment`, `state` (`finished`, `interrupted`, `failed`),
  `stop_reason` (why it stopped, as in `view --json`: an object with `kind` and
  `detail`, or null), `error` (a failed segment's error, else null),
  `last_iteration` (the run's last iteration), `iterations` (records this segment
  added). `time`: when it ended.
- `crashed`: a segment never ended and its process is gone (same host only: a
  process on another host can't be checked). `segment`, `last_iteration`. Reported
  once per segment. `time`: the last sign of life (its status's last update, or the
  run's last event before it).
- `idle`: only for `wait` with run ids, without `--follow`: none of them is running
  and nothing is pending. `runs`: `[{run, short, state}]`, state `finished`,
  `interrupted`, `failed`, `crashed` or `new` (never started a segment). A run
  created in the last minute that has not started its first segment yet (preflight
  checks) counts as starting, not idle; so does nothing else: a run being resumed is
  idle until its new segment starts.
- `timeout`: `--timeout` passed without an event. `timeout_seconds`.

Order and the cursor
--------------------
Events of a run come in the order they happened: a segment's `started`, its
iterations, then its `ended` (or `crashed`). Among pending events (of several runs
too) the oldest `time` comes first; ties go started < iteration < ended < crashed,
then by run id. A record can land after its time (the harness timestamps first), so
an event of one run may come after a newer one of another run.

The cursor records, per run, what has been reported: the last iteration record (its
number and start time: a retried baseline is a new record 0), the last segment
reported started, ended and crashed. It never relies on comparing times, which the
harness takes before writing (a segment's start time precedes its record by its
preflight). A cursor is `1.` + base64url (unpadded) of zlib-compressed compact JSON
`{"r": {run_id: [iteration, started_ms, started, ended, crashed]}}` (runs with
nothing reported are left out; the format may change with its version prefix). A
fresh `wait` (no `--after`) starts from every run's current state, all of the
project's runs whichever are watched, so `--after` with other run ids or none reports
only what happened since. A run missing from a cursor is new: everything it has is
pending.

Only complete, newline-terminated, parseable lines of history.jsonl and
segments.jsonl count; others are skipped (a torn line counts once it is complete).
A record rewritten in place (e.g. an accepted one updated after its stop commands)
keeps its number and start time and is not reported again.
"""

import base64
import json
import os
import socket
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from autohelix.costs import summarize_costs
from autohelix.runs import _ID, RunPaths, short_id

EVENT_SCHEMA = 1
CURSOR_VERSION = "1"
POLL_SECONDS = 0.5
# A run without a segment yet, created this recently, is starting (preflight), not idle.
STARTING_SECONDS = 60.0
# The phases in which an accepted iteration's record may still be rewritten (stop commands).
PROVISIONAL_PHASES = ("accept", "stop check")
_RANK = {"started": 0, "iteration": 1, "ended": 2, "crashed": 3}
_HOST = socket.gethostname()


@dataclass
class Marks:
    """What a cursor says was reported for one run."""

    iteration: int = -1  # the last reported record's number (-1: none)
    started_ms: int = -1  # and its started_at, in ms (-1: none recorded)
    started: int = 0  # the last segment reported started (segments count from 1)
    ended: int = 0  # ... ended
    crashed: int = 0  # ... crashed

    def as_list(self) -> list[int]:
        return [self.iteration, self.started_ms, self.started, self.ended, self.crashed]

    def empty(self) -> bool:
        return self == Marks()


def encode_cursor(marks: dict[str, Marks]) -> str:
    data = {"r": {run: m.as_list() for run, m in sorted(marks.items()) if not m.empty()}}
    packed = zlib.compress(json.dumps(data, separators=(",", ":")).encode(), 9)
    return f"{CURSOR_VERSION}." + base64.urlsafe_b64encode(packed).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Marks]:
    """The marks a cursor holds; ValueError if it isn't one."""
    version, _, body = cursor.strip().partition(".")
    if version != CURSOR_VERSION or not body:
        raise ValueError("not a cursor printed by `autohelix wait`")
    try:
        packed = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        inflate = zlib.decompressobj()
        raw = inflate.decompress(packed, 1 << 20)
        if inflate.unconsumed_tail or not inflate.eof:
            raise ValueError("truncated or too large")
        data = json.loads(raw)
    except (ValueError, zlib.error) as error:  # binascii.Error and UnicodeDecodeError are ValueErrors
        raise ValueError("not a cursor printed by `autohelix wait`") from error
    runs = data.get("r") if isinstance(data, dict) else None
    if not isinstance(runs, dict):
        raise ValueError("not a cursor printed by `autohelix wait`")
    marks = {}
    for run, values in runs.items():
        if (not isinstance(run, str) or not _ID.fullmatch(run) or not isinstance(values, list) or len(values) != 5
                or any(type(v) is not int for v in values)):
            raise ValueError("not a cursor printed by `autohelix wait`")
        marks[run] = Marks(*values)
    return marks


def _ms(value) -> int:
    return int(round(value * 1000)) if _number(value) else -1


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _complete_lines(path: Path) -> list[dict]:
    """The file's newline-terminated lines that parse as JSON objects."""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    objects = []
    for line in data.split(b"\n")[:-1]:  # the last piece is unterminated (or empty)
        try:
            value = json.loads(line)
        except ValueError:  # UnicodeDecodeError included
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def process_gone(pid, host) -> bool:
    """Whether `pid` on `host` has certainly exited (only this host's can be checked)."""
    if host != _HOST or type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:  # PermissionError: someone else's live process
        return False
    return False


def _signature(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_ino, st.st_size, st.st_mtime_ns


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
    """One run's files, reparsed only when their stat changes."""

    def __init__(self, paths: RunPaths):
        self.paths = paths
        self.run = paths.run_id
        self._seen: dict[str, object] = {}
        self.records: list[dict] = []
        self.segments: list[dict] = []
        self.status: dict = {}
        self.created_at: float | None = None
        self._identity_read = False

    def _changed(self, name: str, path: Path) -> bool:
        signature = _signature(path)
        if name in self._seen and self._seen[name] == signature:
            return False
        self._seen[name] = signature
        return True

    def refresh(self, force_segments: bool = False) -> None:
        paths = self.paths
        if self._changed("history", paths.history):
            records = []
            for record in _complete_lines(paths.history):
                n = record.get("iteration")
                if type(n) is int and n >= 0 and type(record.get("accepted")) is bool:
                    records.append(record)
            self.records = records
        if self._changed("segments", paths.segments) or force_segments:
            self.segments = _segments(_complete_lines(paths.segments))
        if self._changed("status", paths.status):
            try:
                status = json.loads(paths.status.read_text())
            except (OSError, ValueError):
                status = {}
            self.status = status if isinstance(status, dict) else {}
        if not self._identity_read:  # until run.json is there (a run being created)
            try:
                created = json.loads(paths.identity.read_text()).get("created_at")
                self._identity_read = True
            except (OSError, ValueError, AttributeError):
                created = None
            if _number(created):
                self.created_at = created

    # --- what the files say -------------------------------------------------------

    def _status_alive(self) -> bool:
        s = self.status
        return s.get("state") == "running" and not process_gone(s.get("pid"), s.get("host"))

    def _records(self) -> list[tuple[tuple[int, int], dict]]:
        """Settled records by (number, started_ms), each key once (the last line with it):
        an accepted record the running harness may still rewrite is left out."""
        by_key = {}
        for record in self.records:
            by_key[(record["iteration"], _ms(record.get("started_at")))] = record
        keyed = sorted(by_key.items(), key=lambda item: item[0])
        if keyed:
            (n, _), last = keyed[-1]
            s = self.status
            if (n > 0 and last["accepted"] and s.get("iteration") == n and s.get("phase") in PROVISIONAL_PHASES
                    and self._status_alive()):
                keyed.pop()
        return keyed

    def _record_time(self, record: dict) -> float:
        if _number(record.get("ended_at")):
            return float(record["ended_at"])
        for path in (self.paths.record(record["iteration"]), self.paths.history):
            try:
                return path.stat().st_mtime
            except OSError:
                pass
        return 0.0

    def _dead(self, segment: dict) -> bool:
        """An open segment whose process is gone: a later segment started (under the run
        lock), or its process on this host has exited."""
        if not segment["open"]:
            return False
        if segment["segment"] != self.segments[-1]["segment"]:
            return True
        return process_gone(segment.get("pid"), segment.get("host"))

    def _crashed(self) -> list[dict]:
        """The open segments whose process is gone, confirmed by a reread of the segment
        log after the check (a process writes its end record before it exits)."""
        if not any(self._dead(s) for s in self.segments):
            return []
        dead = [s["segment"] for s in self.segments if self._dead(s)]
        self.refresh(force_segments=True)
        return [s for s in self.segments if s["segment"] in dead and s["open"]]

    def _crash_time(self, segment: dict) -> float:
        start = segment["started_at"] if _number(segment.get("started_at")) else 0.0
        later = [s["started_at"] for s in self.segments
                 if s["segment"] > segment["segment"] and _number(s.get("started_at"))]
        until = min(later, default=float("inf"))
        times = [start] + [t for t in (self._record_time(r) for _, r in self._records()) if start <= t < until]
        if not later and self.status.get("pid") == segment.get("pid"):
            times += [self.status[k] for k in ("updated_at", "stopped_at") if _number(self.status.get(k))]
        return max(times)

    def last_iteration(self) -> int | None:
        return max((r["iteration"] for r in self.records), default=None)

    def state(self, now: float) -> str:
        """`running`, `starting`, or how it stands idle: the last segment's end state,
        `crashed`, or `new`."""
        if not self.segments:
            created = self.created_at
            if created is None:
                signature = _signature(self.paths.run_dir)
                created = signature[2] / 1e9 if signature else None
            return "starting" if created is not None and now - created < STARTING_SECONDS else "new"
        last = self.segments[-1]
        if last["open"]:
            return "crashed" if self._dead(last) else "running"
        return last.get("state") if isinstance(last.get("state"), str) else "ended"

    # --- events -------------------------------------------------------------------

    def pending(self, marks: Marks) -> list[_Event]:
        """The next pending event of each kind after `marks` (at most one each, the
        lowest: marks advance one event at a time, so nothing is skipped)."""
        events = []
        run = self.run
        # First: confirming a crash rereads the segment log, which the rest then uses.
        crashed = self._crashed() if any(s["open"] and s["segment"] > marks.crashed for s in self.segments) else []
        for (n, ms), record in self._records():
            if n > marks.iteration or (n == marks.iteration and ms > marks.started_ms):
                cost = record.get("cost") if isinstance(record.get("cost"), dict) else {}
                if not cost:
                    calls = record.get("calls") if isinstance(record.get("calls"), list) else []
                    summary = summarize_costs([c.get("usage") or {} for c in calls if isinstance(c, dict)])
                    cost = {"cost_usd": summary.cost_usd, "incomplete": summary.incomplete}
                events.append(_Event(self._record_time(record), "iteration", run, {
                    "iteration": n, "accepted": record["accepted"], "reason": record.get("reason"),
                    "summary": record.get("summary"), "metrics": record.get("metrics") or {},
                    "commit": record.get("commit"), "cost_usd": cost.get("cost_usd"),
                    "cost_incomplete": bool(cost.get("incomplete")), "agent_error": record.get("agent_error"),
                }, _replace(marks, iteration=n, started_ms=ms)))
                break
        for segment in self.segments:
            n = segment["segment"]
            if n > marks.started:
                events.append(_Event(_time(segment.get("started_at")), "started", run, {
                    "segment": n, "start_iteration": segment.get("start_iteration"), "resumed": n > 1,
                }, _replace(marks, started=n)))
                break
        for segment in self.segments:
            n = segment["segment"]
            if n > marks.ended and not segment["open"]:
                events.append(_Event(_time(segment.get("ended_at")), "ended", run, {
                    "segment": n, "state": segment.get("state"), "stop_reason": segment.get("stop_reason"),
                    "error": segment.get("error"), "last_iteration": segment.get("last_iteration"),
                    "iterations": segment.get("iterations"),
                }, _replace(marks, ended=n)))
                break
        for segment in crashed:
            if segment["segment"] > marks.crashed:
                events.append(_Event(self._crash_time(segment), "crashed", run, {
                    "segment": segment["segment"], "last_iteration": self.last_iteration(),
                }, _replace(marks, crashed=segment["segment"])))
                break
        return events

    def current(self) -> Marks:
        """Marks past everything reportable now (a fresh `wait`'s starting point)."""
        records = self._records()
        n, ms = records[-1][0] if records else (-1, -1)
        numbers = [s["segment"] for s in self.segments]
        return Marks(iteration=n, started_ms=ms, started=max(numbers, default=0),
                     ended=max((s["segment"] for s in self.segments if not s["open"]), default=0),
                     crashed=max((s["segment"] for s in self._crashed()), default=0))


def _replace(marks: Marks, **changes) -> Marks:
    return Marks(**{**marks.__dict__, **changes})


def _time(value) -> float:
    return float(value) if _number(value) else 0.0


def _segments(records: list[dict]) -> list[dict]:
    """Start records merged with their end records (`run_status.read_segments`)."""
    segments: dict[int, dict] = {}
    for record in records:
        n = record.get("segment")
        if type(n) is not int:
            continue
        fields = {k: v for k, v in record.items() if k != "event"}
        if record.get("event") == "start":
            segments[n] = {"ended_at": None, "state": None, "stop_reason": None, "last_iteration": None,
                           "iterations": None, "error": None, **fields, "open": True}
        elif record.get("event") == "end" and n in segments:
            segments[n].update(fields, open=False)
    return [segments[n] for n in sorted(segments)]


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
        self.run_ids = list(self.run_ids) if self.run_ids else None
        if self.after is not None:
            self.marks = decode_cursor(self.after)
        else:
            self.marks = {}
            for source in self._scan(watched_only=False):
                self.marks[source.run] = source.current()
        self._prune()

    @property
    def cursor(self) -> str:
        return encode_cursor(self.marks)

    def _present(self) -> list[str]:
        try:
            names = os.listdir(RunPaths(self.project).runs)
        except OSError:
            return []
        return sorted(n for n in names if _ID.fullmatch(n) and (RunPaths(self.project).runs / n).is_dir())

    def _scan(self, watched_only: bool = True) -> list[RunSource]:
        present = self._present()
        if watched_only and self.run_ids is not None:
            present = [run for run in self.run_ids if run in present]
        for run in present:
            if run not in self._sources:
                self._sources[run] = RunSource(RunPaths(self.project, run))
        sources = [self._sources[run] for run in present]
        for source in sources:
            source.refresh()
        return sources

    def _prune(self) -> None:
        """Drop deleted runs' marks (a cursor names only runs that exist)."""
        present = set(self._present())
        self.marks = {run: m for run, m in self.marks.items() if run in present}

    def _event(self, kind: str, run: str | None, at: float, fields: dict) -> dict:
        return {"schema": EVENT_SCHEMA, "event": kind, "run": run, "short": short_id(run) if run else None,
                "time": at, **fields, "cursor": self.cursor}

    def poll(self) -> dict | None:
        """One look: the oldest pending event (the cursor advanced past it), else None."""
        pending = []
        for source in self._scan():
            pending += source.pending(self.marks.get(source.run, Marks()))
        if not pending:
            return None
        event = min(pending, key=_Event.key)
        self.marks[event.run] = event.marks
        self._prune()
        return self._event(event.kind, event.run, event.time, event.fields)

    def idle(self) -> dict | None:
        """With run ids: the `idle` event if none of them is running or starting (call
        after a `poll` that found nothing)."""
        if self.run_ids is None:
            return None
        now = self.now()
        runs = []
        for run in self.run_ids:
            source = self._sources.get(run)
            state = source.state(now) if source is not None else "gone"
            if state in ("running", "starting"):
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
