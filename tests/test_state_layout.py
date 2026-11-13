"""The run state layout: `RunPaths`, one folder per iteration with a derived
record.json, superseded attempts kept under `.internal/attempts/`, schema 2."""

import json
import os
import shutil

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
        other = paths.logs / "elsewhere" / "ask.log"  # one outside it stays as it is
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


# --- evaluation evidence: gates/<k>/, stop/<k>/, transcripts in their folders ----------

ITERATION = '"$AUTOHELIX_RUN_DIR/iterations/$(printf %03d "$AUTOHELIX_ITERATION")"'


def _configure(project, **values):
    from tests.test_runs import configure

    configure(project, **values)


def _listing(folder):
    return sorted(os.listdir(folder)) if folder.is_dir() else None


def _command_logs(record):
    return [call["log"] for call in record.calls if call["role"] == "command"]


def test_gate_folders_by_position_with_their_output_and_transcripts(project):
    _configure(project, gates=[
        "echo out1; echo err1 >&2",                       # 1: passes, both streams
        {"metric": "speed", "regress": "50%"},           # 2: runs nothing: no folder
        {"ask": "Done?"},                                 # 3: its transcript joins it
        'echo only-out; [ "$AUTOHELIX_ITERATION" = 0 ]',  # 4: fails at iteration 1
        "echo never",                                     # 5: never runs after 4 failed
    ], observe=[{"name": "bench", "command": "echo '##autohelix[speed=1]'", "metrics": {"speed": "higher"}}])
    harness = Harness(project, land_mode="manual")
    try:
        harness.run(max_iterations=1, report=False)
        paths = harness.paths
    finally:
        harness.close()
    baseline, first = History(project, paths.run_id).load()
    assert not first.accepted and first.reason.startswith("gate failed")
    assert _listing(paths.iteration_dir(0) / "gates") == ["1", "3", "4", "5"]  # the baseline runs every gate
    assert _listing(paths.iteration_dir(1) / "gates") == ["1", "3", "4"]
    one = paths.gate_dir(1, 1)
    assert _listing(one) == ["command.txt", "stderr.txt", "stdout.txt"]  # a passing gate's output is kept
    assert ((one / "command.txt").read_text(), (one / "stdout.txt").read_text(),
            (one / "stderr.txt").read_text()) == ("echo out1; echo err1 >&2\n", "out1\n", "err1\n")
    assert _listing(paths.gate_dir(1, 4)) == ["command.txt", "stdout.txt"]  # stderr only when not empty
    assert (paths.gate_dir(1, 4) / "stdout.txt").read_text() == "only-out\n"
    ask = paths.gate_dir(1, 3)
    assert "ask.log" in _listing(ask) and (ask / "ask.log").read_text().startswith("Mock judge")
    assert (ask / "command.txt").read_text().startswith("autohelix ask ")  # the rendered ask
    # The call's usage names the stored transcript, in history.jsonl and record.json alike.
    assert _command_logs(baseline) == ["iterations/000/gates/3/ask.log"]
    assert _command_logs(first) == ["iterations/001/gates/3/ask.log"]
    assert _record_file(paths, 1) == _history_lines(paths)[-1]
    assert not paths.logs.exists() or not [p for p in paths.logs.iterdir() if p.is_dir()]  # no logs/iter-N/


