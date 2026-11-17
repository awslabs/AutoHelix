"""Plain-text renders of fixed fake runs, compared with tests/snapshots/*.txt, so a
reader of the repo sees exactly what each screen looks like.

After an intended change of the output, rewrite them and review the diff:

    AUTOHELIX_UPDATE_SNAPSHOTS=1 pytest tests/test_snapshots.py
"""

import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

from autohelix import terminal
from autohelix.config import Config
from autohelix.harness import Budget
from autohelix.terminal import Row, Table

SNAPSHOTS = Path(__file__).parent / "snapshots"
NOW = datetime(2026, 11, 15, 14, 2)
WIDTHS = (80, 120)

CONFIG = Config.from_dict({
    "goal": "Make sort_list faster in sort.py.\nThe benchmark sorts 100k integers.",
    "agent": {"backend": "claude", "model": "sonnet", "timeout": "30m"},
    "gates": ["pytest -q test_sort.py", {"metric": "speed", "regress": "5%"}],
    "observe": [{"name": "bench", "command": "python bench.py", "metrics": {"speed": "higher", "mem_mb": "lower"}}],
    "scope": {"editable": ["sort.py"]},
})


def _gates(command: bool | None, metric: bool | None = None, **values) -> list[dict]:
    return [{"text": "pytest -q test_sort.py", "kind": "command", "passed": command,
             "return_code": None if command is None else 0 if command else 1},
            {"text": "speed regress 5%", "kind": "metric", "passed": metric, **values}]


ROWS = [
    Row(0, True, {"speed": 46.9, "mem_mb": 120}, gates=_gates(True)),
    Row(1, True, {"speed": 17481.5, "mem_mb": 118}, 31, 0.15, commit="a1",
        summary="Replace bubble sort with a merge/insertion hybrid", gates=_gates(True, True)),
    Row(2, False, {}, 143, 0.48, reason="gate failed: pytest -q test_sort.py", gates=_gates(False)),
    Row(3, True, {"speed": 35310.7, "mem_mb": 96}, 126, 0.32, commit="a3",
        summary="Switch to built-in sorted() with key caching", gates=_gates(True, True)),
    Row(4, False, {}, 1800, 1.10, reason="Iteration 4 agent failed: timed out after 30 minutes",
        agent_error="timed out after 30 minutes"),
    Row(5, False, {"speed": 33002.0, "mem_mb": 140}, 108, 0.29,
        reason="metric gate: speed 33002 is 6.5% worse than 35310.7 (regress: 5% allowed)",
        gates=_gates(True, False, value=33002.0, reference=35310.7),
        discarded=["autohelix.yaml", "notes.txt"], discarded_frozen=["autohelix.yaml"]),
    Row(6, True, {"speed": 57520.8, "mem_mb": 95}, 111, 0.31, commit="a6",
        summary="Avoid the defensive list copy; sort in place", gates=_gates(True, True)),
    Row(7, False, {}, 4, None, reason="Iteration 7 agent failed: claude exited 1: rate limited",
        agent_error="claude exited 1: rate limited"),
]


def _header(width: int, budget: Budget, done: int = 0, show_mark: bool = True) -> list:
    title = terminal.title("run krp3", "sorting", terminal.clock(NOW), version="0.3.0")
    return terminal.header(title, terminal.run_fields(CONFIG, terminal.budget_text(budget, done)), width, show_mark)


def _ending(table: Table, stop: dict, first: int = 1) -> list:
    """The end block as the harness assembles it (`Harness._print_ending`): auto landing
    of this segment's commits, the run's cost so far."""
    landed = [row for row in table.rows if row.iteration >= first and row.accepted and row.commit]
    return table.ending(terminal.outcome(stop, None, None), spent=terminal.money(sum(r.cost or 0 for r in table.rows)),
                        landing=terminal.landed(len(landed), "main"), landed=landed,
                        commands=terminal.next_commands("krp3"), files=FILES)


FILES = [("page", ".autohelix/runs/20261115-1402-sorting-krp3/report.html"),
         ("summary", ".autohelix/runs/20261115-1402-sorting-krp3/report.md")]


def run_screen(width: int, show_mark: bool = True) -> list:
    """`autohelix run -n 7 --cost 10`, finished."""
    table = Table.of(CONFIG, width)
    lines = _header(width, Budget(iterations=7, cost_usd=10), show_mark=show_mark)
    for row in ROWS:
        lines += table.add(row)
    return lines + _ending(table, {"kind": "budget", "detail": "iteration budget reached (7)"})


def live_screen(width: int) -> list:
    """`autohelix run -n 7 --cost 10` while iteration 4's agent works (the phase only)."""
    table = Table.of(CONFIG, width)
    lines = _header(width, Budget(iterations=7, cost_usd=10))
    for row in ROWS[:4]:
        lines += table.add(row)
    return lines + [table.live(4, 72, "agent")]


