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
- `crashed`: a segment never ended and its process is gone. `segment`,
  `last_iteration` (the run's last iteration when it crashed; null if
  it had none). Reported once per segment. `time`: the last sign of life (its status's last update, or the run's
  last event before it). Only a process on this host can be checked: a run that
  crashed on another host, or whose process id was reused, looks running until it
  is resumed; only `--timeout` ends a wait on it.
- `idle`: only for `wait` with run ids, without `--follow`: none of them is running
  and nothing is pending. `runs`: `[{run, short, state}]`, state `finished`,
  `interrupted`, `failed` (how its last segment ended), `crashed`, `ended` (a run
  from before the segment log, with history but no segments), `new` (nothing
  started yet) or `gone` (its folder was deleted while waiting). A run created in
  the last minute that has not started its first segment yet (preflight checks)
  counts as starting, not idle; so does nothing else: a run being resumed is idle
  until its new segment starts.
- `timeout`: `--timeout` passed without an event. `timeout_seconds`.

Order and the cursor
--------------------
Events of a run come in the order they happened: a segment's `started`, its
iterations, then its `ended` (or `crashed`). Among pending events (of several runs
too) the oldest `time` comes first; ties go started < iteration < ended < crashed,
then by run id. A record can land after its time (the harness timestamps first), so
an event of one run may come after a newer one of another run.

