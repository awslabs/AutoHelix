# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""`autohelix watch`: follow a run in the terminal.

The run's status (.autohelix/runtime/status.json) says what is happening; watch shows
the log being written for it: the agent's during an iteration, the reviewer's
during a review, the summary agent's while the summary is written.
"""

import time
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from autohelix.display import LiveDisplay
from autohelix.formatting import format_duration
from autohelix.replay import LogTail
from autohelix.run_status import read_run_status
from autohelix.runs import RunPaths

POLL_SECONDS = 0.3
STOPPED = {"finished": "Finished", "interrupted": "Interrupted", "failed": "Failed",
           "crashed": "Stopped unexpectedly"}


def status_line(status: dict | None, now: float | None = None) -> str:
    """One line, worded like the viewer's status."""
    now = now or time.time()
    if not status:
        return "No run recorded yet. Waiting for `autohelix run`…"
    if status.get("state") != "running":
        when = time.strftime("%H:%M", time.localtime(status["stopped_at"])) if status.get("stopped_at") else ""
        word = STOPPED.get(status.get("state"), str(status.get("state")))
        reason = status.get("stop_reason") if status.get("state") == "finished" else None
        why = f" · {reason['detail']}" if isinstance(reason, dict) and reason.get("detail") else ""
        return f"{word}{' at ' + when if when else ''}{why}. Waiting for the next run…"
    since = format_duration(now - status.get("phase_started_at", now))
    if status.get("phase") == "report":
        return f"Running · writing the summary · {since}"
    if status.get("phase") == "baseline":
        return f"Running · measuring the baseline · {since}"
    n = status.get("iteration")
    where = "" if n is None else " · baseline" if n == 0 else f" · iteration {n}"
    return f"Running{where} · {status.get('phase')} · {since}"


def log_for(project: Path, status: dict | None, current: Path | None, run_id: str | None = None) -> tuple[Path, str, float] | None:
    """The log to show for this status: (path, panel title, when it started).

    Between the agent, review and summary phases (checks, merge, …) the panel stays
    on what it showed; a watcher started then shows the iteration's agent log.
    """
    if not status:
        return None
    paths = RunPaths(project, run_id)
    if not paths.run_id:
        return None
    logs = paths.logs
    phase, n = status.get("phase"), status.get("iteration")
    phase_start = status.get("phase_started_at", time.time())
    if phase in {"agent", "review"} and status.get("log"):
        label = "Baseline" if n == 0 else f"Iteration {n}"
        return Path(status["log"]), f"{label} · {'reviewer' if phase == 'review' else 'agent'}", phase_start
    if phase == "report":
        return logs / "report.log", "Summary", phase_start
    if n is None:
        return None
    label = "Baseline" if n == 0 else f"Iteration {n}"
    if phase == "review":
        review = next((call for call in reversed(status.get("calls", [])) if call["role"] == "reviewer"), None)
        if review and review.get("log"):
            return paths.run_dir / review["log"], f"{label} · reviewer", phase_start
        return None
    if n == 0:
        return None  # the baseline only runs commands; the status line covers it
    agent = next((call for call in status.get("calls", []) if call["role"] == "agent"), None)
    if not agent or not agent.get("log"):
        return None
    agent_log = paths.run_dir / agent["log"]
    if phase == "agent" or current is None or f"iter-{n}" not in (current.parent.name, current.parent.parent.name):
        return agent_log, f"{label} · agent", status.get("iteration_started_at", phase_start)
    return None  # keep the current panel


def watch(project: Path, backend: str | None = None, console: Console | None = None, *, run_id: str | None = None) -> None:
    console = console or Console()
    current: Path | None = None
    tail: LogTail | None = None
    display: LiveDisplay | None = None
    started = 0.0
    with Live(console=console, refresh_per_second=4) as live:
        while True:
            status = read_run_status(project, run_id)
            running = bool(status) and status.get("state") == "running"
            if not status or (running and status.get("iteration") is None):
                current = tail = display = None  # cleared, or a new run is starting
            # While a run is going, follow its phases; otherwise show where it stopped, once.
            choice = log_for(project, status, current, run_id) if running or current is None else None
            # A new start time on the same log means the iteration is being rerun (resume).
            if choice and (choice[0], choice[2]) != (current, started):
                current, title, started = choice
                tail = None
                display = LiveDisplay(console=console, title=title,
                                      border_style="magenta" if "reviewer" in title else "blue", verbose=True)
            if tail is None and current is not None and current.is_file():
                tail = LogTail(current, backend)  # replays what is already there, then follows
            if tail is not None:
                try:
                    events = tail.read()
                except OSError:
                    events = []  # e.g. mid-rotation; the next poll retries
                if tail.reset:
                    display = LiveDisplay(console=console, title=display.title,
                                          border_style=display.border_style, verbose=True)
                for event in events:
                    display.handle_event(event)
            # A crashed run has no stopped_at; its last status update is when it was last alive.
            end = time.time() if running or not status else (
                status.get("stopped_at") or status.get("updated_at") or time.time())
            parts = [display.render(elapsed=int(end - started))] if display else []
            parts.append(Text(status_line(status), style="dim"))
            live.update(Group(*parts))
            time.sleep(POLL_SECONDS)
