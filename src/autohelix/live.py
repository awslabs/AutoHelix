# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The live terminal views: `autohelix dashboard` and `autohelix view`.

`dashboard` redraws the run table (run_table.dashboard_lines). `view` shows one
run's screen (run_table.view_parts) above the log being written for what the
run is doing, which its status (runs/<id>/.internal/status.json) says: the
agent's during an iteration, an `ask`/`review` call's transcript while gate,
observe or stop commands make one (live from its call folder, then stored), the
summary agent's while the summary is written.
"""

import time
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from autohelix import run_table
from autohelix.display import LiveDisplay
from autohelix.formatting import clean, format_duration
from autohelix.replay import LogTail
from autohelix.terminal import ROOM, earlier_line, fold
from autohelix.safe_open import open_run_log
from autohelix.run_status import read_run_status, running_iteration
from autohelix.runs import RunPaths
from autohelix.viewer import ProjectRuns

POLL_SECONDS = 0.3  # the log and the run's status
TABLE_SECONDS = 5  # rereading the runs (history, config, git) for `dashboard`
REREAD_SECONDS = 5  # `view`: rereading the run (~200 ms) when none of its files changed (git, reports)
REFRESH_PER_SECOND = 10  # `view`: redrawing (the live row's spinner and clock are timed at draw time)
MIN_LOG_LINES = 5  # the log panel at its smallest: borders, header lines, one line of output
STOPPED = {"finished": "Finished", "interrupted": "Interrupted", "failed": "Failed",
           "crashed": "Stopped unexpectedly"}


def status_line(status: dict | None, now: float | None = None) -> str:
    """One line, worded like the viewer's status (the stop reason and phase come from
    the run's files: no control characters)."""
    return clean(_status_line(status, now))


def _status_line(status: dict | None, now: float | None = None) -> str:
    now = now or time.time()
    if not status:
        return "No run recorded yet. Waiting for `autohelix run`…"
    if status.get("state") != "running":
        when = time.strftime("%H:%M", time.localtime(status["stopped_at"])) if status.get("stopped_at") else ""
        word = STOPPED.get(status.get("state"), str(status.get("state")))
        # Interrupted with a reason: `autohelix stop --now`, or Ctrl-C while the ending landed.
        reason = status.get("stop_reason") if status.get("state") in ("finished", "interrupted") else None
        why = f" · {reason['detail']}" if isinstance(reason, dict) and reason.get("detail") else ""
        return f"{word}{' at ' + when if when else ''}{why}. Waiting for the next run…"
    since = format_duration(now - status.get("phase_started_at", now))
    if status.get("phase") == "report":
        return f"Running · writing the summary · {since}"
    if status.get("phase") == "baseline":
        return f"Running · measuring the baseline · {since}"
    n = status.get("iteration")
    where = "" if n is None else " · baseline" if n == 0 else f" · iteration {n}"
    phase = status.get("phase")
    return f"Running{where} · {run_table.PHASES.get(phase, phase)} · {since}"  # the live row's words


def log_for(project: Path, status: dict | None, current: Path | None, run_id: str) -> tuple[Path, str, float] | None:
    """The log to show for this status: (path, panel title, when it started).

    Between the agent and summary phases (checks, merge, …) the panel follows
    the iteration's latest `ask`/`review` call (`transcript_for`), else stays on
    what it showed; a watcher started then shows the iteration's agent log.
    """
    if not status:
        return None
    paths = RunPaths(project, run_id)
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
    if phase != "agent" and (transcript := transcript_for(paths, status)):
        # Started with the iteration: a later phase is the same call, a rerun (same call folder) a new one.
        return Path(transcript[0]), f"{label} · {transcript[1]}", status.get("iteration_started_at", phase_start)
    if n == 0:
        return None  # the baseline only runs commands; the status line covers it
    agent = next((call for call in status.get("calls", []) if call["role"] == "agent"), None)
    if not agent or not agent.get("log"):
        return None
    agent_log = paths.run_dir / agent["log"]
    if phase == "agent" or current is None or paths.iteration_dir(n) not in current.parents:
        return agent_log, f"{label} · agent", status.get("iteration_started_at", phase_start)
    return None  # keep the current panel


def transcript_for(paths: RunPaths, status: dict | None) -> tuple[str, str] | None:
    """The running iteration's latest `ask`/`review` call with a log that can be read
    (`RunPaths.readable_log`: its live transcript in the call folder while the command
    runs, the stored one after): (where it is, `RunPaths.usage_log`; its source)."""
    from autohelix.usage_log import read_usage

    n = running_iteration(status)
    if n is None:
        return None
    for usage in reversed(read_usage(paths.usage(n))):
        value = paths.usage_log(n, usage)
        if paths.readable_log(value, n):
            source = usage.get("source")
            return value, clean(source) if isinstance(source, str) and source else "call"
    return None


class LogFollower:
    """The log panel: follows the log the run's status points at, replaying what is already there."""

    def __init__(self, project: Path, backend: str | None, console: Console, run_id: str):
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
        if self.tail is None and self.current is not None:
            # The status file names the log; it's run state an agent can write, so the
            # log is opened only below the run's logs/ (or as an iteration's agent.log or
            # judge/review transcript), without symlinks or hard links.
            # A running call's transcript (in its call folder) only while its iteration runs.
            paths, log, running = RunPaths(self.project, self.run_id), self.current, running_iteration(status)
            self.tail = LogTail(log, self.backend, opener=lambda: open_run_log(paths, str(log), running))
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


