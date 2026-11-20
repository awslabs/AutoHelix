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
from tests.helpers import with_words

SNAPSHOTS = Path(__file__).parent / "snapshots"
NOW = datetime(2026, 11, 15, 14, 2)
WIDTHS = (80, 120)

CONFIG = Config.from_dict({
    "goal": "Make sort_list faster in sort.py.\nThe benchmark sorts 100k integers.",
    "agent": {"cli": "claude-code", "model": "sonnet", "timeout": "30m"},
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
        discarded=["autohelix/bench.py", "notes.txt"], discarded_frozen=["autohelix/bench.py"]),
    Row(6, True, {"speed": 57520.8, "mem_mb": 95}, 111, 0.31, commit="a6",
        summary="Avoid the defensive list copy; sort in place", gates=_gates(True, True)),
    Row(7, False, {}, 4, None, reason="Iteration 7 agent failed: claude exited 1: rate limited",
        agent_error="claude exited 1: rate limited"),
]


def _header(width: int, budget: Budget, done: int = 0, show_mark: bool = True) -> list:
    """`Harness._print_header`: piped (no mark), with the Steer field (no live row to show it under)."""
    title = terminal.title("run krp3", "sorting", terminal.clock(NOW), version="0.3.0")
    fields = terminal.run_fields(CONFIG, terminal.budget_text(budget, done))
    fields += [] if show_mark else [("Steer", terminal.steer_text("krp3", interrupt=False))]
    return terminal.header(title, fields, width, show_mark)


def _ending(table: Table, stop: dict, first: int = 1) -> list:
    """The end block as the harness assembles it (`Harness._print_ending`): auto landing
    of this segment's commits, the run's cost so far."""
    landed = [row for row in table.rows if row.iteration >= first and row.accepted and row.commit]
    return table.ending(terminal.outcome(stop, None, None), spent=terminal.money(sum(r.cost or 0 for r in table.rows)),
                        landing=terminal.landed(len(landed), "main"), commands=terminal.next_commands("krp3"),
                        files=FILES, folder=FOLDER)


FOLDER = ".autohelix/runs/20261115-1402-sorting-krp3/"
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
    from rich.text import Text

    return lines + [table.live(4, 72, "agent"), Text(), terminal.steer_line("krp3", width)]


def resume_screen(width: int) -> list:
    """`autohelix resume -n 2` after the 7 iterations above, ended by a stop condition."""
    table = Table.of(CONFIG, width)
    lines = _header(width, Budget(iterations=2), done=7)
    lines += table.earlier(ROWS, "krp3", NOW.replace(hour=15, minute=40))
    lines += table.add(Row(8, True, {"speed": 61004.2, "mem_mb": 94}, 98, 0.27, commit="a8",
                           summary="Use a radix sort for small integer keys", gates=_gates(True, True)))
    return lines + _ending(table, {"kind": "condition", "detail": "stop condition met at iteration 8: speed above 60,000"},
                           first=8)


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
            "config_file": "autohelix/sorting/config.yaml", "config_invalid": False,
            "goal": CONFIG.goal, "title": CONFIG.title, "status": status, "stop_requested": None,
            "accepted": 3, "group": None, "why": None, "archive_why": None, "import": None,
            "metrics": {"iteration": 6 if not running else 3, "values": [
                {"name": "speed", "value": 57520.8 if not running else 35310.7, "baseline": 46.9, "direction": "higher"},
                {"name": "mem_mb", "value": 95.0 if not running else 96.0, "baseline": 120.0, "direction": "lower"}]},
            "progress": {"iterations": len(rows) - 1, "max_iterations": 7,
                         "cost_usd": round(sum(r.cost or 0 for r in rows), 2),
                         "cost_incomplete": False, "seconds": sum(r.seconds or 0 for r in rows), "max_cost_usd": 10.0,
                         "max_seconds": None},
            "target_branch": "main", "landing": {"relation": "same", "commits": 0, "behind": 0, "fast_forward": True},
            "iterations": [_payload_row(row) for row in rows]}


PROJECT = {"schema": 1, "project": "sorting", "host": "devbox", "path": "/home/me/code/sorting",
           "path_display": "~/code/sorting", "branch": "main", "commit": "4d05f32"}


