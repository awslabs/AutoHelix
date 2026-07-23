# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared subprocess runner for agent backends."""

import codecs
import os
import select
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class ProcessRunResult:
    """Outcome from a subprocess execution."""

    exit_code: int
    timed_out: bool = False


def format_timeout_seconds(timeout_seconds: int | None) -> str:
    """Render a timeout value as natural language."""
    if timeout_seconds is None:
        return "timed out"

    remaining = int(timeout_seconds)
    hours, remainder = divmod(remaining, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} hour" if hours == 1 else f"{hours} hours")
    if minutes:
        parts.append(f"{minutes} minute" if minutes == 1 else f"{minutes} minutes")
    if seconds or not parts:
        parts.append(f"{seconds} second" if seconds == 1 else f"{seconds} seconds")
    return f"timed out after {' '.join(parts)}"


def _kill_process_group(process: subprocess.Popen) -> None:
    """Kill a process and its entire process group."""
    if process.poll() is not None:
        return

    pgid = os.getpgid(process.pid)

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        process.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(pgid, signal.SIGKILL)
        process.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass


def run_process(
    cmd: list[str],
    cwd: Path,
    log_path: Path,
    line_callback: Callable[[str], None],
    heartbeat_seconds: int = 30,
    timeout_seconds: int | None = None,
    env: dict[str, str] | None = None,
) -> ProcessRunResult:
    """Run a subprocess, streaming decoded lines to callbacks and logs."""
    process: subprocess.Popen[bytes] | None = None
    try:
        with open(log_path, "w") as log_file:
            process = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                bufsize=0,
                start_new_session=True,
                env=env,
            )

            start = time.monotonic()
            deadline = start + timeout_seconds if timeout_seconds is not None else None
            next_heartbeat = start + heartbeat_seconds

            assert process.stdout is not None
            stdout_fd = process.stdout.fileno()
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            line_buffer = ""

            while True:
                ready, _, _ = select.select([stdout_fd], [], [], 1.0)
                if ready:
                    chunk = os.read(stdout_fd, 4096)
                    if not chunk:
                        if process.poll() is not None:
                            break
                        continue
                    else:
                        text = decoder.decode(chunk)
                        log_file.write(text)
                        log_file.flush()
                        line_buffer += text
                        while "\n" in line_buffer:
                            line, line_buffer = line_buffer.split("\n", 1)
                            if line.strip():
                                line_callback(line)
                elif process.poll() is not None:
                    break

                elapsed = int(time.monotonic() - start)
                now = time.monotonic()
                if process.poll() is None and deadline is not None and now >= deadline:
                    log_file.write("[autohelix] Timeout\n")
                    log_file.flush()
                    _kill_process_group(process)
                    return ProcessRunResult(exit_code=124, timed_out=True)
                if process.poll() is None and now >= next_heartbeat:
                    log_file.write(f"[autohelix] Heartbeat: {elapsed}s\n")
                    log_file.flush()
                    next_heartbeat = now + heartbeat_seconds

            tail = decoder.decode(b"", final=True)
            if tail:
                log_file.write(tail)
                log_file.flush()
                line_buffer += tail
            if line_buffer.strip():
                line_callback(line_buffer)

            return ProcessRunResult(exit_code=process.wait())
    except KeyboardInterrupt:
        if process is not None:
            _kill_process_group(process)
        raise
