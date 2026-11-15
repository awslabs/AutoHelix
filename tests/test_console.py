"""The run's console output (`terminal`): header, column header and one row per
iteration, the end block (outcome, metrics, totals, what landed, next commands, files),
and errors in the end block. Whole screens: test_snapshots.py."""

import subprocess

import pytest
import yaml
from click.testing import CliRunner

from autohelix import terminal
from autohelix.cli import main
from autohelix.config import Config
from autohelix.display import clean
from autohelix.harness import Budget
from autohelix.terminal import Row, Table, budget_text
from autohelix.runs import RunPaths, short_id


def _git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


CONFIG = {
    "goal": "Make sort_list faster in sort.py.\nMore detail below the title.",
    "agent": {"backend": "claude", "model": "opus", "timeout": "30m"},
    "gates": ["pytest -q", {"ask": "keeps the public API?"}, {"metric": "speed", "regress": "5%"}],
    "observe": [{"name": "bench", "command": "python bench.py", "metrics": {"speed": "higher"}},
                {"name": "review", "review": "Review the diff."},
                {"name": "progress", "review": "Count missing items.", "metrics": {"missing": "lower"}}],
    "scope": {"editable": ["sort.py"]},
}


# --- header and rows -----------------------------------------------------------


def _header(config, budget, width=100):
    title = terminal.title("run 0mws", "sorting", "12:00", version="0.3.0")
    return [line.plain for line in terminal.header(title, terminal.run_fields(config, budget_text(budget, 0)),
                                                   width, show_mark=False)]


def test_header_lines():
    lines = _header(Config.from_dict(CONFIG), Budget(iterations=20, cost_usd=10, time_seconds=7200))
    assert lines[:-1] == [
        "  autohelix  v0.3.0 · run 0mws · sorting · 12:00",
        "  Goal     Make sort_list faster in sort.py.",
        "  Agent    claude (opus)",
        "  Gates    pytest -q · ask: keeps the public API? · speed regress 5%",
        "  Observe  bench (speed ↑) · review · progress (missing ↓)",
        "  Scope    editable: sort.py",
        "  Budget   20 iterations · $10.00 · 2h · 30m each",
    ]
    assert set(lines[-1].strip()) == {"─"}


def test_header_omits_empty_lines():
    lines = _header(Config.from_dict({"goal": "g"}), Budget(until=1))
    assert lines[1:-1] == ["  Goal     g", "  Agent    claude", "  Budget   1 iteration"]


def test_the_mark_is_beside_the_header():
    title = terminal.title("run abcd", version="dev")
    lines = terminal.header(title, [("Goal", "g")], 80, show_mark=True)
    assert len(lines) == 7 and lines[0].plain.endswith("autohelix  dev · run abcd")
    assert lines[1].plain[2:2 + terminal.MARK_WIDTH] == terminal.mark()[1].plain


@pytest.mark.parametrize("budget, done, text", [
    (Budget(), 0, "1 iteration (default)"),
    (Budget(), 5, "1 more iteration (default, to 6)"),
    (Budget(iterations=3, cost_usd=5, time_seconds=3600), 5, "3 more iterations (to 8) · $5.00 more · 1h"),
    (Budget(until=8), 5, "3 more iterations (to 8)"),
    (Budget(until=4), 5, "0 more iterations (to 4)"),
    (Budget(cost_usd=5), 5, "no iteration cap · $5.00 more"),
    (Budget(time_seconds=5400), 0, "no iteration cap · 1h30m"),
    (Budget(cost_usd=0.015), 0, "no iteration cap · $0.015"),
    (Budget(cost_usd=1e-9), 0, "no iteration cap · $0.000000001"),
])
def test_header_budget_says_what_this_segment_may_do(budget, done, text):
    assert budget_text(budget, done) == text


@pytest.mark.parametrize("value, text", [(17481.5, "17,481.5"), (120, "120"), (33002.0, "33,002"), (46.9, "46.9"),
                                         (0.12345, "0.1235"), (1e-7, "1e-07"), (float("nan"), "nan")])
def test_numbers(value, text):
    assert terminal.number(value) == text


@pytest.mark.parametrize("value, reference, text", [(17481.5, 46.9, "373×"), (118, 120, "-2%"), (140, 120, "+17%"),
                                                    (100.4, 100, "+0.4%"), (5, 100, "0.05×"), (3, 0, ""), (2, 2, "")])
def test_changes(value, reference, text):
    assert terminal.change(value, reference) == text


def _rows(width=120, config=CONFIG):
    table = Table.of(Config.from_dict(config), width)
    table.add(Row(0, True, {"speed": 46.7, "missing": 5}))
    return table


