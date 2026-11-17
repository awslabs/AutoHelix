"""Which run a command acts on, and which project (05-1, 06-2, 06-3, 06-4, 06-6, 06-8, 07-5, 12-3)."""

import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request

import pytest
import yaml
from click.testing import CliRunner

from autohelix.cli import main
from autohelix.judge import CONFIG_ENV
from autohelix.runs import (SCHEMA, NoRun, RunLock, RunPaths, default_run, find_project, git, project_runs,
                            short_id, write_json)
from tests.helpers import cli, latest_run


def add_run(project, run_id, *, created_at, archived=False):
    paths = RunPaths(project, run_id)
    paths.run_dir.mkdir(parents=True)
    write_json(paths.identity, {"schema": SCHEMA, "id": run_id, "created_at": created_at,
                                **({"archived": True} if archived else {})})
    return paths


# --- the rule ---------------------------------------------------------------------


def test_default_run_is_the_running_one_else_the_latest_not_archived(tmp_path):
    with pytest.raises(NoRun, match="No runs in"):
        default_run(tmp_path)
    old = add_run(tmp_path, "old-aaaa", created_at=1000)
    add_run(tmp_path, "new-bbbb", created_at=3000, archived=True)
    middle = add_run(tmp_path, "mid-cccc", created_at=2000)
    assert default_run(tmp_path)[::2] == (middle, "most recently active")
    with RunLock(old.lock):
        assert default_run(tmp_path)[::2] == (old, "running")
        with RunLock(middle.lock), pytest.raises(RuntimeError, match="Several runs are running: (aaaa, cccc|cccc, aaaa); name one"):
            default_run(tmp_path)


def test_a_probe_creates_no_lock_file(tmp_path):
    paths = add_run(tmp_path, "old-aaaa", created_at=1000)
    default_run(tmp_path)
    assert not paths.lock.exists() and not paths.locks.exists()


def test_the_project_is_found_from_a_subfolder_within_the_checkout(tmp_path):
    outer = tmp_path / "outer"
    project = outer / "repo"
    (project / "src" / "deep").mkdir(parents=True)
    git(project, "init", "-q")
    RunPaths(outer).runs.mkdir(parents=True)  # above the checkout: never taken
    assert find_project(project / "src" / "deep") == (project / "src" / "deep").resolve()
    RunPaths(project).runs.mkdir(parents=True)
    assert find_project(project / "src" / "deep") == project.resolve()
    assert find_project(project) == project.resolve()
    RunPaths(project / "src").runs.mkdir(parents=True)  # the nearest one wins (a subproject)
    assert find_project(project / "src" / "deep") == (project / "src").resolve()


# --- commands on a real project ----------------------------------------------------


@pytest.fixture
def folder_project(project):
    """The shared project with its config at autohelix/perf.yaml, plus autohelix/docs.yaml."""
    (project / "autohelix").mkdir()
    git(project, "mv", "autohelix.yaml", "autohelix/perf.yaml")
    shutil.copy(project / "autohelix" / "perf.yaml", project / "autohelix" / "docs.yaml")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "two configs")
    shutil.rmtree(project / ".autohelix")
    return project


def started(project, *args) -> RunPaths:
    result = cli(project, "run", *args, "-n", "1", "--no-report", "--land", "manual")
    assert result.exit_code == 0, result.output
    return latest_run(project)


