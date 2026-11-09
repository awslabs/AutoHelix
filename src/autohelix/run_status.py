# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Live run status: the harness writes .autohelix/runtime/run.json, observers read it."""

import json
import os
import socket
import time
from pathlib import Path


def _path(project: Path) -> Path:
    return project / ".autohelix" / "runtime" / "run.json"


class RunStatus:
    """Records what the harness is doing right now (iteration and phase)."""

    def __init__(self, project: Path):
        self.path = _path(project)
        self.data: dict = {}

    def start(self, **info) -> None:
        now = time.time()
        self.data = {"state": "running", "pid": os.getpid(), "host": socket.gethostname(),
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


def read_run_status(project: Path) -> dict | None:
    """Return the last recorded status, with state corrected if the process died."""
    try:
        data = json.loads(_path(project).read_text())
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
