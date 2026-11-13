"""The run state layout: `RunPaths`, one folder per iteration with a derived
record.json, superseded attempts kept under `.internal/attempts/`, schema 2."""

import json
import os

import pytest

from autohelix.harness import Harness
from autohelix.history import History, IterationResult
from autohelix.runs import SCHEMA, RunPaths, write_json


def test_paths(tmp_path):
    paths = RunPaths(tmp_path, "20261112-0900-k3f9")
    run = paths.run_dir
    assert run == tmp_path.resolve() / ".autohelix" / "runs" / "20261112-0900-k3f9"
    assert paths.current == tmp_path.resolve() / ".autohelix" / ".internal" / "current"
    assert paths.lock == paths.project_internal / "locks" / "20261112-0900-k3f9.lock"
    assert paths.bin == paths.project_internal / "bin"
    assert (paths.report, paths.report_html, paths.notes) == (run / "report.md", run / "report.html", run / "notes")
    assert paths.status == run / ".internal" / "status.json"
    assert paths.copies_record == run / ".internal" / "copies"
    assert paths.report_info == run / ".internal" / "report.json"
    assert paths.usage(7) == run / ".internal" / "usage" / "007.jsonl"
    assert paths.iteration_dir(0) == run / "iterations" / "000"
    assert paths.iteration_dir(42) == run / "iterations" / "042"
    assert paths.iteration_dir(1000) == run / "iterations" / "1000"  # the width grows past 999
    assert paths.record(3) == run / "iterations" / "003" / "record.json"
    assert (paths.prompt(3).name, paths.agent_log(3).name, paths.iteration_notes(3).name) == (
        "prompt.md", "agent.log", "notes")
    assert paths.observe_entry(3, "bench") == run / "iterations" / "003" / "observe" / "bench"
    assert paths.gate_dir(3, 1) == run / "iterations" / "003" / "gates" / "1"  # 1-based, unpadded
    assert paths.stop_dir(3, 12) == run / "iterations" / "003" / "stop" / "12"
    assert paths.attempt_dir(3, 2) == run / ".internal" / "attempts" / "003-2"
    assert paths.notes_versions == run / ".internal" / "notes"


def test_a_schema_1_run_is_refused(run_paths):
    assert SCHEMA == 2 and run_paths.read_identity()["schema"] == 2
    write_json(run_paths.identity, {"schema": 1, "id": "test"})
    with pytest.raises(RuntimeError, match="Unsupported or inconsistent run identity"):
        run_paths.read_identity()


def _record_file(paths, iteration):
    return json.loads(paths.record(iteration).read_text())


def _history_lines(paths):
    return [json.loads(line) for line in paths.history.read_text().splitlines()]


def test_record_json_is_written_with_every_record(run_paths):
    history = History(run_paths.project, "test")
    history.append(IterationResult(iteration=0, accepted=True, metrics={"speed": 1.0}))
    history.append(IterationResult(iteration=1, accepted=True, metrics={"speed": 2.0}))
    lines = _history_lines(run_paths)
    assert [_record_file(run_paths, n) for n in (0, 1)] == lines
    assert run_paths.record(1).read_text().startswith('{\n  "iteration": 1,')  # pretty-printed
    # An update (stop calls after an accept) rewrites that iteration's record.json, not the others'.
    before = run_paths.record(0).stat().st_mtime_ns
    history.update(IterationResult(iteration=1, accepted=True, metrics={"speed": 2.0},
                                   stop={"command": "true", "reason": "done"}))
    assert _record_file(run_paths, 1) == _history_lines(run_paths)[1]
    assert _record_file(run_paths, 1)["stop"] == {"command": "true", "reason": "done"}
    assert run_paths.record(0).stat().st_mtime_ns == before
    assert not list(run_paths.iteration_dir(1).glob(".*.tmp"))