def test_resume_takes_a_config_name_like_run(folder_project):
    perf = started(folder_project, "perf")
    docs = started(folder_project, "docs")
    result = cli(folder_project, "resume", "perf", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert f"Resuming {short_id(perf.run_id)}" in result.output
    assert len(perf.history.read_text().splitlines()) == 3 and len(docs.history.read_text().splitlines()) == 2
    # A bare resume with two configs names both ways.
    result = cli(folder_project, "resume")
    assert result.exit_code == 1 and "Name a run or a config: `autohelix resume <id|name>`." in result.output
    result = cli(folder_project, "resume", "nope")
    assert result.exit_code == 1 and "No run or config 'nope'" in result.output


def test_a_run_id_takes_precedence_over_a_config_of_that_name(folder_project):
    perf = started(folder_project, "perf")
    short = short_id(perf.run_id)
    (folder_project / "autohelix" / f"{short}.yaml").write_text((folder_project / "autohelix" / "docs.yaml").read_text())
    git(folder_project, "add", "-A")
    git(folder_project, "commit", "-qm", "a config named like the id")
    result = cli(folder_project, "resume", short, "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert f"Resuming {short}" in result.output


def test_land_and_group_are_stored_when_nothing_runs(folder_project):
    perf = started(folder_project, "perf", "--group", "g")
    before = perf.history.read_text()
    result = cli(folder_project, "resume", short_id(perf.run_id), "--until", "1", "--land", "auto", "--group", "")
    assert result.exit_code == 0, result.output
    assert f"Run {short_id(perf.run_id)}: landing set to auto, leaves group g; already at iteration 1" in result.output
    data = perf.read_identity()
    assert data["land"] == "auto" and "group" not in data
    assert perf.history.read_text() == before  # nothing ran
    result = cli(folder_project, "resume", short_id(perf.run_id), "--until", "1", "--land", "auto")
    assert "no change" in result.output


def test_unknown_ids_refuse_the_whole_command(folder_project):
    one, two = started(folder_project, "perf"), started(folder_project, "docs")
    for command in (["archive"], ["delete", "--force"], ["stop"]):
        result = cli(folder_project, *command, short_id(one.run_id), "zzzz", short_id(two.run_id))
        assert result.exit_code == 1 and "No run 'zzzz'" in result.output and "Nothing was done." in result.output
        assert not one.read_identity().get("archived") and two.identity.exists()
    # Known ids: each handled, a failure reported and the command exits 1 after the rest.
    result = cli(folder_project, "stop", short_id(one.run_id), short_id(two.run_id))
    assert result.exit_code == 1 and result.output.count("is not running") == 2
    result = cli(folder_project, "archive", short_id(one.run_id), short_id(two.run_id))
    assert result.exit_code == 0 and all(p.read_identity()["archived"] for p in (one, two))


def test_land_view_and_report_name_the_run_they_chose(folder_project, monkeypatch):
    from tests.test_live import _stop_after

    perf, docs = started(folder_project, "perf"), started(folder_project, "docs")
    _stop_after(monkeypatch)
    result = cli(folder_project, "view", "--terminal")
    assert result.exit_code == 0, result.output
    assert f"Run {short_id(docs.run_id)} (the most recently active run" in result.output
    cli(folder_project, "archive", short_id(docs.run_id))
    result = cli(folder_project, "land")
    assert result.exit_code == 0, result.output
    assert f"Run {short_id(perf.run_id)} (the most recently active run" in result.output
    assert f"Landed {short_id(perf.run_id)}." in result.output
    cli(folder_project, "archive", short_id(perf.run_id))
    result = cli(folder_project, "report")
    assert result.exit_code == 1 and "every run is archived" in result.output


def test_commands_from_a_subfolder_find_the_project(folder_project, monkeypatch):
    perf = started(folder_project, "perf")
    (folder_project / "src").mkdir()
    monkeypatch.chdir(folder_project / "src")
    runner = CliRunner()
    result = runner.invoke(main, ["view", "--json"])
    assert result.exit_code == 0, result.output
    assert "Using project .." in result.output
    assert perf.run_id in result.output
    result = runner.invoke(main, ["stop", short_id(perf.run_id)])
    assert "is not running" in result.output  # found, not "No run"
    result = runner.invoke(main, ["resume", short_id(perf.run_id), "--until", "1"])
    assert result.exit_code == 0 and "Already at iteration 1" in result.output


def test_stop_repeats_the_project_in_its_wait_advice(project, monkeypatch, tmp_path):
    from autohelix.run_status import RunStatus

    paths = add_run(project, "run-aaaa", created_at=time.time())
    status = RunStatus(project, paths.run_id)
    status.start(segment=1)
    monkeypatch.chdir(tmp_path)
    with RunLock(paths.lock):
        result = cli(project, "stop", "aaaa")
    assert result.exit_code == 0, result.output
    assert f"autohelix wait -p {os.path.relpath(project, tmp_path)} aaaa" in result.output


# --- the viewer serves named runs only ----------------------------------------------


def test_the_viewer_has_no_unprefixed_run_routes(folder_project):
    from autohelix.viewer import RunData, make_server

    perf = started(folder_project, "perf")
    with pytest.raises(ValueError):
        RunData(folder_project, None)
    server = make_server(folder_project, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def status(path):
        try:
            with urllib.request.urlopen(base + path) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, None

    try:
        for path in ("/api/state", "/api/iteration/1", "/api/notes/iter-1.md"):
            assert status(path)[0] == 404, path
        code, state = status(f"/api/run/{perf.run_id}/state")
        assert code == 200 and state["run_id"] == perf.run_id
    finally:
        server.shutdown()
        server.server_close()


# --- by hand, ask and review name their agent -----------------------------------------


def test_by_hand_ask_names_its_agent_and_where_the_settings_came_from(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AUTOHELIX_USAGE_FILE", raising=False)
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    config = {"goal": "g", "agent": {"backend": "mock", "model": "m1",
                                     "settings": {"judge_answer": "yes", "judge_reason": "fine"}}}
    (tmp_path / "autohelix").mkdir()
    (tmp_path / "autohelix" / "a.yaml").write_text(yaml.safe_dump(config))
    result = CliRunner().invoke(main, ["ask", "Done?"])
    assert result.exit_code == 0, result.output
    assert "autohelix ask: yes · mock m1 (autohelix/a.yaml) · " in result.output
    (tmp_path / "autohelix" / "b.yaml").write_text(yaml.safe_dump(config))
    result = CliRunner().invoke(main, ["ask", "--agent", "mock", "Done?"])
    assert " · mock (--agent) · " in result.output
    from autohelix.judge import base_config

    assert base_config(tmp_path)[1] == "defaults: several configs here"
    assert base_config(tmp_path / "autohelix")[1] == "defaults: no config here"


def test_project_runs_unchanged_by_a_bare_view(folder_project):
    started(folder_project, "perf")
    before = sorted(p.name for p in (folder_project / ".autohelix").rglob("*"))
    assert cli(folder_project, "view", "--once").exit_code == 0
    assert sorted(p.name for p in (folder_project / ".autohelix").rglob("*")) == before
    assert len(project_runs(folder_project)) == 1


def test_paths_without_an_id_name_no_run_even_beside_an_old_pointer(tmp_path):
    """There is no current-run pointer: an old `.internal/current` file is never followed
    (it used to be, and a hand-edited one broke every command)."""
    pointer = tmp_path / ".autohelix" / ".internal" / "current"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("My Run\n")
    paths = RunPaths(tmp_path)
    assert paths.run_id is None
    assert paths.config == tmp_path / ".autohelix" / "config.yaml"  # no run's stored config
