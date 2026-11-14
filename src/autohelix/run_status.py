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
    """runs/<id>/.internal/segments.jsonl: one JSON line per segment (one `run` or
    `resume` command), written when it starts (`ended_at`, `last_iteration`,
    `stop_reason` and `state` null) and completed when it ends, by rewriting that
    line. Every write replaces the whole file atomically (it holds one short line per
    segment, and the run lock keeps one writer), so no line is ever torn; a segment
    whose process died keeps its open line. Best-effort, like the status."""

    def __init__(self, path: Path):
        self.path = path
        self.record: dict | None = None

    def start(self, *, started_at: float, budget: dict, start_iteration: int) -> None:
        self.record = {"started_at": started_at, "ended_at": None, "pid": os.getpid(),
                       "host": socket.gethostname(), "budget": budget, "start_iteration": start_iteration,
                       "last_iteration": None, "stop_reason": None, "state": None}
        self._write(self._lines() + [self.record])

    def end(self, *, state: str | None, stop_reason: dict | None, last_iteration: int) -> None:
        """Complete the line `start` wrote (appended again if it has gone)."""
        if self.record is None:
            return
        opened, self.record = self.record, None
        done = {**opened, "ended_at": time.time(), "last_iteration": last_iteration,
                "stop_reason": stop_reason, "state": state}
        lines = self._lines()
        if lines and lines[-1] == opened:
            lines[-1] = done
        else:
            lines.append(done)
        self._write(lines)

    def _lines(self) -> list:
        return read_segments(self.path, raw=True)

    def _write(self, lines: list) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text("".join(line if isinstance(line, str) else json.dumps(line) + "\n" for line in lines))
            tmp.replace(self.path)
        except OSError:
            pass  # never break a run over it


def read_segments(path: Path, raw: bool = False) -> list:
    """The segment log's records, oldest first; lines that aren't JSON objects are
    skipped (with `raw`, kept as they are, so a rewrite preserves them)."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return []
    records: list = []
    for line in text.splitlines(keepends=True):
        try:
            record = json.loads(line)
        except ValueError:
            record = None
        if isinstance(record, dict):
            records.append(record)
        elif raw and line.strip():
            records.append(line if line.endswith("\n") else line + "\n")
    return records


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
