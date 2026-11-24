# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""`view`'s terminal text (`--terminal`, or `--once`): the `--json` payloads drawn
with the pieces `run` prints (`terminal`), so a run looks the same everywhere.

A run's own screen: its header, one row per iteration, the live row of the
iteration under way, or, once it stopped, the end block. The dashboard (`view
--once` without a run id, and the entry screen's table): the project's runs, one
line each. Both return lines cut to the width (`rich.text.Text`:
colours on a terminal, plain when piped); `format_run` / `format_runs` are the same
as plain text.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from rich.cells import cell_len
from rich.text import Text

from autohelix import terminal
from autohelix.formatting import clean, format_cap, format_duration
from autohelix.iteration_time import format_limit
from autohelix.config import config_name, run_command
from autohelix.runs import LandingWords, short_id
from autohelix.terminal import Row, Table

WORDS = {"crashed": "stopped unexpectedly", "idle": "not started"}  # others as they are (dashboard.js)
# The status's phase names, in the words the run's live row uses.
PHASES = {"preparing checks": "gates", "checks": "gates", "observe": "observe", "stop check": "stop",
          "report": "summary"}
MIN_GOAL = 20  # the narrowest goal column the dashboard shows
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


def time_cap(run: dict, now: float) -> str:
    """The running segment's time against its --time, as the web pages say it: `12m 05s
    of 1h (this segment)` (the web: `12m5s of 1h`), past it `... (time up)`. "" when the run isn't running or the
    segment has no --time (a stopped run's stop reason says when time ended it). Never
    a run total."""
    status = run.get("status") or {}
    cap, started = status.get("max_time_seconds"), status.get("segment_started_at")
    if (not _live(run) or not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap <= 0
            or not isinstance(started, (int, float))):
        return ""
    used = max(0, int(now - started))
    return f"{format_duration(used)} of {format_limit(cap)} (this segment)" + " (time up)" * (used >= cap)


def _iterations(run: dict) -> str:
    p = run["progress"]
    n = _under_way(run) or p["iterations"]
    return f"{n}/{p['max_iterations']}" if p["max_iterations"] else str(n)


def _config_file(run: dict) -> str:
    """The run's config file, marked when its stored copy can't be read."""
    file = run["config_file"] or ""
    return file + (" (invalid)" if run.get("config_invalid") else "") if file else ""


def _notes(run: dict) -> str:
    """Why the run was started, what it continues from, why it was archived (run.json's
    notes, `run --why`, `--import`, `archive --why`), as the web pages show them."""
    source = (run.get("import") or {}).get("run")
    return " · ".join(filter(None, [run.get("why") and f"why: {run['why']}",
                                    source and f"continues from {source}",
                                    run.get("archive_why") and f"archived: {run['archive_why']}"]))


def _words(run: dict) -> LandingWords:
    """A run payload's `landing_words` (`runs.landing_words`, as `view --json` has them)."""
    words = run["landing_words"]
    return LandingWords(words["cell"], words["text"], words["note"], tuple(words["commands"]))


def _landing(run: dict) -> str:
    """The dashboard's landing cell: `landed`, `2 to land`, `2 diverged` (`runs.landing_words`)."""
    return _words(run).cell


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
    folder: str = ""  # the run's folder, as the end block names it

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
        return cls(config, rows, files, terminal.shown_path(paths.run_dir) + "/")


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
        return terminal.unlanded(n, f"autohelix/{run['id']}", _words(run).note,
                                 "yellow" if landing["relation"] == "diverged" else None)
    return terminal.landed(made, target)


def _live_what(run: dict) -> str:
    """The live row's words for a running run: the phase (`agent`, `gates`), an observe
    entry's name (`observe · bench`), `stopping` once `autohelix stop` asked."""
    status = run["status"]
    entry = status.get("entry") if status.get("phase") == "observe" and isinstance(status.get("entry"), str) else ""
    return " · ".join(filter(None, [_phase(run), clean(entry), "stopping" if run.get("stop_requested") else ""]))


def view_parts(data: dict, saved: Saved, width: int = 100, now: float | None = None, live: bool = False,
               show_mark: bool = False) -> tuple[list[Text], list[list[Text]], list]:
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
        # The status's caps are run totals (`run`'s header says this segment's "2 more"): said so.
        budget = [f"to iteration {progress['max_iterations']}"] if progress.get("max_iterations") else []
        budget += [f"{format_cap(progress['max_cost_usd'])} run total"] if progress.get("max_cost_usd") is not None else []
        budget += [left] if (left := time_cap(run, now)) else []
        called = run.get("agent") or {}  # the latest main-agent call's (`latest_agent_call`)
        agent = terminal.agent_text(config.agent.cli, called.get("model") or config.agent.model,
                                    called.get("executable"))
        fields = terminal.run_fields(config, " · ".join(budget), agent)
        table = Table.of(config, width)
    else:
        fields = [("Goal", run.get("title") or (run.get("goal") or "").split("\n")[0]),
                  ("Config", _config_file(run)), ("Budget", time_cap(run, now))]
        table = Table(_directions([run]) or {name: "higher" for r in run["iterations"] for name in r["metrics"] or {}},
                      width)
    fields += [("Group", run.get("group") or ""), ("Notes", _notes(run))]
    head = terminal.header(title, fields, width, show_mark)  # the mark as `run` has it (`terminal.shows_mark`)
    if saved.rows is None:  # no column header over no rows
        head += terminal.warning("The run's history can't be read: no iterations to show.", width)
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
        accepted = sum(1 for r in table.rows if r.accepted and r.iteration > 0 and r.commit)
        commands = terminal.next_commands(short, view=False, also=_words(run).commands, restart=_restart(run))
        tail = table.ending(_outcome(run), spent=terminal.money(run["progress"]["cost_usd"],
                                                                 run["progress"]["cost_incomplete"]),
                            landing=_landed(run, accepted), commands=commands, files=list(saved.files),
                            folder=saved.folder, target=run.get("target_branch") or "")
    return head, rows, tail


def view_lines(data: dict, saved: Saved, width: int = 100, now: float | None = None,
               show_mark: bool = False) -> list[Text]:
    head, rows, tail = view_parts(data, saved, width, now, show_mark=show_mark)
    return head + [line for row in rows for line in row] + tail


def format_run(data: dict, saved: Saved, now: float | None = None, width: int = 100) -> str:
    """`view --once` as plain text."""
    return _plain(view_lines(data, saved, width, now))


# --- the project's runs (the dashboard: `view --once`) ---------------------------------


def dashboard_lines(data: dict, width: int = 100, metric: str | None = None, now: float | None = None) -> list[Text]:
    """The dashboard: a header (project, where, runs and spend), then one line per run
    (live runs first, then by last activity). Archived runs are counted, not listed."""
    now = time.time() if now is None else now
    runs = data["runs"]
    archived = [r for r in runs if r.get("archived")]
    shown = current_runs(runs)
    progress = [r["progress"] for r in runs if "progress" in r]  # not on unreadable runs
    costs = [p["cost_usd"] for p in progress if p["cost_usd"] is not None]
    summary = [f"{len(shown)} run{'' if len(shown) == 1 else 's'}"]
    if costs:
        summary.append(f"{terminal.money(sum(costs), any(p['cost_incomplete'] for p in progress))} spent")
    if archived:
        summary.append(f"{len(archived)} archived (not listed; see --json)")
    fields = [("Project", _where(data), None), ("Runs", " · ".join(summary))]
    lines = terminal.header(terminal.title("runs", data.get("project"), version=terminal.version()),
                            fields, width, show_mark=False)
    if not shown:
        return lines + [Text("  " + ("No runs yet." if not runs else "All runs are archived."))]
    return lines + table_lines(shown, width, metric, now)


def current_runs(runs: list[dict]) -> list[dict]:
    """The runs the dashboard lists, in its order: not archived, live runs first, then
    by last activity."""
    return sorted((r for r in runs if not r.get("archived")),
                  key=lambda r: (not _live(r), -(r.get("last_activity") or 0)))


def table_lines(shown: list[dict], width: int = 100, metric: str | None = None, now: float | None = None
                ) -> list[Text]:
    """The dashboard's table of `shown` runs (`current_runs`): a column header, then one
    line per run, its goal last, cut to the width. Too narrow for every column, the cost
    goes first (as in `run`'s table), so the activity stays whole; the goal column needs
    `MIN_GOAL` characters. An unreadable run's line is its id and why, in red."""
    now = time.time() if now is None else now
    directions = _directions(shown)
    metric = metric or next(iter(directions), None)
    arrow = f" {'↓' if directions.get(metric) == 'lower' else '↑'}" if metric in directions else ""
    head = ["run", "state", "iter"] + ([metric + arrow] if metric else []) + ["landing", "cost", "activity", "goal"]
    cost, goal = len(head) - 3, len(head) - 1
    right = {2, cost} | ({3} if metric else set())
    rows: list[tuple[list[str], str | None] | dict] = []  # cells and the state's style, or an unreadable run
    for run in shown:
        if run.get("error"):
            rows.append(run)
            continue
        value = next((v["value"] for v in (run["metrics"] or {}).get("values", []) if v["name"] == metric), None)
        p = run["progress"]
        # A running run's activity is what it is doing now (`agent 1m 12s`), else when it last did something.
        cells = [" ".join(filter(None, [short_id(run["id"]), run.get("name")])), _word(run), _iterations(run)] + \
            ([terminal.number(value)] if metric else []) + \
            [_landing(run), terminal.money(p["cost_usd"], p["cost_incomplete"]) or "—",
             _phase(run, now) if _live(run) else _ago(run["last_activity"], now), run.get("title") or ""]
        rows.append(([clean(cell) for cell in cells], terminal.TEAL if _live(run) else STATE_STYLES.get(_word(run))))
    table = [head] + [cells for cells, _ in (r for r in rows if isinstance(r, tuple))]
    widths = [max(len(row[i]) for row in table) for i in range(goal)] + [0]
    widths[0] = max([widths[0]] + [len(short_id(r["id"])) for r in rows if isinstance(r, dict)])
    columns = list(range(len(head)))

    def used() -> int:
        return 2 + sum(widths[i] for i in columns) + 2 * (len(columns) - 1)

    if used() - 2 > width:  # too wide even without the goal: no cost column, the activity stays whole
        columns.remove(cost)
    if used() + MIN_GOAL > width:
        columns.remove(goal)

    def line(row: list[str]) -> str:
        return "  ".join(row[i].rjust(widths[i]) if i in right else row[i].ljust(widths[i]) for i in columns).rstrip()

    lines = [terminal.cut(Text("  " + line(head), style="dim"), width)]
    for row in rows:
        if isinstance(row, dict):  # unreadable: its id and why
            text = Text("  " + short_id(row["id"]).ljust(widths[0]) + "  ")
            text.append(f"unreadable: {clean(row['error'])}", style=terminal.RED)
            lines.append(terminal.cut(text, width))
            continue
        cells, style = row
        text = Text("  " + line(cells))
        text.stylize(style or "", 2 + widths[0] + 2, 2 + widths[0] + 2 + widths[1])
        lines.append(terminal.cut(text, width))
    return lines


def format_runs(data: dict, metric: str | None = None, now: float | None = None, width: int = 100) -> str:
    """`view --once` (no run id) as plain text."""
    return _plain(dashboard_lines(data, width, metric, now))


# --- bare `autohelix`: the entry screen ---------------------------------------------


def suggestion(run: dict) -> tuple[str, str] | None:
    """What the entry screen says under the table about a run: its state in the
    dashboard's words with where it is, and the one command that applies, or None:
    `view` a live run; `resume` an interrupted or crashed one, also with commits to land
    (resuming is the point, and an auto-landing resume lands them); else what lands
    waiting commits (`runs.LandingWords.commands`: `land` when the target can
    fast-forward, `git merge autohelix/<id>` when it diverged); else `resume` one that
    failed or never started. A run whose stored config can't be read (`config_invalid`:
    a run of an old layout, which `resume` refuses) is offered only what lands."""
    if run.get("error"):
        return None
    short = short_id(run["id"])
    state = (run.get("status") or {}).get("state")
    lands = _words(run).commands
    where = f"iteration {_under_way(run) or run['progress']['iterations']}"
    resumable = not run.get("config_invalid")
    if _live(run):
        command = f"autohelix view {short}"
    elif state in ("interrupted", "crashed") and resumable:
        command = f"autohelix resume {short}"
    elif lands:
        command, where = lands[0], _words(run).cell
    elif state == "failed" and (restart := _restart(run)):
        command = restart
    elif state in ("failed", "idle", None) and resumable:
        command = f"autohelix resume {short}"
    else:
        return None
    return f"{_word(run)} · {where}", command


def _restart(run: dict) -> str | None:
    """For a run that ended without an accepted baseline (a setup error: `resume` would
    refuse the fixed config), the command that starts a new run of its config."""
    if run.get("baseline") is not False or (run.get("status") or {}).get("state") != "failed":
        return None
    if not isinstance(file := run.get("config_file"), str) or config_name(file) is None:
        return None
    return run_command(file)


def entry_header(folder: str, width: int = 100, git: tuple | None = None, version: str | None = None
                 ) -> list[Text]:
    """The entry screen's header, drawn before the runs are read: `AutoHelix  v0.x ·
    <folder>`, with a Git field when a run would refuse the checkout or needs a commit
    (`git`, a field's value and style)."""
    fields = [("Git", *git)] if git else []
    return terminal.header(terminal.title(folder, version=version or terminal.version()), fields, width,
                           show_mark=False)


def entry_lines(data: dict, width: int = 100, now: float | None = None) -> list[Text]:
    """The entry screen between its header and its question: the dashboard's table of
    the project's current runs, one line per run with the command that applies
    (`suggestion`), the archived count, and the `--help` footer."""
    runs = data.get("runs") or []
    shown = current_runs(runs)
    archived = len(runs) - len(shown)
    lines: list[Text] = []
    if shown:
        lines += table_lines(shown, width, now=now)
        found = [(short_id(r["id"]), *s) for r in shown if (s := suggestion(r))]
        if found:
            lines.append(Text())
            ids = max(len(short) for short, *_ in found)
            states = max(len(state) for _, state, _ in found)
            for short, state, command in found:
                text = Text(f"  {short:<{ids}}  {clean(state):<{states}}  ").append("next: ", style="dim")
                if text.cell_len + cell_len(command) <= width:
                    lines.append(text.append(command, style=terminal.TEAL))
                else:  # a command is never cut: on its own line(s), broken at spaces only
                    lines.append(terminal.cut(text, width))
                    lines += terminal.hanging("    ", command, width, terminal.TEAL)
    footer = [f"{archived} archived" if shown else f"no current runs, {archived} archived"] if archived else []
    return lines + [Text()] + _footer(footer, width)


def outside_lines(folder: str, problem: str, fix: str | None, width: int = 100, version: str | None = None
                  ) -> list[Text]:
    """The entry screen outside a git checkout: the header, why a run refuses the folder
    (`cli._git_problem`) with the command that makes it a checkout, and the footer;
    nothing to start."""
    lines = entry_header(folder, width, version=version)
    lines += terminal.warning(f"{problem} A run refuses this folder, so there is nothing to start here.", width)
    if fix:  # whole, never broken at its spaces
        lines.append(Text("    to make it a checkout: ", style="dim").append(fix, style=terminal.TEAL))
    return lines + [Text()] + _footer([], width)


def _footer(parts: list[str], width: int) -> list[Text]:
    return terminal.hanging("  ", " · ".join(parts + ["autohelix --help: all commands"]), width, "dim")