def view_screen(width: int, running: bool = False, readable: bool = True) -> list:
    """`autohelix view --once` of the run above: finished, or while iteration 4 runs;
    `readable=False`: its history can't be read."""
    from autohelix.run_table import Saved, view_lines

    saved = Saved(CONFIG, (ROWS[:4] if running else ROWS) if readable else None, FILES, FOLDER)
    return view_lines({**PROJECT, "run": with_words(_run_payload(running))}, saved, width, now=T, show_mark=True)


def dashboard_screen(width: int) -> list:
    """`autohelix view --once`: the run above, a second one running, one archived."""
    from autohelix.run_table import dashboard_lines

    perf = _run_payload(True)
    perf["status"].update(max_time_seconds=3600, segment_started_at=T - 372)  # `run --time 1h`
    perf = {**perf, "id": "20261115-1530-perf-m2xq", "name": "perf", "group": "speed",
            "title": "Cut the memory use of sort_list.", "config_file": "autohelix/perf/config.yaml",
            "why": "try a lower memory bound", "landing": {"relation": "ahead", "commits": 2, "behind": 0, "fast_forward": True}}
    old = {**_run_payload(False), "id": "20261110-0900-old-a1b2", "archived": True}
    return dashboard_lines({**PROJECT, "runs": [with_words(r) for r in [_run_payload(False), perf, old]]}, width, now=T)


def _menu(options: list[tuple[str, str]], width: int) -> list:
    """The arrow-key menu as drawn while choosing (`menu.choose`, no title): the options,
    the first one current, then `(q: quit)`."""
    import click
    from rich.text import Text

    from autohelix.menu import QUIT_HINT, option_line

    return [Text()] + [Text(click.unstyle(option_line(options, i, 0, width - 1))) for i in range(len(options))] + \
        [Text("  " + QUIT_HINT)]


def _question(data: dict, agents: list[str], configs: list[str], width: int) -> list:
    """The entry screen's one question as `cli._entry` asks it: Enter/q with one agent,
    the menu of agents (drawn while choosing) with several."""
    from rich.text import Text

    from autohelix.agents import CLI_NAMES
    from autohelix.cli import ENTRY_TITLE, entry_question

    if len(agents) == 1:
        return [Text(), Text(f"  {entry_question(agents[0])} Enter/q")]
    return [Text(), Text(f"  {ENTRY_TITLE}")] + _menu([(CLI_NAMES[a], "") for a in agents], width)[1:]


def _entry(data: dict, width: int, agents: list[str], configs: list[str], git: tuple | None = None) -> list:
    """Bare `autohelix`'s screen: the header, the runs, the question."""
    from autohelix.run_table import entry_header, entry_lines

    return entry_header("~/code/sorting", width, git) + entry_lines(data, width, now=T) + \
        _question(data, agents, configs, width)


def entry_screen(width: int) -> list:
    """Bare `autohelix` with runs and one agent: the dashboard's runs (the one running,
    one waiting to land, one interrupted, one whose process is gone with commits to land,
    the finished one; one archived), the command for each, and the one question."""
    finished = _run_payload(False)
    perf = {**_run_payload(True), "id": "20261115-1530-perf-m2xq", "name": "perf", "config_file": "autohelix/perf/config.yaml",
            "title": "Cut the memory use of sort_list."}
    manual = {**finished, "id": "20261114-1000-sorting-xax8", "last_activity": T - 7200, "land": "manual",
              "landing": {"relation": "ahead", "commits": 1, "behind": 0, "fast_forward": True}}
    stopped = {**_run_payload(False), "id": "20261113-1000-sorting-0mws", "last_activity": T - 86400 * 2,
               "status": {"state": "interrupted", "iteration": 7}}
    gone = {**_run_payload(False), "id": "20261112-1000-sorting-c9d1", "last_activity": T - 86400 * 3,
            "status": {"state": "crashed", "iteration": 7}, "landing": {"relation": "ahead", "commits": 2, "behind": 0, "fast_forward": True}}
    old = {**_run_payload(False), "id": "20261110-0900-old-a1b2", "archived": True}
    data = {**PROJECT, "runs": [with_words(r) for r in [finished, perf, manual, stopped, gone, old]]}
    return _entry(data, width, ["claude-code"], ["autohelix/sorting/config.yaml", "autohelix/perf/config.yaml"])


def entry_archived_screen(width: int) -> list:
    """Bare `autohelix` where every run is archived, two agents: counted, not "no runs",
    and the question is which agent."""
    data = {**PROJECT, "runs": [with_words({**_run_payload(False), "archived": True})]}
    return _entry(data, width, ["claude-code", "codex"], ["autohelix/sorting/config.yaml"])