def test_accepted_row_shows_values_and_changes_from_the_baseline():
    [row] = _rows().add(Row(3, True, {"speed": 61.0, "missing": 2}, 72, 0.42, commit="c", summary="Use sorted()"))
    assert row.plain.split() == ["✓", "3", "1m", "12s", "$0.42", "61", "+31%", "2", "-60%"]
    assert "Use sorted()" not in row.plain  # summaries are listed under `landed`


def test_rejected_row_dims_its_values_and_says_why():
    [row] = _rows(120).add(Row(4, False, {"speed": 40.0}, 72, 0.42, reason="gate failed: ask: keeps the public API?",
                               gates=[{"text": "pytest -q", "kind": "command", "passed": True},
                                      {"text": "ask: keeps the public API?", "kind": "command", "passed": False}, {}]))
    assert row.plain.rstrip().endswith("  ask: keeps the public API? failed") and " 40 " in row.plain
    assert "-14%" not in row.plain  # no change from the baseline: the reason says how it compares
    value = row.plain.index(" 40 ") + 1
    assert [span.style for span in row.spans if span.start <= value < span.end] == ["dim"]


def test_a_row_without_values_says_why_in_their_place():
    [row] = _rows(80).add(Row(2, False, {}, 5, 0.1, reason="gate failed: pytest -q",
                              gates=[{"text": "pytest -q", "kind": "command", "passed": False, "return_code": 1}, {}, {}]))
    assert row.plain.rstrip().endswith("pytest -q failed (exit 1)") and row.plain.index("pytest") < 40


def test_a_long_reason_goes_under_the_row_and_is_cut_to_the_width():
    lines = _rows(60, {"goal": "g", "observe": [{"name": "b", "command": "c", "metrics": {"speed": "higher"}}]}).add(
        Row(1, False, {"speed": 3.0}, reason="metric gate: " + "x" * 200))
    assert len(lines) == 2 and all(len(line.plain) <= 60 for line in lines)
    assert lines[1].plain.startswith(" " * terminal.INDENT + "xxx") and lines[1].plain.endswith("…")


def test_metric_gate_rejection_names_the_iteration_it_compares_with():
    table = _rows()
    table.add(Row(1, True, {"speed": 50.0}, commit="c"))
    [row] = table.add(Row(2, False, {"speed": 45.0}, reason="metric gate: …", gates=[
        {}, {}, {"text": "speed regress 5%", "kind": "metric", "passed": False, "value": 45.0, "reference": 50.0}]))
    assert row.plain.rstrip().endswith("speed 10.0% worse than iter 1 (5% allowed)")


def test_a_timed_out_agent_row():
    table = Table.of(Config.from_dict({"goal": "g", "agent": {"timeout": "30m"}}), 100)
    [_, row] = table.add(Row(4, False, reason="Iteration 4 agent failed: timed out after 30 minutes",
                             agent_error="timed out after 30 minutes"))
    assert row.plain.rstrip().endswith("agent timed out (30m)")


def test_baseline_line():
    lines = Table.of(Config.from_dict(CONFIG), 100).add(Row(0, True, {"speed": 46.7, "missing": 5}))
    assert lines[0].plain.split() == ["time", "cost", "speed", "↑", "missing", "↓"]
    assert lines[1].plain.split() == ["·", "base", "46.7", "5"]


def test_narrow_tables_drop_cost_then_changes():
    config = {"goal": "g", "observe": [{"name": "b", "command": "c", "metrics": {f"m{i}": "higher" for i in range(4)}}]}
    assert Table.of(Config.from_dict(config), 100).show_cost
    assert not Table.of(Config.from_dict(config), 90).show_cost and Table.of(Config.from_dict(config), 90).show_change
    assert not Table.of(Config.from_dict(config), 80).show_change


def test_clean_strips_control_characters():
    assert clean("a\x1b[2J\x1b]0;title\x07b\tc‮d\r\ne") == "ab cd e"


# --- the ending ----------------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.email", "t@t")
    _git(project, "config", "user.name", "T")
    (project / "hello.py").write_text('print("hello")\n')
    (project / ".gitignore").write_text(".autohelix/\n")
    (project / ".autohelix").mkdir()
    return project