def test_stored_gate_output_replaces_what_commands_planted(project, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _configure(project, gates=[
        # A link planted at gates/ itself, and junk in its own logs/ folder.
        f'mkdir -p {ITERATION}; ln -s {elsewhere} {ITERATION}/gates; logs="$AUTOHELIX_LOG_DIR"; '
        'echo note > "$logs/notes.txt"; ln -s /etc/hosts "$logs/ask-7.log"; mkdir "$logs/review.log"; '
        'echo x > "$logs/ask-8.log"; ln "$logs/ask-8.log" "$logs/ask-9.log"; echo fake > "$logs/ask-6.log"',
        # Files planted in a later gate's folder, before it runs and while it runs.
        f'mkdir -p {ITERATION}/gates/3; echo planted > {ITERATION}/gates/3/planted.txt',
        f'echo three; echo own > {ITERATION}/gates/3/own.txt',
    ])
    harness = Harness(project, land_mode="manual")
    try:
        harness.run(max_iterations=1, report=False)
        paths = harness.paths
    finally:
        harness.close()
    assert list(elsewhere.iterdir()) == []
    for n in (0, 1):
        assert not (paths.iteration_dir(n) / "gates").is_symlink()
        # Only regular single-link transcripts move in (a fake one can't be told apart).
        assert _listing(paths.gate_dir(n, 1)) == ["ask-6.log", "command.txt", "stdout.txt"]
        assert _listing(paths.gate_dir(n, 3)) == ["command.txt", "stdout.txt"]


def test_a_transcript_that_was_not_stored_leaves_no_log(project):
    _configure(project, gates=['autohelix ask "Done?"; rm -f "$AUTOHELIX_LOG_DIR/ask.log"', {"ask": "Done?"}])
    harness = Harness(project, land_mode="manual")
    try:
        harness.run(max_iterations=1, report=False)
        paths = harness.paths
    finally:
        harness.close()
    record = History(project, paths.run_id).load()[-1]
    assert _command_logs(record) == [None, "iterations/001/gates/2/ask.log"]  # still counted


def test_stop_folders_by_position(project):
    _configure(project, stop=['echo s1; echo e1 >&2; exit 1',
                              '[ "$AUTOHELIX_ITERATION" = 0 ] && exit 1; autohelix ask "Stop?"'])
    harness = Harness(project, land_mode="manual")
    try:
        harness.run(max_iterations=3, report=False)
        paths = harness.paths
    finally:
        harness.close()
    baseline, first = History(project, paths.run_id).load()
    assert first.accepted and first.stop["command"].endswith('autohelix ask "Stop?"')
    for n in (0, 1):
        assert _listing(paths.iteration_dir(n) / "stop") == ["1", "2"]
        one = paths.stop_dir(n, 1)
        assert ((one / "command.txt").read_text(), (one / "stdout.txt").read_text(),
                (one / "stderr.txt").read_text()) == ("echo s1; echo e1 >&2; exit 1\n", "s1\n", "e1\n")
    assert "ask.log" not in _listing(paths.stop_dir(0, 2))
    assert (paths.stop_dir(1, 2) / "ask.log").read_text().startswith("Mock judge")
    assert _command_logs(first) == ["iterations/001/stop/2/ask.log"]
    assert _record_file(paths, 1) == _history_lines(paths)[-1]  # updated after the stop calls


def test_gate_and_stop_output_keeps_its_head_and_tail():
    from autohelix.history import OUTPUT_HEAD, OUTPUT_TAIL, cap_output

    assert cap_output("short") == b"short"
    assert cap_output("a" * 10 + "b" * 5 + "c" * 10, head=10, tail=10) == (
        b"a" * 10 + b"\n[autohelix: 5 bytes omitted]\n" + b"c" * 10)
    fits = "x" * (OUTPUT_HEAD + OUTPUT_TAIL)
    assert cap_output(fits) == fits.encode()
    capped = cap_output("H" * OUTPUT_HEAD + "x" * 1000 + "T" * OUTPUT_TAIL)
    assert capped == b"H" * OUTPUT_HEAD + b"\n[autohelix: 1000 bytes omitted]\n" + b"T" * OUTPUT_TAIL


def test_gate_output_is_capped_observe_stdout_is_not(project):
    big = "import sys; sys.{}.write('h' * 70000 + 'm' * 200000 + 't' * 1048576)"
    _configure(project, gates=[f'python3 -c "{big.format("stderr")}"'],
               observe=[{"name": "bench", "command": f'python3 -c "{big.format("stdout")}"'}])
    harness = Harness(project, land_mode="manual")
    try:
        harness._capture_baseline()
        paths = harness.paths
    finally:
        harness.close()
    from autohelix.history import OUTPUT_HEAD, OUTPUT_TAIL

    stderr = (paths.gate_dir(0, 1) / "stderr.txt").read_bytes()
    omitted = 70000 + 200000 + 1048576 - OUTPUT_HEAD - OUTPUT_TAIL
    assert stderr == (b"h" * OUTPUT_HEAD + f"\n[autohelix: {omitted} bytes omitted]\n".encode()
                      + (b"m" * 200000 + b"t" * 1048576)[-OUTPUT_TAIL:])
    assert (paths.gate_dir(0, 1) / "stdout.txt").read_bytes() == b""
    assert (paths.observe_entry(0, "bench") / "stdout.txt").stat().st_size == 70000 + 200000 + 1048576


def test_review_transcripts_join_the_observe_entry(project):
    _configure(project, observe=[{"name": "review", "review": "Is it tidy?"}])
    harness = Harness(project, land_mode="manual")
    try:
        harness.run(max_iterations=2, report=False)
        paths = harness.paths
    finally:
        harness.close()
    records = History(project, paths.run_id).load()
    assert [r.observe[0]["ok"] for r in records] == [True, True, True]
    for record in records:
        n = record.iteration
        assert _command_logs(record) == [f"iterations/{n:03d}/observe/review/review.log"]
        assert (paths.observe_entry(n, "review") / "review.log").is_file()


def test_ask_log_names_are_taken_exclusively(tmp_path, monkeypatch):
    import threading

    from autohelix.judge import _log_path

    logs = tmp_path / "logs"
    monkeypatch.setenv("AUTOHELIX_LOG_DIR", str(logs))
    assert _log_path("ask") == logs / "ask.log"
    (logs / "ask-2.log").symlink_to(tmp_path / "victim")  # planted: skipped, never written through
    assert _log_path("ask") == logs / "ask-3.log"
    assert _log_path("review") == logs / "review.log"
    found = []
    threads = [threading.Thread(target=lambda: found.append(_log_path("ask"))) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(found)) == 20 and not (tmp_path / "victim").exists()
    assert sorted(found) == sorted(logs / f"ask-{k}.log" for k in range(4, 24))
    monkeypatch.delenv("AUTOHELIX_LOG_DIR")  # outside the harness: a private temporary folder
    outside = _log_path("ask")
    assert outside.name == "ask.log" and outside.parent != logs and outside.parent.stat().st_mode & 0o077 == 0


def test_a_baseline_retry_keeps_its_transcripts_with_the_moved_attempt(project, tmp_path):
    from autohelix.safe_open import open_run_log

    marker = tmp_path / "second-attempt"
    _configure(project, gates=[{"ask": "Done?"}], observe=[
        {"name": "bench", "command": f'[ -e {marker} ] && echo "##autohelix[speed=1]"; true',
         "metrics": {"speed": "higher"}}])
    with pytest.raises(RuntimeError, match="Baseline setup error"):
        Harness(project).run(max_iterations=1, report=False)
    marker.touch()
    harness = Harness(project, land_mode="manual")  # resumes the run: a baseline retry
    try:
        harness._capture_baseline()
        paths = harness.paths
    finally:
        harness.close()
    baseline = History(project, paths.run_id).load()[-1]
    assert baseline.accepted
    assert _command_logs(baseline) == [".internal/attempts/000-1/gates/1/ask.log", "iterations/000/gates/1/ask.log"]
    for log in _command_logs(baseline):
        fd = open_run_log(paths, log)
        assert fd is not None
        os.close(fd)


def test_capped_output_is_cut_at_character_boundaries():
    from autohelix.history import cap_output

    capped = cap_output("é" * 20, head=5, tail=5)  # 2 bytes each: both cuts fall mid-character
    assert capped.decode("utf-8") == "éé\n[autohelix: 32 bytes omitted]\néé"
    assert cap_output("aé" + "x" * 10 + "€b", head=2, tail=2).decode() == "a\n[autohelix: 15 bytes omitted]\nb"


@pytest.mark.parametrize("planted", ["gates", "gates/2", "observe", "observe/bench", "stop", "stop/1"])
def test_a_planted_fifo_where_output_goes_is_replaced(project, planted):
    _configure(project, gates=[f'mkdir -p "$(dirname {ITERATION}/{planted})"; mkfifo {ITERATION}/{planted}',
                               "echo two"],
               observe=[{"name": "bench", "command": "echo bench"}], stop=["echo stop; exit 1"])
    harness = Harness(project, land_mode="manual")
    try:
        harness.run(max_iterations=1, report=False)
        paths = harness.paths
    finally:
        harness.close()
    assert [r.accepted for r in History(project, paths.run_id).load()] == [True, True]
    for n in (0, 1):
        for folder in (paths.gate_dir(n, 1), paths.gate_dir(n, 2), paths.observe_entry(n, "bench"),
                       paths.stop_dir(n, 1)):
            assert folder.is_dir() and not folder.is_symlink() and "stdout.txt" in _listing(folder)


def test_a_storage_failure_never_loses_a_gate_or_stop_outcome(project, monkeypatch):
    def broken(*args):
        raise OSError("disk full")

    monkeypatch.setattr(History, "stage_command_output", staticmethod(broken))
    _configure(project, gates=["true", '[ "$AUTOHELIX_ITERATION" = 0 ]'],
               stop=['[ "$AUTOHELIX_ITERATION" = 2 ]'])
    harness = Harness(project, land_mode="manual")
    try:
        harness.run(max_iterations=1, report=False)
        paths = harness.paths
    finally:
        harness.close()
    baseline, first = History(project, paths.run_id).load()
    assert [g["passed"] for g in baseline.gates] == [True, True]
    assert not first.accepted and [g["passed"] for g in first.gates] == [True, False]
    assert not (paths.iteration_dir(1) / "gates").exists() or _listing(paths.iteration_dir(1) / "gates") == []


# --- phase 3: the agent's copy mirrors the run layout, iteration rejected.patch --------


def _wrap_agent(harness, act):
    """Calls `act(worktree, iteration)` before each agent call (the mock agent runs after)."""
    original = harness.run_agent

    def run_agent(worktree, prompt, iteration):
        act(worktree, prompt, iteration)
        return original(worktree, prompt, iteration)

    harness.run_agent = run_agent


def _tree(folder):
    """Every path below `folder` (relative, sorted); symlinks listed, not followed."""
    out = []
    for top, dirs, files in os.walk(folder):
        out += [os.path.relpath(os.path.join(top, name), folder) for name in dirs + files]
    return sorted(out)


def _fill_iteration(paths, n, gates=(True, False)):
    """An iteration's folder as a run leaves it, beside the record History wrote."""
    folder = paths.iteration_dir(n)
    for k, passed in enumerate(gates, start=1):
        paths.gate_dir(n, k).mkdir(parents=True)
        (paths.gate_dir(n, k) / "stdout.txt").write_text(f"gate {k} of {n}\n")
    (paths.observe_entry(n, "bench") / "out").mkdir(parents=True)
    (paths.observe_entry(n, "bench") / "stdout.txt").write_text(f"bench {n}\n")
    (paths.observe_entry(n, "bench") / "out" / "plot.txt").write_text("plot\n")
    paths.stop_dir(n, 1).mkdir(parents=True)
    (paths.stop_dir(n, 1) / "stdout.txt").write_text("stop\n")
    for name in ("agent.log", "prompt.md", "rejected.patch"):
        (folder / name).write_text(name)
    (paths.notes_versions / str(n)).mkdir(parents=True, exist_ok=True)
    os.symlink(os.path.relpath(paths.notes_versions / str(n), folder), folder / "notes")


def _seeded_history(paths, count=9, accepted=(0,)):
    history = History(paths.project, paths.run_id)
    for n in range(count):
        history.append(IterationResult(
            iteration=n, accepted=n in accepted,
            gates=[{"text": "a", "kind": "command", "passed": True},
                   {"text": "b", "kind": "command", "passed": n in accepted},
                   {"text": "m", "kind": "metric", "passed": False if n not in accepted else None}],
            observe=[{"name": "bench", "ok": True}]))
        _fill_iteration(paths, n)
    return history


def _fake_copy(tmp_path, name="copy"):
    from types import SimpleNamespace

    (tmp_path / name).mkdir()
    return SimpleNamespace(working_dir=tmp_path / name)


def test_an_agent_copy_gets_the_last_five_iterations_and_the_last_accepted(run_paths, tmp_path):
    from autohelix.iteration import prepare_state

    _seeded_history(run_paths)
    run_paths.attempt_dir(8, 1).mkdir(parents=True)  # a superseded attempt: never seeded
    (run_paths.attempt_dir(8, 1) / "record.json").write_text("{}")
    copy = _fake_copy(tmp_path)
    prepare_state(run_paths, copy, before=9)
    state = copy.working_dir / ".autohelix-iteration"
    assert _listing(state / "iterations") == ["000", "004", "005", "006", "007", "008"]
    rejected = state / "iterations" / "007"
    # Failing gates' folders only (gate 3 is a metric gate: no folder); no agent.log,
    # prompt.md, notes link or rejected.patch.
    assert _tree(rejected) == ["gates", "gates/2", "gates/2/stdout.txt", "observe", "observe/bench",
                               "observe/bench/out", "observe/bench/out/plot.txt", "observe/bench/stdout.txt",
                               "record.json", "stop", "stop/1", "stop/1/stdout.txt"]
    assert json.loads((rejected / "record.json").read_text()) == _record_file(run_paths, 7)
    assert (rejected / "gates" / "2" / "stdout.txt").read_text() == "gate 2 of 7\n"
    # The baseline, accepted, has no failing gate: no gates/ at all.
    assert "gates" not in _listing(state / "iterations" / "000")
    assert (state / "iterations" / "000" / "observe" / "bench" / "stdout.txt").read_text() == "bench 0\n"
    assert not (state / ".internal").exists() and not (state / "attempts").exists()
    assert "logs" not in _listing(state) and "observations" not in _listing(state)
    # `before`: the iteration being attempted (and later ones) are not reference.
    second = _fake_copy(tmp_path, "second")
    prepare_state(run_paths, second, before=3)
    assert _listing(second.working_dir / ".autohelix-iteration" / "iterations") == ["000", "001", "002"]


def test_the_last_accepted_is_the_last_record_with_its_number(run_paths, tmp_path):
    from autohelix.iteration import seeded_iterations

    history = _seeded_history(run_paths, count=8, accepted=(0, 1))
    history.append(IterationResult(iteration=1, accepted=False))  # a later record for 1 (rewritten)
    assert [n for n, _ in seeded_iterations(history.load())] == [0, 3, 4, 5, 6, 7]


@pytest.mark.parametrize("planted", ["runs", "iterations", "iteration", "observe", "entry", "gate", "stop",
                                     "record", "file", "hardlink"])
def test_seeding_never_follows_planted_links(run_paths, tmp_path, planted):
    from autohelix.iteration import prepare_state

    _seeded_history(run_paths, count=2)
    secret = tmp_path / "secret"
    (secret / "bench").mkdir(parents=True)
    (secret / "stdout.txt").write_text("SECRET\n")
    (secret / "bench" / "stdout.txt").write_text("SECRET\n")
    (secret / "record.json").write_text('"SECRET"')
    folder = run_paths.iteration_dir(1)

    def replace(path, target):
        shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
        path.symlink_to(target)

    if planted == "runs":  # an outer component of the run folder itself
        moved = tmp_path / "moved-runs"
        os.rename(run_paths.runs, moved)
        run_paths.runs.symlink_to(moved)
    elif planted == "iterations":
        os.rename(run_paths.iterations, secret / "iterations")
        run_paths.iterations.symlink_to(secret / "iterations")
    elif planted == "iteration":
        os.rename(folder, secret / "001")
        folder.symlink_to(secret / "001")
    elif planted == "observe":
        replace(run_paths.observe_dir(1), secret)
    elif planted == "entry":
        replace(run_paths.observe_entry(1, "bench"), secret / "bench")
    elif planted == "gate":
        replace(run_paths.gate_dir(1, 2), secret)
    elif planted == "stop":
        replace(run_paths.stop_dir(1, 1).parent, secret)
    elif planted == "record":
        replace(run_paths.record(1), secret / "record.json")
    elif planted == "file":
        replace(run_paths.observe_entry(1, "bench") / "stdout.txt", secret / "stdout.txt")
    else:
        (run_paths.observe_entry(1, "bench") / "stdout.txt").unlink()
        os.link(secret / "stdout.txt", run_paths.observe_entry(1, "bench") / "stdout.txt")
    copy = _fake_copy(tmp_path)
    prepare_state(run_paths, copy, before=2)
    state = copy.working_dir / ".autohelix-iteration"
    for path in state.rglob("*"):
        assert not path.is_symlink(), path
        if path.is_file():
            assert "SECRET" not in path.read_text(), path
    if planted in ("runs", "iterations"):
        assert not (state / "iterations").exists()
    elif planted == "iteration":
        assert _listing(state / "iterations") == ["000"]
    else:
        assert (state / "iterations" / "000" / "observe" / "bench" / "stdout.txt").read_text() == "bench 0\n"


def test_an_agent_copy_holds_a_bounded_number_of_iterations(project):
    """No quadratic growth: however long the run, a copy gets at most six iteration folders."""
    _configure(project, gates=['echo "gate of $AUTOHELIX_ITERATION"; [ "$AUTOHELIX_ITERATION" -lt 2 ]'],
               observe=[{"name": "bench", "command": 'echo "bench $AUTOHELIX_ITERATION"'}])
    seen = {}

    def act(worktree, prompt, iteration):
        state = worktree / ".autohelix-iteration"
        seen[iteration] = (_listing(state / "iterations"), prompt,
                           {n: _tree(state / "iterations" / n) for n in _listing(state / "iterations") or []})

    harness = Harness(project, land_mode="manual")
    _wrap_agent(harness, act)
    try:
        harness.run(max_iterations=9, report=False)
    finally:
        harness.close()
    assert seen[1][0] == ["000"]
    assert all(len(listing) <= 6 for listing, _, _ in seen.values())
    listing, prompt, trees = seen[9]
    assert listing == ["001", "004", "005", "006", "007", "008"]  # 1: the last accepted
    # A failed gate rejects before observe runs: its output, no observe/.
    assert trees["008"] == ["gates", "gates/1", "gates/1/command.txt", "gates/1/stdout.txt", "record.json"]
    assert trees["001"] == ["observe", "observe/bench", "observe/bench/stdout.txt", "record.json"]  # gate passed
    assert "  ✗ " in prompt and "(output: .autohelix-iteration/iterations/008/gates/1/)" in prompt
    assert "iterations/008/observe" not in prompt
    assert "(iter-1; its record and observations: .autohelix-iteration/iterations/001/)" in prompt


def test_a_rejected_attempt_keeps_its_patch_in_its_folder(project):
    _configure(project, gates=['[ "$AUTOHELIX_ITERATION" != 1 ]'])
    patches = {}

    def act(worktree, prompt, iteration):
        patch = worktree / ".autohelix-iteration" / "rejected.patch"
        patches[iteration] = patch.read_bytes() if patch.exists() else None

    harness = Harness(project, land_mode="manual")
    _wrap_agent(harness, act)
    try:
        harness.run(max_iterations=2, report=False)
        paths = harness.paths
    finally:
        harness.close()
    _, rejected, accepted = History(project, paths.run_id).load()
    assert not rejected.accepted and rejected.rejected_ref and accepted.accepted
    stored = paths.rejected_patch(1).read_bytes()
    assert stored == patches[2] and b"+# iteration 1" in stored  # the bytes the next copy got
    assert not paths.rejected_patch(0).exists() and not paths.rejected_patch(2).exists()


def test_no_iteration_patch_for_a_no_op(project):
    _configure(project, agent={"backend": "mock", "settings": {"should_fail": True}})
    harness = Harness(project, land_mode="manual")
    try:
        harness.run(max_iterations=1, report=False)  # fails before changing anything: no tree kept
        paths = harness.paths
    finally:
        harness.close()
    record = History(project, paths.run_id).load()[-1]
    assert not record.accepted and record.rejected_ref is None
    assert paths.record(1).is_file() and not os.path.lexists(paths.rejected_patch(1))


def test_no_iteration_patch_for_an_over_limit_change(project, monkeypatch):
    monkeypatch.setattr("autohelix.iteration.REJECTED_PATCH_LIMIT", 10)
    _configure(project, gates=['[ "$AUTOHELIX_ITERATION" = 0 ]'])
    harness = Harness(project, land_mode="manual")
    try:
        harness.run(max_iterations=1, report=False)
        paths = harness.paths
    finally:
        harness.close()
    record = History(project, paths.run_id).load()[-1]
    assert not record.accepted and record.rejected_ref  # the ref has it
    assert paths.record(1).is_file() and not os.path.lexists(paths.rejected_patch(1))


def test_the_iteration_patch_replaces_what_was_planted(project, tmp_path):
    _configure(project, gates=['[ "$AUTOHELIX_ITERATION" = 0 ]'])
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    harness = Harness(project, land_mode="manual")

    def act(worktree, prompt, iteration):  # e.g. the agent, by absolute path
        harness.paths.iteration_dir(iteration).mkdir(parents=True, exist_ok=True)
        harness.paths.rejected_patch(iteration).symlink_to(victim)

    _wrap_agent(harness, act)
    try:
        harness.run(max_iterations=1, report=False)
        paths = harness.paths
    finally:
        harness.close()
    assert victim.read_text() == "keep\n"
    patch = paths.rejected_patch(1)
    assert not patch.is_symlink() and b"+# iteration 1" in patch.read_bytes()


def test_a_patch_write_failure_never_masks_the_outcome(project, monkeypatch):
    from autohelix.iteration import IterationWorkspace

    def broken(self):
        raise OSError("disk full")

    monkeypatch.setattr(IterationWorkspace, "store_rejected_patch", broken)
    _configure(project, gates=['[ "$AUTOHELIX_ITERATION" = 0 ]'])
    harness = Harness(project, land_mode="manual")
    try:
        harness.run(max_iterations=1, report=False)
        paths = harness.paths
    finally:
        harness.close()
    record = History(project, paths.run_id).load()[-1]
    assert not record.accepted and record.rejected_ref and paths.record(1).is_file()


def test_a_planted_tree_too_deep_to_walk_is_skipped(run_paths, tmp_path, monkeypatch):
    import inspect
    import sys

    from autohelix.iteration import prepare_state

    _seeded_history(run_paths, count=2)
    fd = os.open(run_paths.observe_entry(1, "bench"), os.O_RDONLY)
    for _ in range(200):  # created relative to each level: no path-length limit
        os.mkdir("d", dir_fd=fd)
        child = os.open("d", os.O_RDONLY, dir_fd=fd)
        os.close(fd)
        fd = child
    os.close(fd)
    copy = _fake_copy(tmp_path)
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(len(inspect.stack()) + 120)
    try:
        prepare_state(run_paths, copy, before=2)
    finally:
        sys.setrecursionlimit(limit)
    iterations = copy.working_dir / ".autohelix-iteration" / "iterations"
    assert (iterations / "001" / "record.json").is_file() and not (iterations / "001" / "observe").exists()
    assert (iterations / "000" / "observe" / "bench" / "stdout.txt").read_text() == "bench 0\n"
