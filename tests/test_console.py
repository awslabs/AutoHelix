"""The run's console output: header, baseline and two-line iteration rows, the plain
ending (stop reason, totals, what landed, next commands), and errors first."""

import subprocess

import pytest
import yaml
from click.testing import CliRunner

from autohelix.cli import main
from autohelix.config import Config
from autohelix.display import Transcript, clean, header_lines
from autohelix.harness import Harness
from autohelix.history import IterationResult
from autohelix.runs import RunPaths, short_id


def _git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


CONFIG = {
    "goal": "Make sort_list faster in sort.py.\nMore detail below the title.",
    "name": "sorting",
    "agent": {"backend": "claude", "model": "opus", "timeout": "30m"},
    "gates": ["pytest -q", {"ask": "keeps the public API?"}, {"metric": "speed", "regress": "5%"}],
    "observe": [{"name": "bench", "command": "python bench.py", "metrics": {"speed": "higher"}},
                {"name": "review", "review": "Review the diff."},
                {"name": "progress", "review": "Count missing items.", "metrics": {"missing": "lower"}}],
    "scope": {"editable": ["sort.py"]},
    "budget": {"iterations": 20, "cost": 10, "time": "2h"},
}


# --- header and rows -----------------------------------------------------------


def test_header_lines():
    lines = [line.plain for line in header_lines(Config.from_dict(CONFIG), "20261112-1200-sorting-0mws", "proj", 20)]
    assert lines == [
        "  AutoHelix · run 0mws · sorting",
        "  Goal     Make sort_list faster in sort.py.",
        "  Agent    claude (opus) · 30m per iteration",
        "  Gates    pytest -q · ask: keeps the public API? · speed regress 5%",
        "  Observe  bench (speed ↑) · review · progress (missing ↓)",
        "  Scope    editable: sort.py",
        "  Budget   20 iterations · $10 · 2h",
    ]


def test_header_omits_empty_lines_and_names_the_project():
    lines = [line.plain for line in header_lines(Config.from_dict({"goal": "g"}), "20261112-1200-abcd", "proj", 1)]
    assert lines == ["  AutoHelix · run abcd · proj", "  Goal     g", "  Agent    claude", "  Budget   1 iteration"]


def _record(iteration, accepted, metrics=None, **fields):
    record = IterationResult(iteration=iteration, accepted=accepted, metrics=metrics or {}, **fields)
    record.calls = [{"role": "agent", "usage": {"wall_clock_ms": 72_000, "cost_usd": 0.42}}]
    return record


def test_accepted_row_shows_changes_and_tracked_numbers():
    transcript = Transcript(Config.from_dict(CONFIG), 120)
    [row, summary] = transcript.row(_record(3, True, {"speed": 61.0, "missing": 2}, summary="Use sorted()"),
                                    {"speed": 46.7, "missing": 5}, {"speed": 52.2, "missing": 3})
    assert row.plain.startswith("  iter 3    1m 12s   $0.42  speed 61")
    assert "↑30.6%" in row.plain and row.plain.rstrip().endswith("✓ accepted  · missing 3 → 2")
    assert summary.plain == " " * 11 + "Use sorted()" and summary.style == "" and summary.spans[0].style == "dim"


def test_rejected_row_is_one_line_with_the_reason_and_the_summary_below():
    transcript = Transcript(Config.from_dict(CONFIG), 80)
    record = _record(2, False, reason="gate failed: pytest -q", summary="Tried radix\x1b[31m sort;\nfails\x07",
                     observe=[{"name": "progress", "ok": False, "error": "exited 2"}])
    row, summary, note = transcript.row(record, {"speed": 46.7}, {"speed": 52.2})
    assert row.plain.rstrip().endswith("✗ pytest -q") and "—" not in row.plain
    assert "speed" not in row.plain  # empty columns stay blank
    assert summary.plain == " " * 11 + "Tried radix sort; fails"
    assert note.plain == " " * 11 + "observe progress failed (exited 2)"


