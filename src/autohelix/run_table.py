# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The terminal `dashboard` / `view` (live, or `--once`): the `--json` payloads as plain text.

Same words and choices as the web pages (dashboard.js, viewer.js), one snapshot,
no colors, so it can be piped.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from autohelix.formatting import format_delta, format_duration

WORDS = {"crashed": "stopped unexpectedly", "idle": "not started"}  # others as they are (dashboard.js)
# Control characters (C0, DEL, C1): from agents, configs and paths, they could move the
# cursor, recolor or retitle the terminal, or start a fake row.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean(text) -> str:
    """Text safe to print in one terminal cell: tabs and newlines become spaces,
    other control characters "�"."""
    return CONTROL.sub(lambda m: " " if m[0] in "\t\n\r" else "�", str(text))


def _live(run: dict) -> bool:
    return (run.get("status") or {}).get("state") == "running"


def _number(value) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    return f"{value:.1f}" if abs(value) >= 100 else f"{value:.4g}"


def _money(cost, incomplete=False) -> str:
    return f"${cost:.2f}{'+' if incomplete else ''}" if isinstance(cost, (int, float)) else "—"


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


def _state(run: dict, now: float) -> str:
    """One word for the row's state (archived wins over the last segment's outcome), with the phase while running."""
    if not _live(run):
        kind = "archived" if run.get("archived") else (run.get("status") or {}).get("state") or "idle"
        return WORDS.get(kind, kind)
    status = run["status"]
    phase = "baseline" if status.get("iteration") == 0 else status.get("phase")
    since = status.get("phase_started_at")
    detail = f"{phase} {format_duration(now - since)}" if phase and isinstance(since, (int, float)) else phase
    return f"running · {detail}" if detail else "running"


def _spent(run: dict, now: float) -> str:
    """Time and cost so far; a running iteration's time is included, its cost comes at the end."""
    p = run["progress"]
    seconds = p["seconds"] or 0
    started = run["status"].get("iteration_started_at") if _under_way(run) else None
    if isinstance(started, (int, float)):
        seconds += now - started
    cost = p["cost_usd"]
    return " · ".join(filter(None, [format_duration(seconds) if seconds else None,
                                    _money(cost, p["cost_incomplete"]) if cost is not None else None]))


def _iterations(run: dict) -> str:
    p = run["progress"]
    n = _under_way(run) or p["iterations"]
    return f"{n}/{p['max_iterations']}" if p["max_iterations"] else str(n)


def _label(run: dict) -> str:
    """The run's id, with its group after it: `20261111-1432-k3f9 (perf)`."""
    return f"{run['id']} ({run['group']})" if run.get("group") else run["id"]


def _landing(run: dict) -> str:
    landing = run["landing"]  # None without a target branch or tip
    if not landing:
        return "—"
    if landing["commits"] == 0:
        return "landed" if run["accepted"] else "—"
    n = f"{landing['commits']} commit{'' if landing['commits'] == 1 else 's'}"
    return f"{n} to land" if landing["fast_forward"] else f"{n} · diverged"


def _metric(run: dict, name: str | None) -> str:
    values = (run["metrics"] or {}).get("values", [])
    return _number(next((v["value"] for v in values if v["name"] == name), None))


def metric_names(runs: list[dict]) -> list[str]:
    """Metric names across runs, each run's declared order first (the web page's menu)."""
    return list(dict.fromkeys(v["name"] for run in runs for v in (run.get("metrics") or {}).get("values", [])))


def _table(rows: list[list[str]], right: set[int]) -> list[str]:
    rows = [[clean(cell) for cell in row] for row in rows]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return ["  ".join(cell.rjust(w) if i in right else cell.ljust(w)
                      for i, (cell, w) in enumerate(zip(row, widths))).rstrip() for row in rows]


def _where(data: dict) -> str:
    head = f"{data['branch'] or 'detached'} @ {data['commit']}" if data["commit"] else None
    return " · ".join(filter(None, [f"{data['host']}:{data['path_display']}", head]))