def fit(parts: tuple[list, list[list], list], height: int) -> list:
    """The run's screen (`run_table.view_parts`) in `height` lines: the oldest iteration
    rows go first (with the lines under them), the baseline, header, live row and end
    block stay; then, if still too tall, the lines at the bottom."""
    head, rows, tail = parts
    lines = head + [line for row in rows for line in row] + tail
    if len(lines) <= height:
        return lines
    base = rows[:1] if rows and rows[0] and rows[0][0].plain.lstrip().startswith("· base") else []
    kept = list(rows[len(base):])
    room = height - len(head) - len(tail) - sum(map(len, base)) - 1  # the `… N earlier` line
    while kept and sum(map(len, kept)) > room:
        kept.pop(0)
    hidden = len(rows) - len(base) - len(kept)
    lines = head + [line for row in base + [[earlier_line(hidden)]] + kept for line in row] + tail
    return lines[:max(height, 1)]


def _read_run(runs: ProjectRuns, project: Path, run_id: str | None) -> tuple[dict, run_table.Saved] | str | None:
    """The run's `view --json` data and what `view` reads beside it (`run_table.Saved`),
    an error to show, or None while there is no run."""
    try:
        selected = RunPaths(project, run_id).run_id
        if not selected or not runs.exists(selected):
            return None
        return runs.run(selected), run_table.Saved.read(RunPaths(project, selected))
    except Exception as exc:  # noqa: BLE001 - shown, and retried on the next read
        return f"Cannot read the run: {exc}"


def _changes(project: Path, run_id: str | None) -> tuple:
    """What says the run's files changed since the last read (cheap: `stat` only): the
    current run, and its run.json, history and stop request (the status is compared whole)."""
    paths = RunPaths(project, run_id)

    def stamp(path: Path):
        try:
            info = path.stat()
        except OSError:
            return None
        return info.st_mtime_ns, info.st_size

    if not paths.run_id:
        return (None,)
    return paths.run_id, *(stamp(path) for path in (paths.identity, paths.history, paths.stop_request))


def view_screen(data: tuple[dict, run_table.Saved] | str | None, log: LogFollower, status: dict | None, height: int,
                width: int = 100) -> Group:
    """The run's screen on top, the log panel below it, the status line last, and while
    the run is live `ROOM` blank lines under it (not on the terminal's last lines). The live row
    is timed when drawn (`terminal.LiveRow`).

    On a short terminal the log panel shrinks first, then the oldest iteration rows go, then the panel.
    """
    if data is None:
        parts = None
    elif isinstance(data, str):
        parts = ([Text(clean(data))], [], [])
    else:
        run, saved = data
        head, rows, tail = run_table.view_parts(run, saved, width, live=True)
        # `fit` counts one terminal line per item: a word wider than the screen (a path,
        # a branch name, left whole by `terminal.wrap`) is folded here, where Rich would.
        folded = lambda lines: [piece for line in lines for piece in fold(line, width)]  # noqa: E731
        parts = (folded(head), [folded(row) for row in rows], folded(tail))
    running = bool(status) and status.get("state") == "running"
    # The status line, a spare line so the terminal doesn't scroll, the blank lines under a live run.
    room = height - 2 - ROOM * running
    lines = fit(parts, room - (MIN_LOG_LINES if log.display else 0)) if parts else []
    log_height = room - len(lines)
    if parts and (log_height < MIN_LOG_LINES or (parts[2] and lines[-1] is not parts[2][-1])):
        lines = fit(parts, room)  # no room for the log beside the whole end block: the table gets it all
        log_height = room - len(lines)
    return Group(*lines, *log.render(status, height=log_height), *[Text() for _ in range(ROOM * running)])


def view(project: Path, backend: str | None = None, console: Console | None = None, *,
         run_id: str) -> None:
    """Follow the run `run_id` until interrupted, across its segments.
    The run is reread when its status or files change (`_changes`), else every
    REREAD_SECONDS; the screen is redrawn REFRESH_PER_SECOND times a second meanwhile."""
    console = console or Console()
    runs = ProjectRuns(project)
    log = LogFollower(project, backend, console, run_id)
    data, read_at, seen = None, None, None
    with Live(console=console, refresh_per_second=REFRESH_PER_SECOND) as live:
        while True:
            status = read_run_status(project, run_id)
            log.poll(status)
            state = (status, _changes(project, run_id))
            if read_at is None or state != seen or time.monotonic() - read_at >= REREAD_SECONDS:
                data, read_at, seen = _read_run(runs, project, run_id), time.monotonic(), state
            live.update(view_screen(data, log, status, console.size.height, console.width))
            time.sleep(POLL_SECONDS)


def dashboard(project: Path, console: Console | None = None, *, metric: str | None = None) -> None:
    """Redraw the project's run table every TABLE_SECONDS until interrupted."""
    console = console or Console()
    runs = ProjectRuns(project)
    with Live(console=console, auto_refresh=False) as live:
        while True:
            lines = run_table.dashboard_lines(runs.runs(), console.width, metric=metric)
            footer = Text(f"  updated {time.strftime('%H:%M:%S')} · Ctrl-C to stop", style="dim")
            live.update(Group(*lines, Text(""), footer), refresh=True)
            time.sleep(TABLE_SECONDS)
