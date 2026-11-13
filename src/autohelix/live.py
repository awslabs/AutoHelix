# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The live terminal views: `autohelix dashboard` and `autohelix view`.

`dashboard` redraws the run table (run_table.format_runs). `view` shows one
run's table (run_table.format_run) above the log being written for what the
run is doing, which its status (runs/<id>/runtime/status.json) says: the
agent's during an iteration, the summary agent's while the summary is written.
"""

import time
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from autohelix import run_table
from autohelix.display import LiveDisplay
from autohelix.formatting import format_duration
from autohelix.replay import LogTail
from autohelix.run_status import read_run_status
from autohelix.runs import RunPaths
from autohelix.viewer import ProjectRuns

POLL_SECONDS = 0.3  # the log
TABLE_SECONDS = 5  # rereading the runs (history, config, git), unless the status changed
MIN_LOG_LINES = 5  # the log panel at its smallest: borders, header lines, one line of output
STOPPED = {"finished": "Finished", "interrupted": "Interrupted", "failed": "Failed",
           "crashed": "Stopped unexpectedly"}


def status_line(status: dict | None, now: float | None = None) -> str:
    """One line, worded like the viewer's status (the stop reason and phase come from
    the run's files: no control characters)."""
    return run_table.clean(_status_line(status, now))


def _status_line(status: dict | None, now: float | None = None) -> str:
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

    Between the agent and summary phases (checks, merge, …) the panel stays
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
    if phase == "agent" and status.get("log"):
        label = "Baseline" if n == 0 else f"Iteration {n}"
        return Path(status["log"]), f"{label} · agent", phase_start
    if phase == "report":
        return logs / "report.log", "Summary", phase_start
    if n is None:
        return None
    label = "Baseline" if n == 0 else f"Iteration {n}"
    if n == 0:
        return None  # the baseline only runs commands; the status line covers it
    agent = next((call for call in status.get("calls", []) if call["role"] == "agent"), None)
    if not agent or not agent.get("log"):
        return None
    agent_log = paths.run_dir / agent["log"]
    if phase == "agent" or current is None or f"iter-{n}" not in (current.parent.name, current.parent.parent.name):
        return agent_log, f"{label} · agent", status.get("iteration_started_at", phase_start)
    return None  # keep the current panel


class LogFollower:
    """The log panel: follows the log the run's status points at, replaying what is already there."""

    def __init__(self, project: Path, backend: str | None, console: Console, run_id: str | None = None):
        self.project, self.backend, self.console, self.run_id = project, backend, console, run_id
        self.current: Path | None = None
        self.tail: LogTail | None = None
        self.display: LiveDisplay | None = None
        self.started = 0.0

    def poll(self, status: dict | None) -> None:
        running = bool(status) and status.get("state") == "running"
        if not status or (running and status.get("iteration") is None):
            self.current = self.tail = self.display = None  # cleared, or a new run is starting
        # While a run is going, follow its phases; otherwise show where it stopped, once.
        choice = log_for(self.project, status, self.current, self.run_id) if running or self.current is None else None
        # A new start time on the same log means the iteration is being rerun (resume).
        if choice and (choice[0], choice[2]) != (self.current, self.started):
            self.current, title, self.started = choice
            self.tail = None
            self.display = LiveDisplay(console=self.console, title=title, border_style="blue", verbose=True)
        if self.tail is None and self.current is not None and self.current.is_file():
            self.tail = LogTail(self.current, self.backend)
        if self.tail is not None:
            try:
                events = self.tail.read()
            except OSError:
                events = []  # e.g. mid-rotation; the next poll retries
            if self.tail.reset:
                self.display = LiveDisplay(console=self.console, title=self.display.title,
                                           border_style=self.display.border_style, verbose=True)
            for event in events:
                self.display.handle_event(event)

    def render(self, status: dict | None, height: int | None = None) -> list:
        """The panel (at most `height` lines, none below MIN_LOG_LINES), if there is a log, then the status line."""
        running = bool(status) and status.get("state") == "running"
        # A crashed run has no stopped_at; its last status update is when it was last alive.
        end = time.time() if running or not status else (
            status.get("stopped_at") or status.get("updated_at") or time.time())
        show = self.display and (height is None or height >= MIN_LOG_LINES)
        parts = [self.display.render(elapsed=int(end - self.started), height=height)] if show else []
        return parts + [Text(status_line(status), style="dim")]


