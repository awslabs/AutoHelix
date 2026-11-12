"""Run lifecycle commands: run (always new), resume, archive, delete; ids and the copies sweep."""

import json
import os
import socket
import time

import pytest
from click.testing import CliRunner

from autohelix.cli import main
from autohelix.harness import Harness
from autohelix.history import History
from autohelix.runs import (PROJECT_RECORD, RunLock, RunPaths, copies_base, git, project_key, short_id,
                            sweep_copies)
from tests.test_runs import configure, project  # noqa: F401 - fixture


def cli(project, *args):
    return CliRunner().invoke(main, [*args, "-p", str(project)])


def started(project, *args):
    """`autohelix run` once; returns the new run's paths."""
    result = cli(project, "run", "-n", "1", "--no-report", *args)
    assert result.exit_code == 0, result.output
    return RunPaths(project)


def iterations(paths):
    return [r.iteration for r in History(paths.project, paths.run_id).load()]


def test_run_always_starts_a_new_run_and_points_at_the_previous_one(project):  # noqa: F811
    first = started(project)
    result = cli(project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    second = RunPaths(project)
    assert second.run_id != first.run_id
    short = short_id(first.run_id)
    assert f"Previous run for this config: {short} (`autohelix resume {short}`" in result.output
    assert iterations(second) == [0, 1]


def test_run_warns_about_unlanded_commits_of_the_previous_run(project):  # noqa: F811
    first = started(project, "--land", "manual")
    result = cli(project, "run", "-n", "1", "--no-report")
    short = short_id(first.run_id)
    assert result.exit_code == 0, result.output
    assert f"{short} has 1 unlanded commit;" in result.output
    assert f"`autohelix archive {short}`" in result.output


def test_run_warns_but_starts_beside_a_live_run(project):  # noqa: F811
    first = started(project)
    with RunLock(first.lock):  # another terminal is running it
        result = cli(project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert f"{short_id(first.run_id)} is also running for this config" in result.output
    assert RunPaths(project).run_id != first.run_id


def test_resume_continues_the_most_recently_active_run(project):  # noqa: F811
    first = started(project)
    second = started(project)
    result = cli(project, "resume", short_id(first.run_id), "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert f"Resuming {short_id(first.run_id)}: 1 iteration" in result.output
    assert iterations(first) == [0, 1, 2]
    # first is now the most recently active, and what `current` points at.
    result = cli(project, "resume", "-n", "2", "--no-report")
    assert result.exit_code == 0, result.output
    assert iterations(first) == [0, 1, 2, 3, 4] and iterations(second) == [0, 1]
    assert RunPaths(project).run_id == first.run_id


def test_resume_until_is_a_total_and_n_with_until_is_refused(project):  # noqa: F811
    paths = started(project)
    for _ in range(2):  # repeatable
        result = cli(project, "resume", "--until", "3", "--no-report")
        assert result.exit_code == 0, result.output
    assert iterations(paths) == [0, 1, 2, 3]
    result = cli(project, "resume", "-n", "1", "--until", "5")
    assert result.exit_code == 2 and "not both" in result.output


def test_resume_with_nothing_to_resume_exits_3(project):  # noqa: F811
    result = cli(project, "resume")
    assert result.exit_code == 3
    assert "No run to resume for autohelix.yaml; start one with `autohelix run`." in result.output


def test_bare_resume_skips_archived_runs_and_prefers_a_baseline(project):  # noqa: F811
    older = started(project)
    archived = started(project)
    assert cli(project, "archive", short_id(archived.run_id)).exit_code == 0
    Harness(project, new=True).close()  # the most recent run never got going
    result = cli(project, "resume", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert f"Resuming {short_id(older.run_id)}" in result.output


def test_resuming_an_archived_run_by_id_brings_it_back(project):  # noqa: F811
    paths = started(project)
    assert cli(project, "archive", short_id(paths.run_id)).exit_code == 0
    assert paths.read_identity()["archived"] is True
    listed = json.loads(cli(project, "dashboard", "--json").output)["runs"]
    assert [r["archived"] for r in listed] == [True]
    assert "1 archived (not listed" in cli(project, "dashboard", "--once").output
    result = cli(project, "resume", short_id(paths.run_id), "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert "archived" not in paths.read_identity()


def test_archive_undo_and_archive_refuses_a_live_run(project):  # noqa: F811
    paths = started(project)
    with RunLock(paths.lock):
        result = cli(project, "archive", short_id(paths.run_id))
    assert result.exit_code == 1 and "is running" in result.output
    assert cli(project, "archive", short_id(paths.run_id)).exit_code == 0
    assert cli(project, "archive", "--undo", short_id(paths.run_id)).exit_code == 0
    assert "archived" not in paths.read_identity()


def test_an_abandoned_run_reads_as_archived(project):  # noqa: F811
    paths = started(project)
    data = paths.read_identity()
    data["lifecycle"] = "abandoned"  # written before archive existed
    paths.identity.write_text(json.dumps(data))
    assert cli(project, "resume").exit_code == 3
    assert json.loads(cli(project, "dashboard", "--json").output)["runs"][0]["archived"] is True


def test_delete_refuses_unlanded_commits_unless_forced_and_repoints_current(project):  # noqa: F811
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
    assert RunPaths(project).run_id == first.run_id
    assert cli(project, "delete", short_id(first.run_id)).exit_code == 0  # landed: no --force needed
    assert RunPaths(project).run_id is None


def test_ids_sort_by_start_and_commands_accept_the_suffix(project, tmp_path, monkeypatch):  # noqa: F811
    paths = started(project, "--name", "perf")
    assert paths.run_id.startswith(time.strftime("%Y%m%d-")) and paths.run_id.split("-")[2] == "perf"
    result = cli(project, "view", short_id(paths.run_id), "--json")
    assert result.exit_code == 0 and json.loads(result.output)["run"]["id"] == paths.run_id
    result = cli(project, "view", "perf", "--json")
    assert result.exit_code == 1 and "names aren't ids" in result.output
    (tmp_path / "bench").mkdir()
    monkeypatch.chdir(tmp_path)
    result = cli(project, "view", "bench", "--json")
    assert "bench is a folder: use -p bench" in result.output


def test_a_run_refuses_another_config(project):  # noqa: F811
    paths = started(project)
    other = project / "other.yaml"
    other.write_text((project / "autohelix.yaml").read_text())
    git(project, "add", ".")
    git(project, "commit", "-qm", "other config")
    with pytest.raises(RuntimeError, match="was started with autohelix.yaml, not other.yaml; a run keeps its config"):
        Harness(project, run_id=paths.run_id, config_file=other)  # the API only: the CLI has no -c


def test_the_time_budget_is_checked_before_an_iteration(project):  # noqa: F811
    configure(project, budget={"time": "1s"})
    paths = started(project)
    records = [json.loads(line) for line in paths.history.read_text().splitlines()]
    for record in records:
        if record["iteration"]:
            record["calls"][0]["usage"]["wall_clock_ms"] = 5000
    paths.history.write_text("".join(json.dumps(r) + "\n" for r in records))
    result = cli(project, "resume", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert "time budget reached" in result.output and iterations(paths) == [0, 1]


def test_a_stop_condition_pauses_the_run(project):  # noqa: F811
    configure(project, stop=["test -f STOP && echo asked to stop"])
    paths = started(project)
    (project / "STOP").write_text("")
    git(project, "add", "STOP")
    git(project, "commit", "-qm", "stop")
    result = cli(project, "resume", "-n", "2", "--no-report")
    assert result.exit_code == 0, result.output
    assert iterations(paths) == [0, 1, 2]  # one more iteration, then the condition stops it
    (project / "STOP").unlink()
    git(project, "commit", "-qam", "go on")
    assert cli(project, "resume", "-n", "1", "--no-report").exit_code == 0
    assert iterations(paths) == [0, 1, 2, 3]


def test_the_sweep_removes_leftover_copies(project, tmp_path):  # noqa: F811
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


def test_a_refused_resume_keeps_the_archived_flag(project):  # noqa: F811
    paths = started(project)
    cli(project, "archive", short_id(paths.run_id))
    git(project, "update-ref", "-d", paths.ref)  # resume will refuse: nothing to continue from
    result = cli(project, "resume", short_id(paths.run_id))
    assert result.exit_code == 1 and "has no ref left to resume" in result.output
    assert paths.read_identity()["archived"] is True


def test_bare_resume_refuses_when_its_pick_is_live(project):  # noqa: F811
    started(project)
    latest = started(project)
    with RunLock(latest.lock):
        result = cli(project, "resume")
    assert result.exit_code == 1 and f"Run {short_id(latest.run_id)} is running" in result.output


def test_an_ambiguous_suffix_and_a_torn_run(project):  # noqa: F811
    paths = started(project)
    twin = paths.runs / f"20200101-0000-{short_id(paths.run_id)}"  # e.g. copied from another clone
    twin.mkdir()
    result = cli(project, "view", short_id(paths.run_id), "--json")
    assert result.exit_code == 1 and "matches several runs" in result.output
    # A run folder without run.json (a crash while creating it) can still be deleted.
    assert cli(project, "delete", twin.name).exit_code == 1
    assert cli(project, "delete", "--force", twin.name).exit_code == 0 and not twin.exists()


def test_delete_refuses_a_live_run(project):  # noqa: F811
    paths = started(project)
    with RunLock(paths.lock):
        result = cli(project, "delete", "--force", short_id(paths.run_id))
    assert result.exit_code == 1 and "is running" in result.output and paths.run_dir.exists()


def test_the_time_budget_counts_prior_time_longer_than_the_uptime(project, monkeypatch):  # noqa: F811
    configure(project, budget={"time": "1h"})
    paths = started(project)
    records = [json.loads(line) for line in paths.history.read_text().splitlines()]
    records[-1]["calls"][0]["usage"]["wall_clock_ms"] = 2 * 3600 * 1000
    paths.history.write_text("".join(json.dumps(r) + "\n" for r in records))
    monkeypatch.setattr(time, "monotonic", lambda: 60.0)  # booted a minute ago
    result = cli(project, "resume", "-n", "1", "--no-report")
    assert "time budget reached" in result.output and iterations(paths) == [0, 1]
