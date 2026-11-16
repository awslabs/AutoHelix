# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The terminal `view` and `dashboard` (live, or `--once`): the `--json` payloads
drawn with the pieces `run` prints (`terminal`), so a run looks the same everywhere.

`view` is the run's own screen: its header, one row per iteration, the live row of
the iteration under way, or, once it stopped, the end block. `dashboard` is the
project's runs, one row each. Both return lines cut to the width (`rich.text.Text`:
colours on a terminal, plain when piped); `format_run` / `format_runs` are the same
as plain text.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from rich.text import Text

from autohelix import terminal
from autohelix.formatting import clean, format_cap, format_duration
from autohelix.iteration_time import format_limit
from autohelix.runs import short_id
from autohelix.terminal import Row, Table

WORDS = {"crashed": "stopped unexpectedly", "idle": "not started"}  # others as they are (dashboard.js)
# The status's phase names, in the words the run's live row uses.
PHASES = {"preparing checks": "gates", "checks": "gates", "benchmark": "observe", "stop check": "stop",
          "report": "summary"}
STATE_STYLES = {"stopped unexpectedly": "#d77a7a", "interrupted": "yellow", "failed": "#d77a7a"}


def _live(run: dict) -> bool:
    return (run.get("status") or {}).get("state") == "running"


def _ago(epoch, now: float) -> str:
    if not isinstance(epoch, (int, float)):
        return "—"
    seconds = now - epoch
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    day = datetime.fromtimestamp(epoch)
    return f"{day:%b} {day.day}"


def _under_way(run: dict) -> int | None:
    """The iteration under way while running, if it is past the last one finished."""
    n = (run.get("status") or {}).get("iteration")
    return n if _live(run) and isinstance(n, int) and n > run["progress"]["iterations"] else None


def _word(run: dict) -> str:
    """The run's state in a word (archived wins over the last segment's outcome), with
    `(stopping)` once `autohelix stop` asked a running run to end."""
    if _live(run):
        return "running (stopping)" if run.get("stop_requested") else "running"
    kind = "archived" if run.get("archived") else (run.get("status") or {}).get("state") or "idle"
    return WORDS.get(kind, kind)


def _phase(run: dict, now: float | None = None) -> str:
    """What a running run is doing, in the live row's words (`agent`, `gates`, `baseline`),
    with how long it has been at it when `now` is given: `agent 1m 12s`."""
    status = run["status"]
    phase = "baseline" if status.get("iteration") == 0 else status.get("phase")
    phase = PHASES.get(phase, phase) or ""
    since = status.get("phase_started_at")
    return f"{phase} {format_duration(now - since)}" if phase and now and isinstance(since, (int, float)) else phase


def _spent(run: dict, now: float) -> str:
    """Time and cost so far; a running iteration's time is included, its cost comes at the end."""
    p = run["progress"]
    seconds = p["seconds"] or 0
    started = run["status"].get("iteration_started_at") if _under_way(run) else None
    if isinstance(started, (int, float)):
        seconds += now - started
    spent = " · ".join(filter(None, [format_duration(seconds) if seconds else None,
                                     terminal.money(p["cost_usd"], p["cost_incomplete"])]))
    return f"spent {spent}" if spent else ""


def time_cap(run: dict, now: float) -> str:
    """What is left of the running segment's --time: `47m 55s left (--time 1h)`, past
    it `time up (--time 1h)` (the limit as the flag spells it). "" when the run isn't
    running or the segment has no --time (a stopped run's stop reason says when time
    ended it). Never a run total."""
    status = run.get("status") or {}
    cap, started = status.get("max_time_seconds"), status.get("segment_started_at")
    if (not _live(run) or not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap <= 0
            or not isinstance(started, (int, float))):
        return ""
    left = int(cap) - max(0, int(now - started))
    return f"{format_duration(left)} left (--time {format_limit(cap)})" if left > 0 else f"time up (--time {format_limit(cap)})"


def _iterations(run: dict) -> str:
    p = run["progress"]
    n = _under_way(run) or p["iterations"]
    return f"{n}/{p['max_iterations']}" if p["max_iterations"] else str(n)


