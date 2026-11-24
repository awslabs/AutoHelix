# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared subprocess runner for agent backends."""

import codecs
import os
import select
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class ProcessRunResult:
    """Outcome from a subprocess execution."""

    exit_code: int
    timed_out: bool = False


def _ignore(signum, frame) -> None:
    pass


@contextmanager
def child_signals():
    """Children started in this block (commands, agents) get SIGINT and SIGQUIT at
    their defaults, also when the harness ignores them (a background run: `nohup
    autohelix run ... &`). An ignored signal survives exec, a caught one is reset, so
    while the child starts an ignored one is caught by a handler that does nothing:
    the harness still ignores it. Main thread only, where handlers can be set (no
    `preexec_fn`: unsafe with threads)."""
    caught = []
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGQUIT):
            if signal.getsignal(signum) == signal.SIG_IGN:
                signal.signal(signum, _ignore)
                caught.append(signum)
    try:
        yield
    finally:
        for signum in caught:
            signal.signal(signum, signal.SIG_IGN)


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


def _kill_process_group(process: subprocess.Popen, grace: float = 3) -> None:
    """Kill a process and its entire process group (SIGTERM, then SIGKILL after `grace` s).
    The group is the process's own (`start_new_session`): its id is the process's pid,
    also when the process exits right after the `poll` (where `getpgid` would raise)."""
    if process.poll() is not None:
        return

    pgid = process.pid

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        process.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(pgid, signal.SIGKILL)
        process.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass


def end_group(proc) -> None:
    """SIGKILL whatever is left of a process group (its own session, from
    `start_new_session`), also after its leader (the shell, the agent) exited: a
    background child must not keep running, and writing files, once the harness has
    moved on. A child that starts a session of its own escapes this (until commands
    run in a sandbox).

    PID reuse: once the shell is reaped, its PID (the group's id) is free again only
    when no member of the group is left; a new process could then take it as its own
    group id before this kill, a narrow race. Closing it means not reaping the shell
    until after the kill (waitid with WNOWAIT, then reading the pipes ourselves instead
    of `communicate`), which `subprocess` doesn't offer; left as is."""
    if not isinstance(getattr(proc, "pid", None), int):
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:  # nothing left
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
            with child_signals():
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

            # After end of file (the child closed its output, maybe still
            # running), the pipe is no longer selected: wait for the process
            # instead, so the deadline still applies and nothing spins.
            output_closed = False
            while True:
                if output_closed:
                    try:
                        process.wait(timeout=1.0)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                else:
                    ready, _, _ = select.select([stdout_fd], [], [], 1.0)
                    if ready:
                        chunk = os.read(stdout_fd, 4096)
                        if not chunk:
                            output_closed = True
                            continue
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
                # Whatever is still running: also a grandchild writing to the pipe
                # after the process itself exited.
                if deadline is not None and now >= deadline:
                    log_file.write("[autohelix] Timeout\n")
                    log_file.flush()
                    _kill_process_group(process)
                    end_group(process)
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

            exit_code = process.wait()
            # A background child (silent, or holding the pipe open) must not
            # outlive a normal exit either.
            end_group(process)
            return ProcessRunResult(exit_code=exit_code)
    except BaseException:
        # Ctrl-C, or anything raised while the agent runs (a line callback, a full disk
        # writing the log): the agent must not go on working, unbounded, without the loop.
        if process is not None:
            _kill_process_group(process)
            end_group(process)
        raise