The cursor records, per run, what has been reported: the last iteration record (its
number, and a fingerprint of its start time: a retried baseline is a new record 0
that replaces the old one), the last segment reported started, ended and crashed.
It never relies on comparing times, which the harness takes before writing (a
segment's start time precedes its record by its preflight). A cursor is `1.` +
base64url (unpadded) of zlib-compressed compact JSON `{"r": {run_id: [iteration,
fingerprint, started, ended, crashed]}}` (trailing defaults dropped, runs with
nothing reported left out; the format may change with its version prefix). A fresh
`wait` (no `--after`) starts from every run's current state, all of the project's
runs whichever are watched, so `--after` with other run ids or none reports only
what happened since. A run missing from a cursor is new: everything it has is
pending.

Only complete, newline-terminated, parseable lines of history.jsonl and
segments.jsonl count; others are skipped (a torn line counts once it is complete).
A record rewritten in place (e.g. an accepted one updated after its stop commands)
keeps its number and start time and is not reported again. The run's files are
opened without following symlinks, only as regular files, and up to a size cap
(`CAPS`); one that fails these checks reads as missing.
"""

import base64
import json
import os
import socket
import stat
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from autohelix.costs import summarize_costs
from autohelix.run_status import read_segments
from autohelix.runs import _ID, RunPaths, short_id
from autohelix.safe_open import open_file

EVENT_SCHEMA = 1
CURSOR_VERSION = "1"
CURSOR_CAP = 64 << 20  # bytes of cursor JSON, both ways: never print one `--after` refuses
POLL_SECONDS = 0.5
# A run without a segment yet, created this recently, is starting (preflight), not idle.
STARTING_SECONDS = 60.0
# Phases after which an accepted iteration's record is final (with the same iteration
# number in the status). Before them (accept, stop check, or one read before those),
# the harness may still rewrite it.
FINAL_PHASES = ("between iterations", "report")
# Each file's place in the run folder and the most `wait` reads of it.
FILES = {"history": ("history.jsonl",), "segments": (".internal", "segments.jsonl"),
         "status": (".internal", "status.json"), "identity": ("run.json",)}
CAPS = {"history": 1 << 30, "segments": 16 << 20, "status": 1 << 20, "identity": 1 << 20}
LINE_CAP = 64 << 20  # one history record
_RANK = {"started": 0, "iteration": 1, "ended": 2, "crashed": 3}
_DEFAULTS = (-1, -1, 0, 0, 0)
_HOST = socket.gethostname()
_NOT_A_CURSOR = "not a cursor printed by `autohelix wait`"


@dataclass(frozen=True)
class Marks:
    """What a cursor says was reported for one run."""

    iteration: int = -1  # the last reported record's number (-1: none)
    fingerprint: int = -1  # and its start time's (`_fingerprint`)
    started: int = 0  # the last segment reported started (segments count from 1)
    ended: int = 0  # ... ended
    crashed: int = 0  # ... crashed

    def as_list(self) -> list[int]:
        values = [self.iteration, self.fingerprint, self.started, self.ended, self.crashed]
        while values and values[-1] == _DEFAULTS[len(values) - 1]:
            values.pop()
        return values

    def but(self, **changes) -> "Marks":
        return Marks(**{**self.__dict__, **changes})


def encode_cursor(marks: dict[str, Marks]) -> str:
    data = {"r": {run: m.as_list() for run, m in sorted(marks.items()) if m != Marks()}}
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
                or len(values) > len(_DEFAULTS) or any(type(v) is not int for v in values)):
            raise ValueError(_NOT_A_CURSOR)
        marks[run] = Marks(*values, *_DEFAULTS[len(values):])
    return marks


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fingerprint(started_at) -> int:
    """Tells a record from an earlier one with its number (a retried baseline): its
    start time in ms, modulo 10^6 (two attempts exactly a multiple of 1000 s apart, to
    the ms, would collide); -1 without one."""
    return int(round(started_at * 1000)) % 10**6 if _number(started_at) else -1


def process_gone(pid, host) -> bool:
    """Whether `pid` on `host` has certainly exited (only this host's can be checked;
    a reused pid looks alive)."""
    if host != _HOST or type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:  # PermissionError: someone else's live process
        return False
    return False


@dataclass
class _Event:
    time: float
    kind: str
    run: str
    fields: dict
    marks: Marks  # the run's marks once this event is reported

    def key(self):
        return self.time, _RANK[self.kind], self.run


@dataclass
class _Record:
    """What events need of one history record."""

    n: int
    fingerprint: int
    accepted: bool
    ended_at: float | None
    fields: dict


def _slim(record) -> _Record | None:
    if not isinstance(record, dict):
        return None
    n, accepted = record.get("iteration"), record.get("accepted")
    if type(n) is not int or n < 0 or type(accepted) is not bool:
        return None
    cost = record.get("cost")
    if not isinstance(cost, dict):
        calls = record.get("calls") if isinstance(record.get("calls"), list) else []
        summary = summarize_costs([c.get("usage") or {} for c in calls if isinstance(c, dict)])
        cost = {"cost_usd": summary.cost_usd, "incomplete": summary.incomplete}
    return _Record(n, _fingerprint(record.get("started_at")), accepted,
                   float(record["ended_at"]) if _number(record.get("ended_at")) else None, {
                       "iteration": n, "accepted": accepted, "reason": record.get("reason"),
                       "summary": record.get("summary"), "metrics": record.get("metrics") or {},
                       "commit": record.get("commit"), "cost_usd": cost.get("cost_usd"),
                       "cost_incomplete": bool(cost.get("incomplete")), "agent_error": record.get("agent_error"),
                   })


class RunSource:
    """One run's files, reread only when their stat changes (history.jsonl from where
    the last read ended, while it only grows)."""

    def __init__(self, runs: Path, run_id: str):
        self.runs = runs  # the project's runs folder: files are opened below it
        self.run = run_id
        self.paths = RunPaths(runs.parent.parent, run_id)
        self._seen: dict[str, tuple | None] = {}
        self.records: dict[int, _Record] = {}  # by number: the last line with it
        self._offset = 0  # history bytes consumed (complete lines)
        self._last_line = b""  # the last of them
        self._sorted: list[_Record] | None = None
        self.segments: list[dict] = []
        self.status: dict = {}
        self.created_at: float | None = None
        self._identity_read = False
        self.dead: set[int] = set()  # open segments whose process is gone
        self.version = 0  # bumped whenever anything read changed

    # --- reading ------------------------------------------------------------------

    def _path(self, name: str) -> Path:
        return self.runs.joinpath(self.run, *FILES[name])

    def _signature(self, name: str):
        """(inode, size, mtime) of a regular file; None for anything else (missing, a
        symlink, a FIFO: never opened)."""
        try:
            st = os.lstat(self._path(name))
        except OSError:
            return None
        return (st.st_ino, st.st_size, st.st_mtime_ns) if stat.S_ISREG(st.st_mode) else None

    def _open(self, name: str):
        """The file as a binary stream, or None: missing, or not a regular file reached
        without symlinks from the run folder (`safe_open.open_file`)."""
        if self._signature(name) is None:
            return None
        fd = open_file(self.runs / self.run, list(FILES[name]))
        return None if fd is None else os.fdopen(fd, "rb")

    def _read(self, name: str) -> bytes | None:
        stream = self._open(name)
        if stream is None:
            return None
        with stream:
            data = stream.read(CAPS[name] + 1)
        return data if len(data) <= CAPS[name] else None

    def _json(self, name: str) -> dict:
        data = self._read(name)
        try:
            value = json.loads(data) if data is not None else {}
        except ValueError:
            value = {}
        return value if isinstance(value, dict) else {}

    def _read_status(self) -> None:
        signature = self._signature("status")
        if self._seen.get("status", ()) != signature:
            self._seen["status"] = signature
            self.status = self._json("status")
            self.version += 1

    def _read_segments(self, force: bool = False) -> None:
        signature = self._signature("segments")
        if force or self._seen.get("segments", ()) != signature:
            self._seen["segments"] = signature
            data = self._read("segments")
            segments = read_segments(None, data=data or b"", terminated=True)
            if segments != self.segments:
                self.segments = segments
                self.version += 1

    def _read_history(self) -> None:
        signature = self._signature("history")
        old = self._seen.get("history", ())
        if old == signature:
            return
        self._seen["history"] = signature
        self.version += 1
        self._sorted = None
        # Appended to (same file, grown, still ending with the last line read): read on
        # from there. A replaced file (`History.update`) is read again from the start.
        grown = signature is not None and old and old[0] == signature[0] and signature[1] > old[1]
        stream = self._open("history")
        if stream is None:
            self.records, self._offset, self._last_line = {}, 0, b""
            return
        with stream:
            if grown and self._offset:
                stream.seek(self._offset - len(self._last_line))
                grown = stream.read(len(self._last_line)) == self._last_line
            if not grown:
                self.records, self._offset, self._last_line = {}, 0, b""
            stream.seek(self._offset)
            offset = self._offset
            while True:
                line = stream.readline(LINE_CAP)
                if not line.endswith(b"\n"):
                    if len(line) >= LINE_CAP:  # a line no record can be: unreadable
                        self.records, self._offset, self._last_line = {}, 0, b""
                        return
                    break  # unterminated: still being written
                offset += len(line)
                if offset > CAPS["history"]:
                    self.records, self._offset, self._last_line = {}, 0, b""
                    return
                self._last_line = line
                try:
                    record = _slim(json.loads(line))
                except ValueError:  # UnicodeDecodeError included
                    continue
                if record is not None:
                    self.records.pop(record.n, None)  # keep file order: the last line wins
                    self.records[record.n] = record
            self._offset = offset

    def refresh(self) -> None:
        """Reread what changed, in the reverse of the harness's write order: the status
        (a phase past an accepted record's rewrite is written after it), then the
        segment log (an end record after the segment's last history record), then the
        history, so what is read later is at least as new as what was read first.
        Then confirm crashes: an open segment whose process is gone is dead only if a
        reread of the log (after its process was seen gone: an ending process writes
        its end record first) still shows it open; a later segment's start proves it."""
        self._read_status()
        self._read_segments()
        self._read_history()
        if not self._identity_read:  # until run.json is there (a run being created)
            data = self._read("identity")
            if data is not None:
                self._identity_read = True
                try:
                    created = json.loads(data).get("created_at")
                except (ValueError, AttributeError):
                    created = None
                self.created_at = created if _number(created) else None
        dead = {s["segment"] for s in self.segments[:-1] if s["open"]}
        last = self.segments[-1] if self.segments else None
        if last is not None and last["open"] and process_gone(last.get("pid"), last.get("host")):
            self._read_segments(force=True)
            self._read_history()
            dead = {s["segment"] for s in self.segments[:-1] if s["open"]}
            if self.segments[-1]["segment"] == last["segment"] and self.segments[-1]["open"]:
                dead.add(last["segment"])
        if dead != self.dead:
            self.dead = dead
            self.version += 1

    # --- what the files say -------------------------------------------------------

    @property
    def live(self) -> bool:
        """The last segment is open and not known dead."""
        return bool(self.segments) and self.segments[-1]["open"] and self.segments[-1]["segment"] not in self.dead

    def _sorted_records(self) -> list[_Record]:
        if self._sorted is None:
            self._sorted = sorted(self.records.values(), key=lambda r: r.n)
        return self._sorted

    def _settled(self) -> list[_Record]:
        """The records, by number, but an accepted latest one the running harness may
        still rewrite: while its segment is open and live, and the status says that
        iteration is running in a phase before its record is final."""
        records = self._sorted_records()
        if records and records[-1].accepted and records[-1].n > 0 and self.live:
            s = self.status
            if (s.get("state") == "running" and s.get("iteration") == records[-1].n
                    and s.get("phase") not in FINAL_PHASES and not process_gone(s.get("pid"), s.get("host"))):
                return records[:-1]
        return records

    def _record_time(self, record: _Record) -> float:
        if record.ended_at is not None:
            return record.ended_at
        for path in (self.paths.record(record.n), self.paths.history):
            try:
                return os.lstat(path).st_mtime
            except OSError:
                pass
        return 0.0

    def _crash(self, segment: dict) -> tuple[float, int | None]:
        """A dead segment's time (its last sign of life) and the run's last iteration then:
        from the records before the next segment's start."""
        start = _time(segment.get("started_at"))
        later = [_time(s.get("started_at")) for s in self.segments if s["segment"] > segment["segment"]]
        until = min(later, default=float("inf"))
        before = [(self._record_time(r), r.n) for r in self._sorted_records()]
        before = [(t, n) for t, n in before if t < until]
        times = [start] + [t for t, _ in before if t >= start]
        if not later and self.status.get("pid") == segment.get("pid"):
            times += [self.status[k] for k in ("updated_at", "stopped_at") if _number(self.status.get(k))]
        return max(times), max((n for _, n in before), default=None)

    def state(self, now: float) -> str:
        """`running`, `starting`, or how it stands idle (see the module docstring)."""
        if self.segments:
            last = self.segments[-1]
            if last["open"]:
                return "crashed" if last["segment"] in self.dead else "running"
            return last.get("state") if isinstance(last.get("state"), str) else "ended"
        status = self.status
        if status.get("state") == "running":  # between the status's start and the log's
            return "crashed" if process_gone(status.get("pid"), status.get("host")) else "starting"
        if status.get("state") in ("finished", "interrupted", "failed"):
            return status["state"]
        if self.records:
            return "ended"
        created = self.created_at
        if created is None:
            signature = self._signature("identity") or self._dir_signature()
            created = signature[2] / 1e9 if signature else None
        return "starting" if created is not None and now - created < STARTING_SECONDS else "new"

    def _dir_signature(self):
        try:
            st = os.lstat(self.runs / self.run)
        except OSError:
            return None
        return st.st_ino, st.st_size, st.st_mtime_ns

    # --- events -------------------------------------------------------------------

    def pending(self, marks: Marks) -> list[_Event]:
        """The next pending event of each kind after `marks` (at most one each, the
        lowest: marks advance one event at a time, so nothing is skipped)."""
        events = []
        run = self.run
        for record in self._settled():
            if record.n > marks.iteration or (record.n == marks.iteration and record.fingerprint != marks.fingerprint):
                events.append(_Event(self._record_time(record), "iteration", run, dict(record.fields),
                                     marks.but(iteration=record.n, fingerprint=record.fingerprint)))
                break
        for segment in self.segments:
            n = segment["segment"]
            if n > marks.started:
                events.append(_Event(_time(segment.get("started_at")), "started", run, {
                    "segment": n, "start_iteration": segment.get("start_iteration"), "resumed": n > 1,
                }, marks.but(started=n)))
                break
        for segment in self.segments:
            n = segment["segment"]
            if n > marks.ended and not segment["open"]:
                events.append(_Event(_time(segment.get("ended_at")), "ended", run, {
                    "segment": n, "state": segment.get("state"), "stop_reason": segment.get("stop_reason"),
                    "error": segment.get("error"), "last_iteration": segment.get("last_iteration"),
                    "iterations": segment.get("iterations"),
                }, marks.but(ended=n)))
                break
        for segment in self.segments:
            n = segment["segment"]
            if n > marks.crashed and n in self.dead:
                at, last = self._crash(segment)
                events.append(_Event(at, "crashed", run, {"segment": n, "last_iteration": last},
                                     marks.but(crashed=n)))
                break
        return events

    def current(self) -> Marks:
        """Marks past everything reportable now (a fresh `wait`'s starting point)."""
        records = self._settled()
        last = records[-1] if records else None
        return Marks(iteration=last.n if last else -1, fingerprint=last.fingerprint if last else -1,
                     started=max((s["segment"] for s in self.segments), default=0),
                     ended=max((s["segment"] for s in self.segments if not s["open"]), default=0),
                     crashed=max(self.dead, default=0))


def _time(value) -> float:
    return float(value) if _number(value) else 0.0


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
    _quiet: dict[str, tuple] = field(init=False, default_factory=dict)  # run: (version, marks) with nothing pending

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
            self._quiet.pop(run, None)

    def _event(self, kind: str, run: str | None, at: float, fields: dict) -> dict:
        return {"schema": EVENT_SCHEMA, "event": kind, "run": run, "short": short_id(run) if run else None,
                "time": at, **fields, "cursor": self.cursor}

    def poll(self) -> dict | None:
        """One look: the oldest pending event (the cursor advanced past it), else None."""
        present = self._present()
        pending = []
        for source in self._scan(present):
            marks = self.marks.get(source.run, Marks())
            # Nothing changed since nothing was pending; a live segment may die unseen.
            if not source.live and self._quiet.get(source.run) == (source.version, marks):
                continue
            found = source.pending(marks)
            if found:
                self._quiet.pop(source.run, None)
                pending += found
            else:
                self._quiet[source.run] = (source.version, marks)
        self._prune(present)
        if not pending:
            return None
        event = min(pending, key=_Event.key)
        self.marks[event.run] = event.marks
        return self._event(event.kind, event.run, event.time, event.fields)

    def idle(self) -> dict | None:
        """With run ids: one more look, its event if it found one; else the `idle` event
        if none of the runs is running or starting (states from that same look)."""
        if self.run_ids is None:
            return None
        if (event := self.poll()) is not None:
            return event
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