def _config_file(run: dict, unless_named: bool = False) -> str:
    """The run's config file, marked when the run has no stored copy (an old run) or it
    is invalid; with `unless_named`, "" when the run is named after it (the dashboard)."""
    file = run["config_file"] or ""
    flag = " (not stored)" if run.get("config_missing") else " (invalid)" if run.get("config_invalid") else ""
    named = unless_named and not flag and file.rsplit("/", 1)[-1] in (f"{run.get('name')}.yaml", "autohelix.yaml")
    return file + flag if file and not named else ""


def _notes(run: dict) -> str:
    """Why the run was started, what it continues from, why it was archived (run.json's
    notes, `run --why`, `--import`, `archive --why`), as the web pages show them."""
    source = (run.get("import") or {}).get("run")
    return " · ".join(filter(None, [run.get("why") and f"why: {run['why']}",
                                    source and f"continues from {source}",
                                    run.get("archive_why") and f"archived: {run['archive_why']}"]))


def _landing(run: dict) -> str:
    """The dashboard's landing cell: `landed`, `2 to land`, `2 diverged`."""
    landing = run["landing"]  # None without a target branch or tip
    if not landing:
        return "—"
    if landing["commits"] == 0:
        return "landed" if run["accepted"] else "—"
    return f"{landing['commits']} {'to land' if landing['fast_forward'] else 'diverged'}"


def _directions(runs: list[dict]) -> dict[str, str]:
    """Metric name -> direction across runs, each run's declared order first."""
    found: dict[str, str] = {}
    for run in runs:
        for value in (run.get("metrics") or {}).get("values", []):
            found.setdefault(value["name"], value.get("direction") or "higher")
    return found


def metric_names(runs: list[dict]) -> list[str]:
    """Metric names across runs, each run's declared order first (the web page's menu)."""
    return list(_directions(runs))


def _where(data: dict) -> str:
    head = f"{data['branch'] or 'detached'} @ {data['commit']}" if data["commit"] else None
    return " · ".join(filter(None, [data["path_display"], head]))


def _plain(lines: list[Text]) -> str:
    return "".join(line.plain.rstrip() + "\n" for line in lines)


# --- one run (`view`) ---------------------------------------------------------------


@dataclass
class Saved:
    """What `view` reads beside the `view --json` payload: the run's stored config
    (None: an old run, an invalid file), its rows from the history (as `run` printed
    them; None if unreadable) and the report files it has written (label, path)."""

    config: object = None
    rows: list[Row] | None = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def read(cls, paths) -> "Saved":
        from autohelix.history import History
        from autohelix.runs import load_run_config

        try:
            config = load_run_config(paths)
        except Exception:  # noqa: BLE001 - `view` shows what the payload has
            config = None
        try:
            records = sorted(History(paths.project, paths.run_id).load(), key=lambda r: r.iteration)
            rows = [Row.of(r) for r in records if r.iteration > 0 or r.accepted]
        except Exception:  # noqa: BLE001 - `view` says so
            rows = None
        files = [(label, terminal.shown_path(path))
                 for label, path in (("page", paths.report_html), ("summary", paths.report)) if path.is_file()]
        return cls(config, rows, files)


def _outcome(run: dict) -> tuple[str, str, str | None]:
    """The end block's first line for a run that isn't running."""
    status = run.get("status") or {}
    state = status.get("state")
    if state == "crashed":
        return "Stopped", "unexpectedly (its process is gone) · progress saved", terminal.RED
    if state == "interrupted":
        detail = (status.get("stop_reason") or {}).get("detail")
        return "Interrupted", " · ".join(filter(None, [detail, "progress saved"])), "yellow"
    if state == "failed":
        return "Error", (status.get("stop_reason") or {}).get("detail") or "the run failed", terminal.RED
    return terminal.outcome(status.get("stop_reason"), None, None)


def _landed(run: dict, made: int) -> tuple[str, str, str | None] | None:
    """The end block's landing line from `view --json`'s `landing` (commits not yet on
    the target) and the `made` commits of the run, noted as `run` notes them."""
    landing, target = run.get("landing"), run.get("target_branch")
    if not landing or not target:
        return None
    if n := landing["commits"]:
        if not landing["fast_forward"]:
            return terminal.unlanded(n, f"autohelix/{run['id']}", f"{target} has moved on", "yellow")
        return terminal.unlanded(n, f"autohelix/{run['id']}", "manual landing" if run.get("land") == "manual" else "")
    return terminal.landed(made, target)


