"""Keep the machine awake while a `run`/`resume` segment runs.

macOS: `caffeinate -i -w <pid>`; Linux: `systemd-inhibit` holding an idle/sleep block
for as long as `tail --pid=<pid>` waits. Both watch this process, so the block goes
when it ends however it ends (killed included); `keep_awake` also releases it when
the segment ends. A missing or refused tool (a container, no systemd, polkit over
ssh; a `tail` without GNU `--pid`, as on busybox) means no block and no message.
This doesn't stop a closed lid from sleeping, and keeps no credentials or VPN alive.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from contextlib import contextmanager
from typing import Iterator


def inhibit_command(pid: int, platform: str) -> list[str] | None:
    """The command that keeps the machine awake until process `pid` ends (None: none on
    this platform)."""
    if platform == "darwin":
        return ["caffeinate", "-i", "-w", str(pid)]
    if platform.startswith("linux"):
        return ["systemd-inhibit", "--what=idle:sleep", "--who=AutoHelix", "--why=an AutoHelix run is iterating",
                "--mode=block", "tail", f"--pid={pid}", "-f", "/dev/null"]
    return None


def _start(command: list[str]) -> subprocess.Popen | None:
    """Start `command` quietly in its own session (Ctrl-C in the terminal doesn't reach
    it); None when it can't start. The root conftest.py replaces this in every test."""
    if shutil.which(command[0]) is None:
        return None
    try:
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None


def _release(process: subprocess.Popen) -> None:
    """End the block: its whole process group (systemd-inhibit and its `tail`)."""
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.wait()


@contextmanager
def keep_awake() -> Iterator[None]:
    """Keep the machine awake for the `with` block (silently nothing when it can't)."""
    command = inhibit_command(os.getpid(), sys.platform)
    process = _start(command) if command else None
    try:
        yield
    finally:
        if process is not None:
            _release(process)
