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

import re
import time
from datetime import datetime

from rich.text import Text

from autohelix import terminal
from autohelix.formatting import clean, format_cap, format_duration
from autohelix.iteration_time import format_duration as format_limit
from autohelix.runs import short_id
from autohelix.terminal import Row, Table

WORDS = {"crashed": "stopped unexpectedly", "idle": "not started"}  # others as they are (dashboard.js)
# The status's phase names, in the words the run's live row uses.
PHASES = {"preparing checks": "gates", "checks": "gates", "benchmark": "observe", "stop check": "stop",
          "report": "summary"}
STATE_STYLES = {"stopped unexpectedly": "#d77a7a", "interrupted": "yellow", "failed": "#d77a7a"}
_AGENT_FAILED = re.compile(r"Iteration \d+ agent failed: (.*)", re.S)


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
    return " · ".join(filter(None, [format_duration(seconds) if seconds else None,
                                    terminal.money(p["cost_usd"], p["cost_incomplete"])]))


def time_cap(run: dict, now: float, short: bool = False) -> str:
    """The running segment's --time against its clock: `12m5s of 1h (this segment)`,
    or with `short` (the dashboard's row) `12m5s/1h`; past the cap `(time up)` is
    added. "" when the run isn't running or the segment has no --time (a stopped
    run's stop reason says when time ended it). Never a run total. Both figures in
    the cap's format (`iteration_time.format_duration`: 1h30m)."""
    status = run.get("status") or {}
    cap, started = status.get("max_time_seconds"), status.get("segment_started_at")
    if (not _live(run) or not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap <= 0
            or not isinstance(started, (int, float))):
        return ""
    elapsed = max(0, int(now - started))
    text = f"{format_limit(elapsed)}/{format_limit(int(cap))}" if short else \
        f"{format_limit(elapsed)} of {format_limit(int(cap))} (this segment)"
    return text + " (time up)" * (elapsed >= cap)


def _iterations(run: dict) -> str:
    p = run["progress"]
    n = _under_way(run) or p["iterations"]
    return f"{n}/{p['max_iterations']}" if p["max_iterations"] else str(n)




def _config_file(run: dict) -> str:
    """The run's config file, marked when the run has no stored copy (an old run) or it is invalid."""
    file = run["config_file"] or ""
    flag = " (not stored)" if run.get("config_missing") else " (invalid)" if run.get("config_invalid") else ""
    return file + flag if file else ""


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
    return " · ".join(filter(None, [f"{data['host']}:{data['path_display']}", head]))


def _plain(lines: list[Text]) -> str:
    return "".join(line.plain.rstrip() + "\n" for line in lines)


# --- one run (`view`) ---------------------------------------------------------------


def row_of(item: dict) -> Row:
    """A `view --json` iteration as a row (an agent failure is told by its reason)."""
    reason = item.get("reason")
    failed = _AGENT_FAILED.fullmatch(reason) if isinstance(reason, str) and not item["accepted"] else None
    return Row(iteration=item["iteration"], accepted=item["accepted"], metrics=item.get("metrics") or {},
               seconds=item.get("seconds"), cost=item.get("cost"), cost_incomplete=item.get("cost_incomplete", False),
               commit="?" if item["accepted"] and item["iteration"] else None, reason=reason,
               agent_error=failed[1] if failed else None,
               summary=item.get("summary") if item["accepted"] else None,
               gates=item.get("gates") or [], observe=item.get("observe") or [],
               discarded=item.get("discarded") or [], discarded_frozen=item.get("discarded_frozen") or [])


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


def _landed(run: dict, short: str) -> tuple[str, str, str | None] | None:
    """The end block's landing line (`view --json`'s `landing`: commits not yet on the target)."""
    landing, target = run.get("landing"), run.get("target_branch")
    if not landing or not target:
        return None
    n = landing["commits"]
    if n == 0:
        return ("landed", f"on {target}", None) if run.get("accepted") else ("landed", "nothing (no accepted changes)", None)
    commits = f"{n} commit{'s' * (n != 1)}"
    if not landing["fast_forward"]:
        return "unlanded", f"{commits} on autohelix/{run['id']} · {target} has moved on (diverged)", "yellow"
    return "unlanded", f"{commits} on autohelix/{run['id']}", None