def format_runs(data: dict, metric: str | None = None, now: float | None = None) -> str:
    """The dashboard: live runs first, then by last activity; archived runs are counted, not listed."""
    now = time.time() if now is None else now
    runs = data["runs"]
    archived = [r for r in runs if r.get("archived")]
    shown = sorted((r for r in runs if not r.get("archived")),
                   key=lambda r: (not _live(r), -(r.get("last_activity") or 0)))
    progress = [r["progress"] for r in runs if "progress" in r]  # not on unreadable runs
    costs = [p["cost_usd"] for p in progress if p["cost_usd"] is not None]
    summary = [f"{len(shown)} run{'' if len(shown) == 1 else 's'}"]
    if costs:
        summary.append(f"{_money(sum(costs), any(p['cost_incomplete'] for p in progress))} spent")
    if archived:
        summary.append(f"{len(archived)} archived (not listed; see --json)")
    lines = [f"{data['project']}  {_where(data)}", " · ".join(summary)]
    if not shown:
        return "\n".join(map(clean, lines + ["", "No runs yet." if not runs else "All runs are archived."])) + "\n"

    metric = metric or next(iter(metric_names(shown)), None)
    head = ["RUN", "STATUS", "ITER"] + ([metric] if metric else []) + ["LANDING", "ACTIVE", "GOAL"]
    rows = [head]
    for run in shown:
        if run.get("error"):
            rows.append([run["id"], f"unreadable: {run['error']}"] + [""] * (len(head) - 2))
            continue
        rows.append([_label(run), _state(run, now), _iterations(run)]
                    + ([_metric(run, metric)] if metric else [])
                    + [_landing(run), "now" if _live(run) else _ago(run["last_activity"], now),
                       run.get("title") or ""])
        rows.append(["  " + (run["config_file"] or ""), _spent(run, now)] + [""] * (len(head) - 2))
    return "\n".join(list(map(clean, lines)) + [""] + _table(rows, right={2, 3} if metric else {2})) + "\n"


def format_run(data: dict, now: float | None = None) -> str:
    """One run: what it is doing, where its accepted work stands, and one row per iteration."""
    now = time.time() if now is None else now
    run = data["run"]
    under_way = _under_way(run)
    lines = [" · ".join([_label(run), _state(run, now)] + ([f"iteration {under_way}"] if under_way else []))]
    second = [run["config_file"], _spent(run, now), f"iterations {_iterations(run)}"]
    landing = _landing(run)
    if landing != "—":
        second.append(f"{landing} ({run['target_branch']})")
    lines.append(" · ".join(filter(None, second)))
    stop = (run["status"] or {}).get("stop_reason") or {}
    if not _live(run) and stop.get("detail"):
        lines.append(f"Stopped: {stop['detail']}")
    title = run.get("title") or (run["goal"].splitlines()[0] if run["goal"] else None)
    if title:
        lines.append(f"Goal: {title}")
    metrics = run["metrics"]
    if metrics and metrics["iteration"] > 0:
        parts = [" ".join(filter(None, [f"{v['name']} {_number(v['value'])}",
                                        format_delta(v["value"], v["baseline"]) if v["baseline"] is not None else ""]))
                 for v in metrics["values"]]
        lines.append(f"Latest accepted (iteration {metrics['iteration']}): " + " · ".join(parts))

    iterations = run["iterations"]
    names = list(dict.fromkeys(name for r in iterations for name in r["metrics"] or {}))
    rows = [["ITER", "OK"] + names + ["COST", "TIME", "SUMMARY"]]
    for r in iterations:
        first, more = _summary(r)
        rows.append([str(r["iteration"]), "✓" if r["accepted"] else "✗"]
                    + [_number((r["metrics"] or {}).get(n)) for n in names]
                    + [_money(r["cost"], r["cost_incomplete"]),
                       format_duration(r["seconds"]) if r["seconds"] else "—", first])
        rows.extend([""] * (len(names) + 4) + [line] for line in more)
    if under_way:
        rows.append([str(under_way), "…"] + [""] * len(names) + ["", "", _state(run, now)])
    return "\n".join(list(map(clean, lines)) + [""] + _table(rows, right={0, *range(2, len(names) + 4)})) + "\n"


def _summary(r: dict) -> tuple[str, list[str]]:
    """An iteration's SUMMARY cell and the lines under it.

    Rejected: the failing gate (else the harness's reason), then the agent's summary.
    Accepted: the agent's summary. Then failed observe entries and discarded paths."""
    def first_line(text) -> str:
        text = (text or "").strip()
        return text.splitlines()[0] if text else ""

    reason = r.get("reason")
    summary = first_line(r.get("summary"))
    failed_gate = next((g for g in r.get("gates") or [] if isinstance(g, dict) and g.get("passed") is False), None)
    if r["iteration"] == 0 and not summary:
        cell, more = "baseline", []
    elif not r["accepted"]:
        cell = f"gate failed: {failed_gate.get('text')}" if failed_gate else first_line(reason) or summary
        more = [summary] if summary and summary != cell and summary != first_line(reason) else []
    else:
        cell, more = summary, []
    for entry in r.get("observe") or []:
        if isinstance(entry, dict) and entry.get("ok") is False:
            more.append(f"! observe {entry.get('name')} failed" + (f": {entry['error']}" if entry.get("error") else ""))
    discarded = r.get("discarded") or []
    if discarded:
        shown = ", ".join(discarded[:5]) + (f", … ({len(discarded)} paths)" if len(discarded) > 5 else "")
        more.append(f"! discarded, outside the editable scope: {shown}")
    return cell, more
