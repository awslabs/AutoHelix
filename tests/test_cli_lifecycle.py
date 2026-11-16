"""Run lifecycle commands: run (always new), resume, archive, delete; ids and the copies sweep."""

import json
import os
import socket
import time

import pytest
from click.testing import CliRunner

from autohelix.cli import main
from autohelix.harness import Budget, Harness
from autohelix.history import History, record_seconds
from autohelix.runs import (PROJECT_RECORD, RunLock, RunPaths, copies_base, git, project_key, project_runs, short_id,
                            sweep_copies)
from tests.test_runs import configure
from tests.helpers import latest_run


def cli(project, *args):
    return CliRunner().invoke(main, [*args, "-p", str(project)])


def started(project, *args):
    """`autohelix run` once; returns the new run's paths."""
    result = cli(project, "run", "-n", "1", "--no-report", *args)
    assert result.exit_code == 0, result.output
    return latest_run(project)


def iterations(paths):
    return [r.iteration for r in History(paths.project, paths.run_id).load()]


def test_run_always_starts_a_new_run_and_points_at_the_previous_one(project):
    first = started(project)
    result = cli(project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    second = latest_run(project)
    assert second.run_id != first.run_id
    short = short_id(first.run_id)
    assert f"Previous run for this config: {short} (`autohelix resume {short}`" in result.output
    assert iterations(second) == [0, 1]


def test_run_warns_about_unlanded_commits_of_the_previous_run(project):
    first = started(project, "--land", "manual")
    result = cli(project, "run", "-n", "1", "--no-report")
    short = short_id(first.run_id)
    assert result.exit_code == 0, result.output
    assert f"{short} has 1 unlanded commit;" in result.output
    assert f"`autohelix archive {short}`" in result.output


def test_run_warns_but_starts_beside_a_live_run(project):
    first = started(project)
    with RunLock(first.lock):  # another terminal is running it
        result = cli(project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert f"{short_id(first.run_id)} is also running for this config" in result.output
    assert latest_run(project).run_id != first.run_id


def test_resume_continues_the_most_recently_active_run(project):
    first = started(project, "--land", "manual")  # auto: second's landing would put main past first's tip
    second = started(project, "--land", "manual")
    result = cli(project, "resume", short_id(first.run_id), "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert f"Resuming {short_id(first.run_id)}: 1 iteration" in result.output
    assert iterations(first) == [0, 1, 2]
    # first is now the most recently active, and what `current` points at.
    result = cli(project, "resume", "-n", "2", "--no-report")
    assert result.exit_code == 0, result.output
    assert iterations(first) == [0, 1, 2, 3, 4] and iterations(second) == [0, 1]
    assert latest_run(project).run_id == first.run_id


def test_resume_until_is_a_total_and_n_with_until_is_refused(project):
    paths = started(project)
    for _ in range(2):  # repeatable
        result = cli(project, "resume", "--until", "3", "--no-report")
        assert result.exit_code == 0, result.output
    assert iterations(paths) == [0, 1, 2, 3]
    result = cli(project, "resume", "-n", "1", "--until", "5")
    assert result.exit_code == 2 and "not both" in result.output


def test_resume_with_nothing_to_resume_exits_3(project):
    result = cli(project, "resume")
    assert result.exit_code == 3
    assert "No run to resume for autohelix.yaml; start one with `autohelix run`." in result.output


def test_bare_resume_skips_archived_runs_and_prefers_a_baseline(project):
    older = started(project, "--land", "manual")
    archived = started(project, "--land", "manual")
    assert cli(project, "archive", short_id(archived.run_id)).exit_code == 0
    Harness(project, new=True).close()  # the most recent run never got going
    result = cli(project, "resume", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert f"Resuming {short_id(older.run_id)}" in result.output


def test_resuming_an_archived_run_by_id_brings_it_back(project):
    paths = started(project)
    assert cli(project, "archive", short_id(paths.run_id)).exit_code == 0
    assert paths.read_identity()["archived"] is True
    listed = json.loads(cli(project, "dashboard", "--json").output)["runs"]
    assert [r["archived"] for r in listed] == [True]
    assert "1 archived (not listed" in cli(project, "dashboard", "--once").output
    result = cli(project, "resume", short_id(paths.run_id), "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert "archived" not in paths.read_identity()


def test_archive_undo_and_archive_refuses_a_live_run(project):
    paths = started(project)
    with RunLock(paths.lock):
        result = cli(project, "archive", short_id(paths.run_id))
    assert result.exit_code == 1 and "is running" in result.output
    assert cli(project, "archive", short_id(paths.run_id)).exit_code == 0
    assert cli(project, "archive", "--undo", short_id(paths.run_id)).exit_code == 0
    assert "archived" not in paths.read_identity()


def test_an_abandoned_run_reads_as_archived(project):
    paths = started(project)
    data = paths.read_identity()
    data["lifecycle"] = "abandoned"  # written before archive existed
    paths.identity.write_text(json.dumps(data))
    assert cli(project, "resume").exit_code == 3
    assert json.loads(cli(project, "dashboard", "--json").output)["runs"][0]["archived"] is True


def test_delete_refuses_unlanded_commits_unless_forced(project):
    first = started(project)
    second = started(project, "--land", "manual")
    short = short_id(second.run_id)
    result = cli(project, "delete", short)
    assert result.exit_code == 1
    assert f"Run {short} has 1 unlanded commit on autohelix/{second.run_id}" in result.output
    result = cli(project, "delete", "--force", short)
    assert result.exit_code == 0, result.output
    assert not second.run_dir.exists()
    assert git(project, "rev-parse", "--verify", second.ref, check=False).returncode != 0
    assert [p.run_id for p, _ in project_runs(project)] == [first.run_id]
    view = cli(project, "view", "--json")  # the run without an id is now the remaining one
    assert json.loads(view.stdout)["run"]["id"] == first.run_id
    assert cli(project, "delete", short_id(first.run_id)).exit_code == 0  # landed: no --force needed
    assert project_runs(project) == []
    assert not (RunPaths(project).project_internal / "current").exists()


def test_ids_sort_by_start_and_commands_accept_the_suffix(project, tmp_path, monkeypatch):
    (project / "autohelix").mkdir()
    git(project, "mv", "autohelix.yaml", "autohelix/perf.yaml")
    git(project, "commit", "-qm", "named config")
    paths = started(project, "perf")
    assert paths.run_id.startswith(time.strftime("%Y%m%d-")) and paths.run_id.split("-")[2] == "perf"
    result = cli(project, "view", short_id(paths.run_id), "--json")
    assert result.exit_code == 0 and json.loads(result.stdout)["run"]["id"] == paths.run_id
    result = cli(project, "view", "perf", "--json")
    assert result.exit_code == 1 and "names aren't ids" in result.output
    (tmp_path / "bench").mkdir()
    monkeypatch.chdir(tmp_path)
    result = cli(project, "view", "bench", "--json")
    assert "bench is a folder: use -p bench" in result.output


def test_a_run_refuses_another_config(project):
    paths = started(project)
    other = project / "other.yaml"
    other.write_text((project / "autohelix.yaml").read_text())
    git(project, "add", ".")
    git(project, "commit", "-qm", "other config")
    with pytest.raises(RuntimeError, match="was started with autohelix.yaml, not other.yaml; a run keeps its config"):
        Harness(project, run_id=paths.run_id, config_file=other)  # the API only: the CLI has no -c


def test_a_stop_condition_pauses_the_run(project):
    configure(project, stop=['test -f "$AUTOHELIX_PROJECT/STOP" && echo asked to stop'])
    paths = started(project)
    (project / "STOP").write_text("")  # untracked: stop commands run in a copy of the run's tip
    result = cli(project, "resume", "-n", "2", "--no-report")
    assert result.exit_code == 0, result.output
    assert iterations(paths) == [0, 1, 2]  # one more iteration, then the condition stops it
    (project / "STOP").unlink()
    result = cli(project, "resume", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert "stopped because asked to stop; it stops again" in " ".join(result.output.split())
    assert iterations(paths) == [0, 1, 2, 3]


def test_the_sweep_removes_leftover_copies(project, tmp_path):
    harness = Harness(project)  # records the project in its copies folder
    paths = harness.paths
    harness.close()
    leftover = paths.copies / "iter-3-agent"  # a SIGKILLed segment
    leftover.mkdir(parents=True)
    base = copies_base()
    gone, recent = base / "gone-00000000", base / "recent-00000000"
    for folder, path in ((gone, tmp_path / "deleted"), (recent, tmp_path / "unmounted")):
        (folder / "k3f9" / "iter-1-agent").mkdir(parents=True)
        (folder / PROJECT_RECORD).write_text(json.dumps({"path": str(path), "host": socket.gethostname()}))
    old = time.time() - 2 * 86400
    os.utime(gone / PROJECT_RECORD, (old, old))
    with RunLock(paths.lock):  # live: left alone
        sweep_copies(project)
    assert leftover.exists()
    sweep_copies(project)
    assert not leftover.exists()
    assert not gone.exists()
    assert recent.exists()  # its checkout may come back
    assert (base / project_key(project.resolve()) / PROJECT_RECORD).exists()


def test_a_refused_resume_keeps_the_archived_flag(project):
    paths = started(project)
    cli(project, "archive", short_id(paths.run_id))
    git(project, "update-ref", "-d", paths.ref)  # resume will refuse: nothing to continue from
    result = cli(project, "resume", short_id(paths.run_id))
    assert result.exit_code == 1 and "has no ref left to resume" in result.output
    assert paths.read_identity()["archived"] is True


def test_bare_resume_refuses_when_its_pick_is_live(project):
    started(project)
    latest = started(project)
    with RunLock(latest.lock):
        result = cli(project, "resume")
    assert result.exit_code == 1 and f"Run {short_id(latest.run_id)} is running" in result.output


def test_an_ambiguous_suffix_and_a_torn_run(project):
    paths = started(project)
    twin = paths.runs / f"20200101-0000-{short_id(paths.run_id)}"  # e.g. copied from another clone
    twin.mkdir()
    result = cli(project, "view", short_id(paths.run_id), "--json")
    assert result.exit_code == 1 and "matches several runs" in result.output
    # A run folder without run.json (a crash while creating it) can still be deleted.
    assert cli(project, "delete", twin.name).exit_code == 1
    assert cli(project, "delete", "--force", twin.name).exit_code == 0 and not twin.exists()


def test_delete_refuses_a_live_run(project):
    paths = started(project)
    with RunLock(paths.lock):
        result = cli(project, "delete", "--force", short_id(paths.run_id))
    assert result.exit_code == 1 and "is running" in result.output and paths.run_dir.exists()


# --- budgets: command-line flags, per segment ----------------------------------------


def status(paths):
    return json.loads(paths.status.read_text())


def begun(project, *args):
    """`autohelix run` with these budget flags only; returns the new run's paths."""
    result = cli(project, "run", "--no-report", *args)
    assert result.exit_code == 0, result.output
    return latest_run(project)


def test_budget_flags_are_validated(project):
    for args, message in ((["-n", "2", "--until", "3"], "not both"), (["--cost", "0"], "--cost"),
                          (["--cost", "-1"], "--cost"), (["--time", "soon"], "not a duration"),
                          (["--time", "0"], "not a duration"), (["-n", "0"], "-n")):
        result = cli(project, "run", *args)
        assert result.exit_code == 2 and message in result.output, (args, result.output)
    assert not latest_run(project).run_id  # refused before a run exists


def test_without_a_budget_flag_one_iteration(project):
    """A bare `run` is one checked attempt; loops are typed."""
    result = cli(project, "run", "--no-report")
    assert result.exit_code == 0, result.output
    paths = latest_run(project)
    assert "Budget     1 iteration (default)" in result.output
    assert iterations(paths) == [0, 1] and status(paths)["max_iterations"] == 1
    result = cli(project, "resume", "--no-report")
    assert "Budget     1 more iteration (default, to 2)" in result.output
    assert iterations(paths) == [0, 1, 2]
    assert "iteration budget reached (2)" in result.output


def test_until_on_run_and_resume(project):
    paths = begun(project, "--until", "2")
    assert iterations(paths) == [0, 1, 2]
    for _ in range(2):  # a total: the second one runs nothing
        result = cli(project, "resume", "--until", "3", "--no-report")
        assert result.exit_code == 0, result.output
    assert iterations(paths) == [0, 1, 2, 3] and status(paths)["max_iterations"] == 3


def test_cost_alone_has_no_iteration_cap_and_counts_this_segment(project):
    """The mock charges $0.10 an iteration: `--cost 0.35` runs four (past the default
    one), and a resume gets its whole --cost again."""
    paths = begun(project, "--cost", "0.35")
    assert iterations(paths) == [0, 1, 2, 3, 4]
    assert status(paths)["max_iterations"] is None
    assert status(paths)["max_cost_usd"] == pytest.approx(0.35)
    assert status(paths)["stop_reason"]["detail"] == "cost budget reached ($0.35 this segment)"
    result = cli(project, "resume", "--cost", "0.15", "--no-report")
    assert result.exit_code == 0, result.output
    assert "Budget     no iteration cap · $0.15 more" in result.output
    assert iterations(paths) == [0, 1, 2, 3, 4, 5, 6]  # $0.40 before doesn't count
    assert status(paths)["max_cost_usd"] == pytest.approx(0.55)  # spent before + --cost


def test_time_counts_this_segment_only(project):
    """Two hours recorded before don't stop a `--time 1h` resume; status records the
    segment's own cap (not a run total) and when the segment started."""
    paths = started(project)
    records = [json.loads(line) for line in paths.history.read_text().splitlines()]
    records[-1]["calls"][0]["usage"]["wall_clock_ms"] = 2 * 3600 * 1000
    paths.history.write_text("".join(json.dumps(r) + "\n" for r in records))
    before = sum(record_seconds(r) for r in History(project, paths.run_id).load())
    clock = time.time()
    result = cli(project, "resume", "-n", "1", "--time", "1h", "--no-report")
    assert result.exit_code == 0, result.output
    assert "Budget     1 more iteration (to 2) · 1h" in result.output
    assert iterations(paths) == [0, 1, 2]
    assert status(paths)["max_time_seconds"] == 3600 and before > 7200
    assert clock <= status(paths)["segment_started_at"] <= status(paths)["started_at"]


def test_time_counts_the_baseline_and_is_checked_before_an_iteration(project, monkeypatch):
    """A clock that jumps 10s at the baseline and at each iteration: with --time 15s,
    the baseline and one iteration fit, and no second iteration starts."""
    jump = [0.0]
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + jump[0])
    harness = Harness(project, new=True)
    for name in ("_capture_baseline", "run_iteration"):
        def later(*args, _original=getattr(harness, name)):
            result = _original(*args)
            jump[0] += 10
            return result
        monkeypatch.setattr(harness, name, later)
    harness.run(Budget(time_seconds=15), report=False)
    assert iterations(harness.paths) == [0, 1]
    assert status(harness.paths)["stop_reason"]["detail"] == "time budget reached (15s this segment)"


@pytest.mark.parametrize("state", ["interrupted", "crashed"])
def test_resume_after_an_interrupted_segment_says_what_was_left(project, state):
    paths = begun(project, "-n", "1")
    data = status(paths)  # as if `run -n 9` had stopped after iteration 1 (a dead PID: crashed)
    del data["stop_reason"]  # it didn't end for its own reason
    data.update(state="running" if state == "crashed" else state, max_iterations=9, pid=2 ** 22 + 12345,
                host=socket.gethostname())
    paths.status.write_text(json.dumps(data))
    result = cli(project, "resume", "--no-report")
    assert result.exit_code == 0, result.output
    assert "1 more (default); the interrupted segment had 8 left: -n 8" in result.output
    assert iterations(paths) == [0, 1, 2]


def test_no_interrupted_note_with_a_budget_flag_or_after_a_finished_segment(project):
    paths = begun(project, "-n", "2")
    result = cli(project, "resume", "--no-report")
    assert "interrupted segment" not in result.output  # finished: nothing was left
    data = status(paths)
    data.update(state="interrupted", max_iterations=20)
    paths.status.write_text(json.dumps(data))
    result = cli(project, "resume", "-n", "1", "--no-report")
    assert result.exit_code == 0 and "interrupted segment" not in result.output


def test_non_finite_budgets_are_usage_errors(project):
    for args in (["--cost", "inf"], ["--cost", "1e400"], ["--cost", "nan"]):
        result = cli(project, "run", *args)
        assert result.exit_code == 2 and "positive finite number" in result.output, (args, result.output)
    assert not latest_run(project).run_id


def test_cost_alone_with_an_unknown_cost_stops_before_the_next_iteration(project):
    """With --cost the only bound, a charge it can't see could make it run forever."""
    configure(project, agent={"backend": "mock", "settings": {
        "change_file": "code.py", "change_content": "# iteration {iteration}"}})  # no cost reported
    paths = begun(project, "--cost", "1")
    assert iterations(paths) == [0, 1]
    assert status(paths)["stop_reason"] == {
        "kind": "budget", "budget": "cost",
        "detail": "cost unknown: --cost can't bound this segment (add -n or --time)"}
    paths = begun(project, "--cost", "1", "-n", "2")  # another bound: it goes on, counting known costs
    assert iterations(paths) == [0, 1, 2]


def test_until_already_reached_runs_nothing(project):
    paths = begun(project, "-n", "2")
    before = paths.status.read_bytes(), sorted(p.name for p in paths.run_dir.rglob("*"))
    result = cli(project, "resume", "--until", "2")
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "Already at iteration 2; --until 2 runs nothing."
    assert (paths.status.read_bytes(), sorted(p.name for p in paths.run_dir.rglob("*"))) == before


def test_no_interrupted_note_after_a_stop(project):
    paths = begun(project, "-n", "1")
    data = status(paths)
    data.update(state="interrupted", max_iterations=9,
                stop_reason={"kind": "condition", "detail": "stop condition met at iteration 1: done"})
    paths.status.write_text(json.dumps(data))
    result = cli(project, "resume", "--no-report")
    assert result.exit_code == 0 and "interrupted segment" not in result.output


# --- the segment log ----------------------------------------------------------------


def segments(paths):
    from autohelix.run_status import read_segments

    return read_segments(paths.segments)


def test_each_segment_that_started_is_in_the_segment_log(project):
    paths = begun(project)
    result = cli(project, "resume", "--time", "1h", "-n", "2", "--no-report")
    assert result.exit_code == 0, result.output
    first, second = segments(paths)
    assert (first["segment"], second["segment"]) == (1, 2)
    assert first["budget"] == {"iterations": None, "until": None, "cost_usd": None, "time_seconds": None,
                               "default": True}
    assert (first["start_iteration"], first["iterations"], first["last_iteration"], first["state"]) == (
        0, 2, 1, "finished")  # the baseline and iteration 1
    assert first["stop_reason"]["detail"] == "iteration budget reached (1)"
    assert second["budget"] == {"iterations": 2, "until": None, "cost_usd": None, "time_seconds": 3600,
                                "default": False}
    assert (second["start_iteration"], second["iterations"], second["last_iteration"]) == (2, 2, 3)
    assert not first["open"] and not second["open"] and first["error"] is None
    assert first["started_at"] <= first["ended_at"] <= second["started_at"] <= second["ended_at"]
    assert second["started_at"] == status(paths)["segment_started_at"]
    assert second["pid"] == os.getpid() and second["host"] == socket.gethostname()
    # Append-only: a start and an end record each.
    events = [json.loads(line)["event"] for line in paths.segments.read_text().splitlines()]
    assert events == ["start", "end", "start", "end"]
    # --until already reached: no segment, nothing written.
    assert cli(project, "resume", "--until", "3").exit_code == 0
    assert len(segments(paths)) == 2


def test_an_interrupted_segment_ends_in_the_log(project, monkeypatch):
    harness = Harness(project, new=True)

    def interrupted(iteration):
        raise KeyboardInterrupt

    monkeypatch.setattr(harness, "run_iteration", interrupted)
    harness.run(Budget(iterations=5), report=False)
    [line] = segments(harness.paths)
    assert (line["state"], line["stop_reason"], line["error"]) == ("interrupted", None, None)
    assert (line["iterations"], line["last_iteration"]) == (1, 0)  # only the baseline
    assert line["budget"]["iterations"] == 5 and line["ended_at"] >= line["started_at"]


def test_a_failed_segment_records_its_error(project, monkeypatch):
    harness = Harness(project, new=True)

    def broken(iteration):
        raise RuntimeError("disk\nfull")

    monkeypatch.setattr(harness, "run_iteration", broken)
    with pytest.raises(RuntimeError):
        harness.run(Budget(iterations=2), report=False)
    [line] = segments(harness.paths)
    assert (line["state"], line["stop_reason"], line["error"]) == ("failed", None, "RuntimeError: disk full")


def test_ctrl_c_while_landing_keeps_the_stop_reason(project, monkeypatch):
    harness = Harness(project, new=True)

    def land():
        raise KeyboardInterrupt

    monkeypatch.setattr(harness, "_land", land)
    with pytest.raises(KeyboardInterrupt):
        harness.run(Budget(iterations=1), report=False)
    [line] = segments(harness.paths)
    assert line["state"] == "interrupted"
    assert line["stop_reason"]["detail"] == "iteration budget reached (1)"
    assert line["iterations"] == 2 and line["error"] is None


def test_ctrl_c_right_after_the_start_record_still_ends_it(project, monkeypatch):
    harness = Harness(project, new=True)
    original = harness.segment_log.start

    def start(**fields):
        original(**fields)
        raise KeyboardInterrupt

    monkeypatch.setattr(harness.segment_log, "start", start)
    with pytest.raises(KeyboardInterrupt):
        harness.run(Budget(iterations=1), report=False)
    [line] = segments(harness.paths)
    assert not line["open"] and line["state"] == "interrupted" and line["iterations"] == 0


def test_the_segment_log_survives_damage_and_reports_open_segments(tmp_path):
    from autohelix.run_status import SegmentLog, read_segments

    path = tmp_path / "segments.jsonl"
    first = SegmentLog(path)
    first.start(segment=first.next_number(), started_at=1.0, budget={"iterations": 3},
                start_iteration=0)  # its process died
    with path.open("ab") as stream:
        stream.write(b"not json\n\xff\xfe{\n")  # junk and undecodable bytes
    log = SegmentLog(path)
    log.start(segment=log.next_number(), started_at=2.0, budget={"iterations": 1}, start_iteration=4)
    log.end(state="finished", stop_reason={"detail": "iteration budget reached (4)"}, last_iteration=4,
            iterations=1)
    crashed, done = read_segments(path)
    assert (crashed["segment"], crashed["open"], crashed["ended_at"], crashed["state"]) == (1, True, None, None)
    assert (done["segment"], done["open"], done["started_at"], done["iterations"], done["state"]) == (
        2, False, 2.0, 1, "finished")
    assert path.read_bytes().count(b"\xff\xfe{") == 1  # nothing earlier rewritten
    assert sorted(p.name for p in tmp_path.iterdir()) == ["segments.jsonl"]  # no temporary files


def test_n_help_says_more_only_on_resume(project):
    assert "Run up to N iterations" in cli(project, "run", "--help").output
    assert "Run up to N more iterations" in cli(project, "resume", "--help").output