def view_parts(data: dict, width: int = 100, now: float | None = None,
               config=None) -> tuple[list[Text], list[list[Text]], list[Text]]:
    """`view`'s screen in three parts: the header and column header; each iteration's
    lines (its row and the lines under it); then the live row of the iteration under
    way, or the end block. `config`: the run's stored config (`runs.load_run_config`),
    for the header's fields and the metric gates' wording; without it, the payload's."""
    now = time.time() if now is None else now
    run = data["run"]
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
    head = terminal.header(title, fields, width, show_mark=False) + table.heading()
    rows = [table.add(row_of(item)) for item in run["iterations"]]
    if (under_way := _under_way(run)) is not None or _live(run):
        status = run["status"]
        started = status.get("iteration_started_at") if under_way is not None else status.get("phase_started_at")
        seconds = now - started if isinstance(started, (int, float)) else 0
        what = " · ".join(filter(None, [_phase(run), "stopping" if run.get("stop_requested") else ""]))
        tail = [table.live(under_way if under_way is not None else status.get("iteration") or 0, seconds, what,
                           int(now * 10))]
    else:
        commands = [f"autohelix resume {short} -n 5"]
        landing = _landed(run, short)
        if landing and landing[0] == "unlanded" and (run.get("landing") or {}).get("fast_forward"):
            commands.append(f"autohelix land {short}")
        landed = [r for r in table.rows if r.accepted and r.iteration > 0]
        tail = table.ending(_outcome(run), spent=terminal.money(run["progress"]["cost_usd"],
                                                                 run["progress"]["cost_incomplete"]),
                            landing=landing, landed=landed, commands=commands, files=[])
    return head, rows, tail


def view_lines(data: dict, width: int = 100, now: float | None = None, config=None) -> list[Text]:
    head, rows, tail = view_parts(data, width, now, config)
    return head + [line for row in rows for line in row] + tail


def format_run(data: dict, now: float | None = None, width: int = 100, config=None) -> str:
    """`view --once` as plain text."""
    return _plain(view_lines(data, width, now, config))


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
    head = ["run", "state", "iter"] + ([metric + arrow] if metric else []) + ["landing", "active", "goal"]
    right = {2, 3} if metric else {2}
    rows: list[tuple[list[str], str | None, str]] = []  # cells, the state's style, the line under it
    for run in shown:
        if run.get("error"):
            rows.append(([short_id(run["id"]), "unreadable"] + [""] * (len(head) - 2), terminal.RED,
                         clean(run["error"])))
            continue
        value = next((v["value"] for v in (run["metrics"] or {}).get("values", []) if v["name"] == metric), None)
        name = " ".join(filter(None, [short_id(run["id"]), run.get("name")]))
        cells = [name, _word(run), _iterations(run)] + ([terminal.number(value)] if metric else []) + \
            [_landing(run), "now" if _live(run) else _ago(run["last_activity"], now), run.get("title") or ""]
        under = [_phase(run, now) if _live(run) else "", run.get("group") and f"group {run['group']}",
                 _config_file(run), _spent(run, now), time_cap(run, now, short=True), _notes(run)]
        rows.append((cells, terminal.TEAL if _live(run) else STATE_STYLES.get(_word(run)),
                     " · ".join(filter(None, under))))
    cells = [[clean(cell) for cell in row] for row in [head] + [cells for cells, _, _ in rows]]
    widths = [max(len(row[i]) for row in cells) for i in range(len(head))]

    def line(row: list[str]) -> str:
        return "  ".join(c.rjust(w) if i in right else c.ljust(w) for i, (c, w) in enumerate(zip(row, widths))).rstrip()

    lines.append(terminal.cut(Text("  " + line(cells[0]), style="dim"), width))
    for row, (_, style, under) in zip(cells[1:], rows):
        text = Text("  " + line(row))
        text.stylize(style or "", 2 + widths[0] + 2, 2 + widths[0] + 2 + widths[1])
        lines.append(terminal.cut(text, width))
        if under:
            lines.append(terminal.cut(Text("    " + under, style="dim"), width))
    return lines


def format_runs(data: dict, metric: str | None = None, now: float | None = None, width: int = 100) -> str:
    """`dashboard --once` as plain text."""
    return _plain(dashboard_lines(data, width, metric, now))