def resume_screen(width: int) -> list:
    """`autohelix resume -n 2` after the 7 iterations above, ended by a stop condition."""
    table = Table.of(CONFIG, width)
    lines = _header(width, Budget(iterations=2), done=7)
    lines += table.earlier(ROWS, "krp3", NOW.replace(hour=15, minute=40))
    lines += table.add(Row(8, True, {"speed": 61004.2, "mem_mb": 94}, 98, 0.27, commit="a8",
                           summary="Use a radix sort for small integer keys", gates=_gates(True, True)))
    return lines + _ending(table, {"kind": "condition", "detail": "stop condition met at iteration 8: speed above 60,000"},
                           first=8)


def setup_screen(width: int, picked: int | None = None) -> list:
    """`autohelix setup` in a terminal with two agents: the header and the menu as drawn
    while choosing; with `picked`, the menu folded into its answer instead, then what
    the template choice prints."""
    import click
    from rich.text import Text

    from autohelix.cli import SETUP_QUESTION, setup_options
    from autohelix.menu import option_line

    fields = terminal.setup_fields("~/code/sorting", "autohelix.yaml", False, ["Claude Code", "Codex"],
                                   ("main · clean",))
    lines = terminal.header(terminal.title("setup", version="0.3.0"), fields, width, show_mark=True)
    options = setup_options(["claude", "codex"])
    if picked is None:
        return lines + [Text("  " + SETUP_QUESTION)] + [Text(click.unstyle(option_line(options, i, 0, width - 1)))
                                                        for i in range(len(options))]
    lines.append(Text(f"  {SETUP_QUESTION} {options[picked][0]}"))
    steps = terminal.setup_steps("autohelix run")
    return lines + terminal.setup_written("autohelix.yaml", steps, terminal.setup_also("autohelix setup", "claude"), width)


def setup_problem_screen(width: int) -> list:
    """`autohelix setup perf` outside git (a real empty folder), no agent found, piped:
    the Git field says why a run would refuse, and the first step fixes it."""
    from autohelix.cli import _git_field

    with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, GIT_CEILING_DIRECTORIES=folder):
        git, fix = _git_field(Path(folder))
    fields = terminal.setup_fields("~/code/sorting", "autohelix/perf.yaml", False, [], git)
    lines = terminal.header(terminal.title("setup", version="0.3.0"), fields, width, show_mark=False)
    steps = terminal.setup_steps("autohelix run perf", fix)
    return lines + terminal.setup_written("autohelix/perf.yaml", steps, terminal.setup_also("", None), width)


# --- view and dashboard: the `--json` payloads of the run above ---

T = NOW.timestamp()


def _payload_row(row: Row) -> dict:
    """`view --json`'s iteration for a row (its agent failure in the reason, as stored)."""
    return {"iteration": row.iteration, "accepted": row.accepted, "metrics": row.metrics, "summary": row.summary,
            "reason": row.reason if row.iteration else None, "cost": row.cost, "cost_incomplete": False,
            "seconds": row.seconds, "gates": row.gates, "observe": [], "discarded": row.discarded,
            "discarded_frozen": row.discarded_frozen}


def _run_payload(running: bool) -> dict:
    rows = ROWS[:4] if running else ROWS
    status = ({"state": "running", "phase": "agent", "iteration": 4, "iteration_started_at": T - 72,
               "phase_started_at": T - 72, "max_iterations": 7} if running else
              {"state": "finished", "phase": None, "iteration": 7, "stopped_at": T,
               "stop_reason": {"kind": "budget", "detail": "iteration budget reached (7)"}})
    return {"id": "20261115-1402-sorting-krp3", "name": "sorting", "archived": False, "last_activity": T - 30,
            "config_file": "autohelix/sorting.yaml", "config_invalid": False,
            "goal": CONFIG.goal, "title": CONFIG.title, "status": status, "stop_requested": None,
            "accepted": 3, "group": None, "why": None, "archive_why": None, "import": None,
            "metrics": {"iteration": 6 if not running else 3, "values": [
                {"name": "speed", "value": 57520.8 if not running else 35310.7, "baseline": 46.9, "direction": "higher"},
                {"name": "mem_mb", "value": 95.0 if not running else 96.0, "baseline": 120.0, "direction": "lower"}]},
            "progress": {"iterations": len(rows) - 1, "max_iterations": 7,
                         "cost_usd": round(sum(r.cost or 0 for r in rows), 2),
                         "cost_incomplete": False, "seconds": sum(r.seconds or 0 for r in rows), "max_cost_usd": 10.0,
                         "max_seconds": None},
            "target_branch": "main", "landing": {"commits": 0, "fast_forward": True},
            "iterations": [_payload_row(row) for row in rows]}