def _lines(text: str) -> Text:
    """Table text as it is printed, one terminal line per line (cut at the right edge, not
    wrapped), with no control characters but the line breaks."""
    return Text("\n".join(map(run_table.clean, text.rstrip("\n").split("\n"))), no_wrap=True, overflow="ellipsis")


def fit(text: str, height: int) -> str:
    """The run's table in `height` lines: the oldest iteration rows go first (with the
    lines under them: summary, failures), the header and column names stay."""
    lines = text.rstrip("\n").split("\n")
    if len(lines) <= height or "" not in lines:
        return "\n".join(lines[:max(height, 1)])
    head = lines.index("") + 2  # the run's lines, the blank line, the column names
    keep = max(0, height - head - 1)
    rows = lines[head:]
    # An iteration's own row has its number in the ITER column; the lines under it don't.
    starts = lambda part: [line for line in part if line[:len("ITER")].strip()]  # noqa: E731
    kept = rows[len(rows) - keep:] if keep else []
    while kept and not kept[0][:len("ITER")].strip():
        kept = kept[1:]  # don't start with the lines under a row that was left out
    hidden = len(starts(rows)) - len(starts(kept))
    more = f"  … {hidden} earlier iteration{'' if hidden == 1 else 's'}"
    return "\n".join(lines[:head] + [more] + kept)


def _read_run(runs: ProjectRuns, project: Path, run_id: str | None) -> dict | str | None:
    """The run's `view --json` data, an error to show, or None while there is no run."""
    try:
        selected = RunPaths(project, run_id).run_id
        if not selected or not runs.exists(selected):
            return None
        return runs.run(selected)
    except Exception as exc:  # noqa: BLE001 - shown, and retried on the next read
        return f"Cannot read the run: {exc}"


def view_screen(data: dict | str | None, log: LogFollower, status: dict | None, height: int) -> Group:
    """The run's table on top, the log panel below it, the status line last.

    On a short terminal the log panel shrinks first, then the oldest iteration rows go, then the panel.
    """
    table = "" if data is None else data if isinstance(data, str) else run_table.format_run(data)
    room = height - 2  # the status line, and a spare line so the terminal doesn't scroll
    lines = fit(table, room - (MIN_LOG_LINES if log.display else 0)) if table else ""
    log_height = room - (lines.count("\n") + 1 if lines else 0)
    if log_height < MIN_LOG_LINES and table:
        lines = fit(table, room)  # no room for the log: the table gets it all
    return Group(*([_lines(lines)] if lines else []), *log.render(status, height=log_height))


def view(project: Path, backend: str | None = None, console: Console | None = None, *,
         run_id: str | None = None) -> None:
    """Follow a run (the current one unless `run_id`) until interrupted, across its segments."""
    console = console or Console()
    runs = ProjectRuns(project)
    log = LogFollower(project, backend, console, run_id)
    data, read_at, seen = None, None, None
    with Live(console=console, refresh_per_second=4) as live:
        while True:
            status = read_run_status(project, run_id)
            log.poll(status)
            if read_at is None or status != seen or time.monotonic() - read_at >= TABLE_SECONDS:
                data, read_at, seen = _read_run(runs, project, run_id), time.monotonic(), status
            live.update(view_screen(data, log, status, console.size.height))
            time.sleep(POLL_SECONDS)


def dashboard(project: Path, console: Console | None = None, *, metric: str | None = None) -> None:
    """Redraw the project's run table every TABLE_SECONDS until interrupted."""
    console = console or Console()
    runs = ProjectRuns(project)
    with Live(console=console, auto_refresh=False) as live:
        while True:
            table = run_table.format_runs(runs.runs(), metric=metric)
            footer = Text(f"Updated {time.strftime('%H:%M:%S')} · Ctrl-C to stop", style="dim")
            live.update(Group(_lines(table), Text(""), footer), refresh=True)
            time.sleep(TABLE_SECONDS)