def test_record_json_is_never_written_through_a_planted_link(run_paths, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    run_paths.iterations.mkdir(parents=True)
    run_paths.iteration_dir(1).symlink_to(elsewhere)
    History(run_paths.project, "test").append(IterationResult(iteration=1, accepted=False))
    assert list(elsewhere.iterdir()) == []
    assert run_paths.iteration_dir(1).is_dir() and not run_paths.iteration_dir(1).is_symlink()
    assert _record_file(run_paths, 1)["iteration"] == 1


def test_supersede_moves_earlier_attempts_aside(run_paths):
    history = History(run_paths.project, "test")
    assert history.supersede(3) is None  # nothing there
    for attempt in ("first", "second"):
        run_paths.iteration_dir(3).mkdir(parents=True)
        run_paths.prompt(3).write_text(attempt)
        history.supersede(3)
    assert not run_paths.iteration_dir(3).exists()
    assert (run_paths.attempt_dir(3, 1) / "prompt.md").read_text() == "first"
    assert (run_paths.attempt_dir(3, 2) / "prompt.md").read_text() == "second"


def test_a_baseline_retry_points_kept_calls_at_the_moved_attempt(project):
    harness = Harness(project, land_mode="manual")
    try:
        paths = harness.paths
        harness.history.append(IterationResult(iteration=0, accepted=False, reason="baseline failed: x"))
        log = paths.iteration_dir(0) / "gates" / "1" / "ask.log"  # a judge log inside the attempt's folder
        log.parent.mkdir(parents=True)
        log.write_text("transcript\n")
        other = paths.command_logs(0) / "call-001-command" / "ask.log"  # one outside it stays as it is
        usage = paths.usage(0)
        usage.parent.mkdir(parents=True, exist_ok=True)
        usage.write_text(json.dumps({"id": "a", "cost_usd": 0.1, "log": str(log)}) + "\n"
                         + json.dumps({"id": "b", "cost_usd": 0.2, "log": str(other)}) + "\n"
                         + json.dumps({"id": "c", "log": "iterations/000/observe/review/review.log"}) + "\n")
        moved = harness.history.supersede(0)
        harness._start_command_usage(0, moved)
        assert moved == paths.attempt_dir(0, 1)
        kept = [json.loads(line) for line in usage.read_text().splitlines()]
        assert [k["id"] for k in kept] == ["a", "b", "c"]  # still counted toward the cost
        assert kept[0]["log"] == str(moved / "gates" / "1" / "ask.log")
        assert (moved / "gates" / "1" / "ask.log").read_text() == "transcript\n"
        assert kept[1]["log"] == str(other)
        assert kept[2]["log"] == ".internal/attempts/000-1/observe/review/review.log"  # relative stays relative
        # After an accepted baseline, the next attempt at a number starts from no calls.
        harness.history.update(IterationResult(iteration=0, accepted=True))
        harness._start_command_usage(0, harness.history.supersede(0))
        assert usage.read_text() == ""
    finally:
        harness.close()


def test_an_iteration_folder_holds_its_record_prompt_agent_log_and_notes(project):
    harness = Harness(project, land_mode="manual")
    try:
        harness.run(max_iterations=1, report=False)
        paths = harness.paths
        assert _record_file(paths, 0) == _history_lines(paths)[0]
        assert _record_file(paths, 1) == _history_lines(paths)[1]
        assert not paths.prompt(0).exists() and not paths.agent_log(0).exists()  # no agent at the baseline
        assert paths.prompt(1).read_text().strip() and paths.agent_log(1).is_file()
        record = History(project, paths.run_id).load()[-1]
        assert next(call for call in record.calls if call["role"] == "agent")["log"] == "iterations/001/agent.log"
        assert paths.iteration_notes(1).is_symlink() and paths.iteration_notes(1).resolve().parent == (
            paths.notes_versions.resolve())
        assert paths.status.is_file() and paths.copies_record.is_file()
        assert paths.report_html.is_file()
    finally:
        harness.close()


def test_supersede_never_reaches_through_a_planted_iterations_link(run_paths, tmp_path):
    victim = tmp_path / "victim"
    (victim / "002").mkdir(parents=True)
    (victim / "002" / "keep.txt").write_text("keep\n")
    run_paths.iterations.symlink_to(victim)
    assert History(run_paths.project, "test").supersede(2) is None
    assert (victim / "002" / "keep.txt").read_text() == "keep\n"
    assert not run_paths.iterations.exists()  # the planted link is gone, its target untouched


def test_a_moved_attempt_keeps_its_notes_link(run_paths):
    from autohelix.iteration import save_notes

    copy = type("C", (), {})()
    copy.working_dir = run_paths.project / "fake-copy"
    notes = copy.working_dir / ".autohelix-iteration" / "notes"
    notes.mkdir(parents=True)
    (notes / "iter-2.md").write_text("tried\n")
    save_notes(run_paths, copy, 2)
    moved = History(run_paths.project, "test").supersede(2)
    assert (moved / "notes").is_symlink() and os.readlink(moved / "notes") == "../../notes/2"
    assert (moved / "notes" / "iter-2.md").read_text() == "tried\n"
    # A planted notes entry (not a link to a notes version) is dropped, not followed.
    run_paths.iteration_dir(2).mkdir()
    (run_paths.iteration_dir(2) / "notes").symlink_to("/etc")
    moved = History(run_paths.project, "test").supersede(2)
    assert not os.path.lexists(moved / "notes")


@pytest.mark.parametrize("damage", ["missing", "stale"])
def test_ownership_recovery_heals_record_json(run_paths, damage):
    from autohelix.runs import repair_history

    history = History(run_paths.project, "test")
    history.append(IterationResult(iteration=0, accepted=True))
    history.append(IterationResult(iteration=1, accepted=True, metrics={"speed": 2.0}))
    if damage == "missing":  # killed after the history append, before record.json
        run_paths.record(1).unlink()
    else:  # killed after the post-stop update rewrote history, before record.json
        record = json.loads(run_paths.record(1).read_text())
        history.update(IterationResult(iteration=1, accepted=True, metrics={"speed": 2.0},
                                       stop={"command": "true", "reason": "done"}))
        run_paths.record(1).write_text(json.dumps(record))
    before = run_paths.record(0).stat().st_mtime_ns
    content = run_paths.history.read_bytes()
    repair_history(run_paths)
    assert run_paths.history.read_bytes() == content  # history itself untouched
    assert json.loads(run_paths.record(1).read_text()) == _history_lines(run_paths)[1]
    assert run_paths.record(0).stat().st_mtime_ns == before  # a correct one is not rewritten