def _configure(project, settings=None, **sections):
    config = {"goal": "Test the console", "agent": {"backend": "mock", "settings": {
        "change_file": "hello.py", "change_content": "# iteration {iteration}", **(settings or {})}}, **sections}
    (project / "autohelix.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "config")


def _run(project, *args):
    # The width is fixed: the ending's `next` line wraps by it (one command per line under 80+).
    result = CliRunner(env={"COLUMNS": "80"}).invoke(main, ["run", "--path", str(project), "--no-report", "-n", "1", *args])
    run_id = RunPaths(project).run_id
    return result, short_id(run_id), run_id


def _ending(output: str) -> list[str]:
    lines = [line.rstrip() for line in output.splitlines()]
    return lines[next(i for i, line in enumerate(lines) if line.startswith(("  Done", "  Stopped", "  Error"))):]


def test_ending_after_landing(project):
    _configure(project, observe=[{"name": "bench", "command": "echo '##autohelix[speed=3]'",
                                  "metrics": {"speed": "higher"}}], scope={"editable": ["hello.py"]})
    result, short, run_id = _run(project)
    assert result.exit_code == 0, result.output
    ending = _ending(result.output)
    assert ending[0] == "  Done      iteration budget reached (1)"
    assert ending[1] == "  speed     3"  # no accepted change measured it again
    assert ending[2].startswith("  run       1 iteration · 1 accepted")
    assert ending[3] == "  landed    1 commit on main"
    assert ending[4] == f"  next      autohelix view {short} · autohelix resume {short} -n 5"
    assert ending[5].startswith("  page      ") and ending[5].endswith(f"{run_id}/report.html")
    assert len(ending) == 6


def test_ending_when_nothing_landed(project):
    _configure(project, settings={"change_file": "elsewhere.txt"}, scope={"editable": ["hello.py"]})
    result, _, _ = _run(project)
    assert result.exit_code == 0, result.output
    assert "  landed    nothing (no accepted changes)" in _ending(result.output)


def test_ending_with_manual_landing(project):
    _configure(project, scope={"editable": ["hello.py"]})
    result, short, run_id = _run(project, "--land", "manual")
    assert result.exit_code == 0, result.output
    ending = _ending(result.output)
    assert ending[2] == f"  unlanded  1 commit on autohelix/{run_id} (manual landing)"
    assert ending[3:6] == [f"  next      autohelix view {short}", f"            autohelix resume {short} -n 5",
                           f"            autohelix land {short}"]  # one per line when they don't fit


def test_ending_when_landing_fails(project, monkeypatch):
    import autohelix.harness

    def refuse(*_):
        raise RuntimeError("main is checked out elsewhere")

    monkeypatch.setattr(autohelix.harness, "land", refuse)
    _configure(project, scope={"editable": ["hello.py"]})
    result, _, _ = _run(project)
    assert result.exit_code == 0, result.output
    assert "  landing   failed: main is checked out elsewhere" in _ending(result.output)


def test_an_error_heads_the_ending_then_landing_and_the_run_page(project):
    _configure(project, stop=["echo broken; exit 3"], scope={"editable": ["hello.py"]})
    result, _, _ = _run(project)
    assert result.exit_code == 1
    ending = _ending(result.output)
    message = "Stop command failed on the baseline: echo broken; exit 3 (expected exit 0 to stop or 1 to continue)"
    assert ending[0].startswith("  Error     Stop command failed on the baseline: echo broken;")
    totals = next(i for i, line in enumerate(ending) if line.startswith("  run "))
    assert " ".join(" ".join(ending[:totals]).split()) == f"Error {message}"  # wrapped, not cut
    assert [line.split()[0] for line in ending[totals:]] == ["run", "landed", "next", "page"]
    assert result.output.count("Error") == 1  # not repeated by the CLI


def test_agent_failures_show_in_the_row_and_three_in_a_row_stop_the_run(project):
    _configure(project, settings={"should_fail": True}, scope={"editable": ["hello.py"]})
    # The width is fixed: the ending's `next` line wraps by it (one command per line under 80+).
    result = CliRunner(env={"COLUMNS": "80"}).invoke(main, ["run", "--path", str(project), "--no-report", "-n", "5"])
    assert result.exit_code == 0, result.output
    rows = [line.rstrip() for line in result.output.splitlines() if line.startswith("  ✗ ")]
    assert len(rows) == 3 and all(row.endswith("agent failed: Mock failure") for row in rows)
    assert "  Stopped   the agent failed 3 times in a row: Mock failure" in result.output.splitlines()


def test_rejected_row_in_a_run(project):
    _configure(project, gates=['test "$AUTOHELIX_ITERATION" = 0'], scope={"editable": ["hello.py"]})
    result, _, _ = _run(project)
    rows = [line for line in result.output.splitlines() if line.startswith("  ✗ 1")]
    assert len(rows) == 1 and rows[0].rstrip().endswith('test "$AUTOHELIX_ITERATION" = 0 failed (exit 1)')
    assert result.output.count('test "$AUTOHELIX_ITERATION" = 0') == 2  # the header's Gates, the row