def _can_land(run: dict) -> bool:
    """Whether `autohelix land` would fast-forward the target onto the run's commits."""
    landing = run.get("landing") or {}
    return bool(run.get("target_branch") and landing.get("commits") and landing.get("fast_forward"))


def _live_what(run: dict) -> str:
    """The live row's words for a running run: the phase (`agent`, `gates`), an observe
    entry's name (`observe · bench`), `stopping` once `autohelix stop` asked."""
    status = run["status"]
    entry = status.get("entry") if status.get("phase") == "benchmark" and isinstance(status.get("entry"), str) else ""
    return " · ".join(filter(None, [_phase(run), clean(entry), "stopping" if run.get("stop_requested") else ""]))


def view_parts(data: dict, saved: Saved, width: int = 100, now: float | None = None, live: bool = False
               ) -> tuple[list[Text], list[list[Text]], list]:
    """`view`'s screen in three parts: the header and column header; each iteration's
    lines (its row and the lines under it); then the live row of the iteration under
    way, or the end block. `saved` (`Saved.read`): the stored config gives the header's
    fields and the metric gates' wording (without it, the payload's); the rows are the
    history's. When the history can't be read, the end block is the outcome only: the
    totals and landing would be counted from nothing. With `live`, the live row is a
    `terminal.LiveRow` timed when it is drawn (the live `view`), else a line at `now`."""
    now = time.time() if now is None else now
    run, config = data["run"], saved.config
    short = short_id(run["id"])
    title = terminal.title(f"run {short}", run.get("name") or data.get("project"), _word(run),
                           version=terminal.version())
    if config is not None:
        progress = run["progress"]
        budget = [f"{progress['max_iterations']} iterations"] if progress.get("max_iterations") else []
        budget += [format_cap(progress["max_cost_usd"])] if progress.get("max_cost_usd") is not None else []
        fields = terminal.run_fields(config, " · ".join(budget))
        table = Table.of(config, width)
    else:
        fields = [("Goal", run.get("title") or (run.get("goal") or "").split("\n")[0]),
                  ("Config", _config_file(run))]
        table = Table(_directions([run]) or {name: "higher" for r in run["iterations"] for name in r["metrics"] or {}},
                      width)
    fields += [("Group", run.get("group") or ""), ("Notes", _notes(run))]
    head = terminal.header(title, fields, width, show_mark=False)
    if saved.rows is None:  # no column header over no rows
        head.append(Text("  ! The run's history can't be read: no iterations to show.", style="yellow"))
    else:
        head += table.heading()
    rows = [table.add(row) for row in saved.rows or []]
    if (under_way := _under_way(run)) is not None or _live(run):
        status = run["status"]
        started = status.get("iteration_started_at") if under_way is not None else status.get("phase_started_at")
        row = terminal.LiveRow(table, under_way if under_way is not None else status.get("iteration") or 0,
                               start=started if isinstance(started, (int, float)) else now, clock=time.time)
        row.set(_live_what(run))
        tail = [row if live else row.line(now)]
    elif saved.rows is None:
        tail = [Text(), terminal.rule(width), *table.outcome_lines(_outcome(run))]
    else:
        landed = [r for r in table.rows if r.accepted and r.iteration > 0]
        commands = terminal.next_commands(short, view=False, land=_can_land(run))
        tail = table.ending(_outcome(run), spent=terminal.money(run["progress"]["cost_usd"],
                                                                 run["progress"]["cost_incomplete"]),
                            landing=_landed(run, sum(1 for r in landed if r.commit)), landed=landed,
                            commands=commands, files=list(saved.files))
    return head, rows, tail


def view_lines(data: dict, saved: Saved, width: int = 100, now: float | None = None) -> list[Text]:
    head, rows, tail = view_parts(data, saved, width, now)
    return head + [line for row in rows for line in row] + tail


def format_run(data: dict, saved: Saved, now: float | None = None, width: int = 100) -> str:
    """`view --once` as plain text."""
    return _plain(view_lines(data, saved, width, now))


# --- the project's runs (`dashboard`) -------------------------------------------------