PROJECT = {"schema": 1, "project": "sorting", "host": "devbox", "path": "/home/me/code/sorting",
           "path_display": "~/code/sorting", "branch": "main", "commit": "4d05f32"}


def view_screen(width: int, running: bool = False, readable: bool = True) -> list:
    """`autohelix view --once` of the run above: finished, or while iteration 4 runs;
    `readable=False`: its history can't be read."""
    from autohelix.run_table import Saved, view_lines

    saved = Saved(CONFIG, (ROWS[:4] if running else ROWS) if readable else None, FILES)
    return view_lines({**PROJECT, "run": _run_payload(running)}, saved, width, now=T)


def dashboard_screen(width: int) -> list:
    """`autohelix dashboard --once`: the run above, a second one running, one archived."""
    from autohelix.run_table import dashboard_lines

    perf = _run_payload(True)
    perf["status"].update(max_time_seconds=3600, segment_started_at=T - 372)  # `run --time 1h`
    perf = {**perf, "id": "20261115-1530-perf-m2xq", "name": "perf", "group": "speed",
            "title": "Cut the memory use of sort_list.", "config_file": "autohelix/perf.yaml",
            "why": "try a lower memory bound", "landing": {"commits": 2, "fast_forward": True}}
    old = {**_run_payload(False), "id": "20261110-0900-old-a1b2", "archived": True}
    return dashboard_lines({**PROJECT, "runs": [_run_payload(False), perf, old]}, width, now=T)


@pytest.fixture(autouse=True)
def _version(monkeypatch):
    monkeypatch.setattr(terminal, "version", lambda: "0.3.0")


SCREENS = {
    **{f"view-{w}": (lambda w=w: view_screen(w)) for w in WIDTHS},
    **{f"view-live-{w}": (lambda w=w: view_screen(w, running=True)) for w in WIDTHS},
    "view-unreadable-80": lambda: view_screen(80, readable=False),
    **{f"dashboard-{w}": (lambda w=w: dashboard_screen(w)) for w in WIDTHS},
    **{f"setup-{w}": (lambda w=w: setup_screen(w)) for w in WIDTHS},
    **{f"setup-template-{w}": (lambda w=w: setup_screen(w, picked=1)) for w in WIDTHS},
    "setup-no-git-80": lambda: setup_problem_screen(80),
    **{f"run-{w}": (lambda w=w: run_screen(w)) for w in WIDTHS},
    **{f"run-live-{w}": (lambda w=w: live_screen(w)) for w in WIDTHS},
    **{f"resume-{w}": (lambda w=w: resume_screen(w)) for w in WIDTHS},
    "run-piped": lambda: run_screen(100, show_mark=False),
}


def _text(lines) -> str:
    return "".join(line.plain.rstrip() + "\n" for line in lines)


@pytest.mark.parametrize("name", sorted(SCREENS))
def test_snapshot(name):
    text = _text(SCREENS[name]())
    path = SNAPSHOTS / f"{name}.txt"
    if os.environ.get("AUTOHELIX_UPDATE_SNAPSHOTS"):
        SNAPSHOTS.mkdir(exist_ok=True)
        path.write_text(text)
    assert path.is_file(), f"{path} is missing: run with AUTOHELIX_UPDATE_SNAPSHOTS=1"
    assert text == path.read_text(), f"{name} changed: run with AUTOHELIX_UPDATE_SNAPSHOTS=1 and review the diff"


@pytest.mark.parametrize("name", sorted(SCREENS))
def test_screens_fit_their_width(name):
    width = int(name.rsplit("-", 1)[-1]) if name[-1].isdigit() else 100
    assert all(line.cell_len <= width for line in SCREENS[name]())


def test_an_unreadable_history_shows_the_outcome_only():
    """Totals and landing would be counted from no rows, so the end block stops after
    the outcome; the warning is `terminal.warning`'s, like every warning."""
    lines = view_screen(80, readable=False)
    [warning] = [line for line in lines if "history can't be read" in line.plain]
    assert warning.plain.startswith("  ! ") and [span.style for span in warning.spans] == ["yellow"]
    assert warning.spans[0].start == 2 and warning.spans[0].end == 4
    assert lines[-1].plain.split() == ["Done", "iteration", "budget", "reached", "(7)"]


def test_view_says_what_waits_for_a_manual_landing():
    """Like `run`'s end block: the commits on the run's branch, why, and `autohelix land`."""
    from autohelix.run_table import Saved, view_lines

    run = {**_run_payload(False), "land": "manual", "landing": {"commits": 3, "fast_forward": True}}
    text = _text(view_lines({**PROJECT, "run": run}, Saved(CONFIG, ROWS, []), 120, now=T))
    assert "  unlanded   3 commits on autohelix/20261115-1402-sorting-krp3 (manual landing)\n" in text
    assert "  next       autohelix resume krp3 · autohelix land krp3\n" in text
