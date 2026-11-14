# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Live run status: the harness writes runs/<id>/.internal/status.json, observers read it;
and the segment log, runs/<id>/.internal/segments.jsonl."""

import json
import os
import socket
import time
from pathlib import Path
from autohelix.runs import RunPaths, SCHEMA


def _path(project: Path, run_id: str | None = None) -> Path:
    return RunPaths(project, run_id).status


class RunStatus:
    """Records what the harness is doing right now (iteration and phase)."""

    def __init__(self, project: Path, run_id: str | None = None):
        self.path = _path(project, run_id)
        self.data: dict = {}

    def start(self, **info) -> None:
        now = time.time()
        self.data = {"schema": SCHEMA, "state": "running", "pid": os.getpid(), "host": socket.gethostname(),
                     "started_at": now, "iteration": None, "phase": "starting",
                     "phase_started_at": now, **info}
        self._write()

    def phase(self, phase: str, iteration: int | None = None) -> None:
        if not self.data:
            return
        now = time.time()
        if iteration is not None and iteration != self.data.get("iteration"):
            self.data.update(iteration=iteration, iteration_started_at=now)
        self.data.update(phase=phase, phase_started_at=now)
        self._write()

    def note(self, **info) -> None:
        """Record extra fields (e.g. stop_reason) on a started run."""
        if not self.data:
            return
        self.data.update(info)
        self._write()

    def stop(self, state: str = "finished") -> None:
        if self.data.get("state") != "running":
            return
        self.data.update(state=state, phase=None, stopped_at=time.time())
        self._write()

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({**self.data, "updated_at": time.time()}))
            tmp.replace(self.path)
        except OSError:
            pass  # Status is best-effort; never break a run over it.


class SegmentLog:
    """runs/<id>/.internal/segments.jsonl, append-only: for every segment (one `run`
    or `resume` command) that started, a start record, and an end record when it
    ended, linked by `segment` (1, 2, ... in the run). Each record is one `write()`
    on an O_APPEND descriptor, then fsync: nothing earlier is ever rewritten, so a
    damaged line costs only itself. A segment whose process died has no end record
    (`read_segments` reports it open). Best-effort, like the status; the run lock
    keeps one writer."""

    def __init__(self, path: Path):
        self.path = path
        self.segment: int | None = None

    def start(self, *, started_at: float, budget: dict, start_iteration: int) -> None:
        # Past every number in the file, end records too (a damaged start line can't make one reused).
        self.segment = max((r["segment"] for r in _segment_records(self.path)), default=0) + 1
        self._append({"segment": self.segment, "event": "start", "started_at": started_at, "pid": os.getpid(),
                      "host": socket.gethostname(), "budget": budget, "start_iteration": start_iteration})

    def end(self, *, state: str | None, stop_reason: dict | None, last_iteration: int | None,
            iterations: int | None, error: str | None = None) -> None:
        """The end record for the segment `start` began (nothing without one)."""
        segment, self.segment = self.segment, None
        if segment is None:
            return
        self._append({"segment": segment, "event": "end", "ended_at": time.time(), "state": state,
                      "stop_reason": stop_reason, "last_iteration": last_iteration, "iterations": iterations,
                      "error": error})

    def _append(self, record: dict) -> None:
        data = (json.dumps(record) + "\n").encode()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o644)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass  # never break a run over it


def _segment_records(path: Path) -> list[dict]:
    """The log's records (objects with an int `segment`), damaged lines skipped."""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    records = []
    for line in data.splitlines():
        try:
            record = json.loads(line)
        except ValueError:  # UnicodeDecodeError included
            continue
        if isinstance(record, dict) and type(record.get("segment")) is int:
            records.append(record)
    return records


def read_segments(path: Path) -> list[dict]:
    """The segments in the log, oldest first: each start record with its end record's
    fields merged in (`ended_at`, `state`, `stop_reason`, `last_iteration`,
    `iterations`, `error`), and `open` true when it has none (still running, or its
    process died). Lines that aren't records (damaged, undecodable) are skipped."""
    segments: dict[int, dict] = {}
    for record in _segment_records(path):
        n = record["segment"]
        fields = {k: v for k, v in record.items() if k != "event"}
        if record.get("event") == "start":
            segments[n] = {"ended_at": None, "state": None, "stop_reason": None, "last_iteration": None,
                           "iterations": None, "error": None, **fields, "open": True}
        elif record.get("event") == "end" and n in segments:
            segments[n].update(fields, open=False)
    return [segments[n] for n in sorted(segments)]


def read_run_status(project: Path, run_id: str | None = None) -> dict | None:
    """Return the last recorded status, with state corrected if the process died."""
    paths = RunPaths(project, run_id)
    if paths.run_id is None:
        return None
    try:
        data = json.loads(paths.status.read_text())
    except (OSError, ValueError):
        return None
    if data.get("state") == "running" and data.get("host") == socket.gethostname():
        try:
            os.kill(int(data["pid"]), 0)
        except ProcessLookupError:
            data["state"] = "crashed"
        except (PermissionError, KeyError, TypeError, ValueError):
            pass
    return data


def running_iteration(status: dict | None) -> int | None:
    """The iteration a status says is running (the baseline is 0), else None: the one
    whose call folders may hold live transcripts (`RunPaths.live_transcript`)."""
    if not isinstance(status, dict) or status.get("state") != "running":
        return None
    n = status.get("iteration")
    return n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else None