def dashboard_lines(data: dict, width: int = 100, metric: str | None = None, now: float | None = None) -> list[Text]:
    """The dashboard: a header (project, where, runs and spend), then one row per run
    (live runs first, then by last activity) with its config, time and cost under it.
    Archived runs are counted, not listed."""
    now = time.time() if now is None else now
    runs = data["runs"]
    archived = [r for r in runs if r.get("archived")]
    shown = sorted((r for r in runs if not r.get("archived")),
                   key=lambda r: (not _live(r), -(r.get("last_activity") or 0)))
    progress = [r["progress"] for r in runs if "progress" in r]  # not on unreadable runs
    costs = [p["cost_usd"] for p in progress if p["cost_usd"] is not None]
    summary = [f"{len(shown)} run{'' if len(shown) == 1 else 's'}"]
    if costs:
        summary.append(f"{terminal.money(sum(costs), any(p['cost_incomplete'] for p in progress))} spent")
    if archived:
        summary.append(f"{len(archived)} archived (not listed; see --json)")
    fields = [("Project", _where(data), None), ("Runs", " · ".join(summary))]
    lines = terminal.header(terminal.title("dashboard", data.get("project"), version=terminal.version()),
                            fields, width, show_mark=False)
    if not shown:
        return lines + [Text("  " + ("No runs yet." if not runs else "All runs are archived."))]

    directions = _directions(shown)
    metric = metric or next(iter(directions), None)
    arrow = f" {'↓' if directions.get(metric) == 'lower' else '↑'}" if metric in directions else ""
    head = ["run", "state", "iter"] + ([metric + arrow] if metric else []) + ["landing", "activity"]
    right = {2, 3} if metric else {2}
    rows: list[tuple[list[str], str | None, str, str, str]] = []  # cells, the state's style, goal, the line under it, notes
    for run in shown:
        if run.get("error"):
            rows.append(([short_id(run["id"]), "unreadable"] + [""] * (len(head) - 2), terminal.RED, "",
                         clean(run["error"]), ""))
            continue
        value = next((v["value"] for v in (run["metrics"] or {}).get("values", []) if v["name"] == metric), None)
        name = " ".join(filter(None, [short_id(run["id"]), run.get("name")]))
        # A running run's activity is what it is doing now (`agent 1m 12s`), else when it last did something.
        cells = [name, _word(run), _iterations(run)] + ([terminal.number(value)] if metric else []) + \
            [_landing(run), _phase(run, now) if _live(run) else _ago(run["last_activity"], now)]
        under = [run.get("group") and f"group {run['group']}", _config_file(run, unless_named=True), _spent(run, now),
                 time_cap(run, now)]
        rows.append((cells, terminal.TEAL if _live(run) else STATE_STYLES.get(_word(run)), clean(run.get("title")),
                     " · ".join(filter(None, under)), clean(_notes(run))))
    cells = [[clean(cell) for cell in row] for row in [head] + [cells for cells, *_ in rows]]
    widths = [max(len(row[i]) for row in cells) for i in range(len(head))]
    # The goal is the last column when every run's fits whole; else it goes under its row.
    used = 2 + sum(widths) + 2 * len(widths)
    goal_column = all(used + len(goal) <= width for _, _, goal, *_ in rows)
    if goal_column:
        cells[0].append("goal")
        widths.append(0)

    def line(row: list[str]) -> str:
        return "  ".join(c.rjust(w) if i in right else c.ljust(w) for i, (c, w) in enumerate(zip(row, widths))).rstrip()

    lines.append(terminal.cut(Text("  " + line(cells[0]), style="dim"), width))
    for row, (_, style, goal, under, notes) in zip(cells[1:], rows):
        text = Text("  " + line(row + [goal] * goal_column))
        text.stylize(style or "", 2 + widths[0] + 2, 2 + widths[0] + 2 + widths[1])
        lines.append(terminal.cut(text, width))
        if goal and not goal_column:
            lines.append(terminal.cut(Text("    " + goal), width))
        # Dim under the row: group, config, spend, then the notes, on their own line when they don't fit.
        both = " · ".join(filter(None, [under, notes]))
        if 4 + len(both) <= width:
            texts = [both]
        else:
            texts = [under, notes]
        lines += [terminal.cut(Text("    " + text, style="dim"), width) for text in texts if text]
    return lines


def format_runs(data: dict, metric: str | None = None, now: float | None = None, width: int = 100) -> str:
    """`dashboard --once` as plain text."""
    return _plain(dashboard_lines(data, width, metric, now))