def entry_config_screen(width: int) -> list:
    """Bare `autohelix` with a config and no runs, no commit yet: the Git field, then the
    one question."""
    git = ("No commits yet; a run starts from a commit.", "yellow")
    return _entry({**PROJECT, "runs": []}, width, ["claude-code"], ["autohelix/sorting/config.yaml"], git)


def entry_new_screen(width: int) -> list:
    """Bare `autohelix` with no runs and no configs: one Enter/q question."""
    return _entry({**PROJECT, "runs": []}, width, ["claude-code"], [])


def entry_no_git_screen(width: int) -> list:
    """Bare `autohelix` outside a git checkout (a real empty folder): nothing to start."""
    from autohelix.cli import _git_problem
    from autohelix.run_table import outside_lines

    with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, GIT_CEILING_DIRECTORIES=folder):
        problem, fix = _git_problem(Path(folder))
    return outside_lines("~/notes", problem, fix, width)


@pytest.fixture(autouse=True)
def _version(monkeypatch):
    monkeypatch.setattr(terminal, "version", lambda: "0.3.0")


SCREENS = {
    **{f"view-{w}": (lambda w=w: view_screen(w)) for w in WIDTHS},
    **{f"view-live-{w}": (lambda w=w: view_screen(w, running=True)) for w in WIDTHS},
    "view-unreadable-80": lambda: view_screen(80, readable=False),
    **{f"dashboard-{w}": (lambda w=w: dashboard_screen(w)) for w in WIDTHS},
    **{f"entry-{w}": (lambda w=w: entry_screen(w)) for w in WIDTHS},
    "entry-archived-80": lambda: entry_archived_screen(80),
    "entry-config-80": lambda: entry_config_screen(80),
    "entry-new-80": lambda: entry_new_screen(80),
    "entry-no-git-80": lambda: entry_no_git_screen(80),
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

    run = {**_run_payload(False), "land": "manual", "landing": {"relation": "ahead", "commits": 3, "behind": 0, "fast_forward": True}}
    text = _text(view_lines({**PROJECT, "run": with_words(run)}, Saved(CONFIG, ROWS, []), 120, now=T))
    assert "  unlanded   3 commits on autohelix/20261115-1402-sorting-krp3 (manual landing)\n" in text
    assert "  next       autohelix resume krp3 · autohelix land krp3\n" in text


def test_view_offers_git_merge_for_a_diverged_run():
    """A target that can't fast-forward: `diverged` (the one word), and `next` offers the
    merge that works instead of `autohelix land`, which would refuse."""
    from autohelix.run_table import Saved, view_lines

    run = {**_run_payload(False), "landing": {"relation": "diverged", "commits": 2, "behind": 1,
                                             "fast_forward": False}}
    text = _text(view_lines({**PROJECT, "run": with_words(run)}, Saved(CONFIG, ROWS, []), 140, now=T))
    assert "  unlanded   2 commits on autohelix/20261115-1402-sorting-krp3 (diverged from main)\n" in text
    assert "  next       autohelix resume krp3 · git merge autohelix/20261115-1402-sorting-krp3\n" in text
    assert "moved on" not in text and "autohelix land" not in text


def test_the_entry_screen_never_cuts_a_command():
    """A suggestion wider than the screen (a long run id, `git cherry-pick <tip>..`):
    the command goes on its own line, broken at spaces only, never cut."""
    from autohelix.run_table import entry_lines

    run_id = "20261117-2253-benchmark-optimization-sweep-k3f9"
    tip = "0123456789abcdef0123456789abcdef01234567"
    run = {**_run_payload(False), "id": run_id,
           "landing": {"relation": "diverged", "commits": 2, "behind": 1, "fast_forward": False}}
    for words, command in [(with_words(run), f"git merge autohelix/{run_id}"),
                           ({**run, "landing_words": {"cell": "2 diverged", "text": "", "note": "",
                                                       "commands": [f"git cherry-pick {tip}..autohelix/{run_id}"]}},
                            f"git cherry-pick {tip}..autohelix/{run_id}")]:
        lines = [line.plain for line in entry_lines({**PROJECT, "runs": [words]}, 80, now=T)]
        text = "\n".join(lines)
        assert "…" not in "".join(line for line in lines if "next:" in line or "autohelix/" in line)
        assert command.split()[-1] in text.split()  # the long word whole
        assert " ".join(" ".join(lines).split()).count(command) == 1