def test_long_reasons_are_cut_to_the_width():
    transcript = Transcript(Config.from_dict({"goal": "g"}), 60)
    [row] = transcript.row(_record(1, False, reason="metric gate: " + "x" * 200), {}, {})
    assert len(row.plain) <= 60 and row.plain.endswith("…") and "metric gate:" not in row.plain


def test_baseline_line():
    transcript = Transcript(Config.from_dict(CONFIG), 100)
    [line] = transcript.baseline(_record(0, True, {"speed": 46.7, "missing": 5}))
    assert line.plain.split() == ["baseline", "speed", "46.7", "·", "missing", "5"]


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
    result = CliRunner().invoke(main, ["run", "--path", str(project), "--no-report", "-n", "1", *args])
    run_id = RunPaths(project).run_id
    return result, short_id(run_id), run_id


def _ending(output: str) -> list[str]:
    lines = [line.rstrip() for line in output.splitlines()]
    return lines[next(i for i, line in enumerate(lines) if line.startswith("  Stopped:")):]


def test_ending_after_landing(project):
    _configure(project, observe=[{"name": "bench", "command": "echo '##autohelix[speed=3]'",
                                  "metrics": {"speed": "higher"}}], scope={"editable": ["hello.py"]})
    result, short, _ = _run(project)
    assert result.exit_code == 0, result.output
    ending = _ending(result.output)
    assert ending[0] == "  Stopped: iteration budget reached (1)"
    assert ending[1].startswith("  1 iteration · 1 accepted · ") and ending[1].endswith("best speed 3 (baseline)")
    assert ending[2] == "  Landed 1 commit on main."
    assert ending[3] == f"  autohelix view {short}  ·  autohelix resume {short}"
    assert ending[4].startswith("  Run page: .autohelix/runs/") and len(ending) == 5
    assert "report.html" not in "\n".join(ending[:4])


def test_ending_when_nothing_landed(project):
    _configure(project, settings={"change_file": "elsewhere.txt"}, scope={"editable": ["hello.py"]})
    result, _, _ = _run(project)
    assert result.exit_code == 0, result.output
    assert "  Nothing landed: no accepted changes." in _ending(result.output)
    assert "Landed" not in result.output


def test_ending_with_manual_landing(project):
    _configure(project, scope={"editable": ["hello.py"]})
    result, short, run_id = _run(project, "--land", "manual")
    assert result.exit_code == 0, result.output
    ending = _ending(result.output)
    assert ending[2] == f"  1 unlanded commit on autohelix/{run_id} (manual landing)."
    assert ending[3] == f"  autohelix view {short}  ·  autohelix resume {short}  ·  autohelix land {short}"


def test_an_error_comes_first_then_landing_and_the_run_page(project):
    _configure(project, settings={"should_fail": True}, scope={"editable": ["hello.py"]})
    result, _, _ = _run(project)
    assert result.exit_code == 1
    lines = [line.strip() for line in result.output.splitlines()]
    error = lines.index("Error: Iteration 1 agent failed: Mock failure")
    landing = lines.index("Nothing landed: no accepted changes.")
    page = next(i for i, line in enumerate(lines) if line.startswith("Run page:"))
    assert error < landing < page
    assert result.output.count("Iteration 1 agent failed") == 1  # not repeated by the CLI


def test_rejected_row_in_a_run(project):
    _configure(project, gates=['test "$AUTOHELIX_ITERATION" = 0'], scope={"editable": ["hello.py"]})
    result, _, _ = _run(project)
    rows = [line for line in result.output.splitlines() if line.startswith("  iter 1")]
    assert len(rows) == 1 and rows[0].rstrip().endswith('✗ test "$AUTOHELIX_ITERATION" = 0')
    assert not any(line.strip().startswith("✗") for line in result.output.splitlines())  # no duplicate line
