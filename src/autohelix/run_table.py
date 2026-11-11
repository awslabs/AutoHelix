# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""`dashboard --print` / `view --print`: the `--json` payloads as plain text.

Same words and choices as the web pages (dashboard.js, viewer.js), one snapshot,
no colors, so it can be piped.
"""

from __future__ import annotations

import time
from datetime import datetime
from autohelix.formatting import format_delta, format_duration

WORDS = {"crashed": "stopped unexpectedly", "idle": "not started"}  # others as they are (dashboard.js)


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
    """One word for the row's state (abandoned wins over the last segment's outcome), with the phase while running."""
    if not _live(run):
        kind = "abandoned" if run.get("lifecycle") == "abandoned" else (run.get("status") or {}).get("state") or "idle"
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
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return ["  ".join(cell.rjust(w) if i in right else cell.ljust(w)
                      for i, (cell, w) in enumerate(zip(row, widths))).rstrip() for row in rows]


def _where(data: dict) -> str:
    head = f"{data['branch'] or 'detached'} @ {data['commit']}" if data["commit"] else None
    return " · ".join(filter(None, [f"{data['host']}:{data['path_display']}", head]))


def format_runs(data: dict, metric: str | None = None, now: float | None = None) -> str:
    """The dashboard: live runs first, then by last activity; abandoned runs are counted, not listed."""
    now = time.time() if now is None else now
    runs = data["runs"]
    abandoned = [r for r in runs if r.get("lifecycle") == "abandoned"]
    shown = sorted((r for r in runs if r.get("lifecycle") != "abandoned"),
                   key=lambda r: (not _live(r), -(r.get("last_activity") or 0)))
    progress = [r["progress"] for r in runs if "progress" in r]  # not on unreadable runs
    costs = [p["cost_usd"] for p in progress if p["cost_usd"] is not None]
    summary = [f"{len(shown)} open run{'' if len(shown) == 1 else 's'}"]
    if costs:
        summary.append(f"{_money(sum(costs), any(p['cost_incomplete'] for p in progress))} spent")
    if abandoned:
        summary.append(f"{len(abandoned)} abandoned (not listed; see --json)")
    lines = [f"{data['project']}  {_where(data)}", " · ".join(summary)]
    if not shown:
        return "\n".join(lines + ["", "No runs yet." if not runs else "No open runs."]) + "\n"

    metric = metric or next(iter(metric_names(shown)), None)
    head = ["RUN", "STATUS", "ITER"] + ([metric] if metric else []) + ["LANDING", "ACTIVE"]
    rows = [head]
    for run in shown:
        if run.get("error"):
            rows.append([run["id"], f"unreadable: {run['error']}"] + [""] * (len(head) - 2))
            continue
        rows.append([run["id"], _state(run, now), _iterations(run)]
                    + ([_metric(run, metric)] if metric else [])
                    + [_landing(run), "now" if _live(run) else _ago(run["last_activity"], now)])
        rows.append(["  " + (run["config_file"] or ""), _spent(run, now)] + [""] * (len(head) - 2))
    return "\n".join(lines + [""] + _table(rows, right={2, 3} if metric else {2})) + "\n"


def format_run(data: dict, now: float | None = None) -> str:
    """One run: what it is doing, where its accepted work stands, and one row per iteration."""
    now = time.time() if now is None else now
    run = data["run"]
    under_way = _under_way(run)
    lines = [" · ".join([run["id"], _state(run, now)] + ([f"iteration {under_way}"] if under_way else []))]
    second = [run["config_file"], _spent(run, now), f"iterations {_iterations(run)}"]
    landing = _landing(run)
    if landing != "—":
        second.append(f"{landing} ({run['target_branch']})")
    lines.append(" · ".join(filter(None, second)))
    stop = (run["status"] or {}).get("stop_reason") or {}
    if not _live(run) and stop.get("detail"):
        lines.append(f"Stopped: {stop['detail']}")
    if run["goal"]:
        lines.append(f"Goal: {run['goal'].splitlines()[0]}")
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
        summary = (r["summary"] or ("baseline" if r["iteration"] == 0 else "")).strip()
        rows.append([str(r["iteration"]), "✓" if r["accepted"] else "✗"]
                    + [_number((r["metrics"] or {}).get(n)) for n in names]
                    + [_money(r["cost"], r["cost_incomplete"]),
                       format_duration(r["seconds"]) if r["seconds"] else "—",
                       summary.splitlines()[0] if summary else ""])
    if under_way:
        rows.append([str(under_way), "…"] + [""] * len(names) + ["", "", _state(run, now)])
    return "\n".join(lines + [""] + _table(rows, right={0, *range(2, len(names) + 4)})) + "\n"
