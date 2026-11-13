# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Structured run logging.

Writes timestamped entries to runs/<id>/logs/run-<timestamp>.log
so users can tail -f or review after the fact.
"""

from datetime import datetime, timezone
from pathlib import Path
from autohelix.runs import RunPaths


class RunLog:
    """Appends structured log lines to a per-run log file."""

    def __init__(self, project_path: Path, run_id: str | None = None):
        self._logs_dir = RunPaths(project_path, run_id).logs
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._path = self._logs_dir / f"run-{ts}.log"

    @property
    def path(self) -> Path:
        return self._path

    def _write(self, level: str, message: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        line = f"{ts} [{level}] {message}\n"
        with open(self._path, "a") as f:
            f.write(line)

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warn(self, message: str) -> None:
        self._write("WARN", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)
