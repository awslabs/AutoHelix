"""Groups (runs that see each other's notes and code), per-run landing modes, the
copy layout (`.autohelix-iteration/`), and git lock retries."""

import json
import os
import subprocess

import pytest
from click.testing import CliRunner

from autohelix import workspace
from autohelix.cli import main
from autohelix.config import Config
from autohelix.harness import Harness
from autohelix.runs import RunLock, RunPaths, archive, git, short_id
from tests.test_runs import configure, project  # noqa: F401 - fixture


def cli(project, *args):
    return CliRunner().invoke(main, [*args, "-p", str(project)])


def started(project, *args):
    result = cli(project, "run", "-n", "1", "--no-report", *args)
    assert result.exit_code == 0, result.output
    return RunPaths(project), result


def identity(paths):
    return json.loads(paths.identity.read_text())


def watch_agent(harness, monkeypatch, seen):
    """Record what the agent's copy holds when the agent starts."""
    real = harness.run_agent

    def agent(cwd, prompt, iteration):
        state = cwd / ".autohelix-iteration"
        seen.update(
            prompt=prompt, prompt_file=(state / "prompt.md").read_text(),
            runs=sorted(p.name for p in (state / "runs").iterdir()) if (state / "runs").is_dir() else [],
            member_files={p.name: sorted(c.name for c in p.iterdir()) for p in (state / "runs").iterdir()}
            if (state / "runs").is_dir() else {},
            refs=subprocess.run(["git", "for-each-ref", "--format=%(refname)"], cwd=cwd, capture_output=True,
                                text=True, env={**os.environ, "GIT_CEILING_DIRECTORIES": str(cwd.parent)}
                                ).stdout.split(),
            project_state=(cwd / ".autohelix").exists(),
        )
        return real(cwd, prompt, iteration)

    monkeypatch.setattr(harness, "run_agent", agent)


def test_group_runs_see_each_others_state_and_code_and_nothing_else(project, monkeypatch):  # noqa: F811
    configure(project, observe=[{"name": "bench", "command": "echo score: 3.5", "metrics": {"score": "higher"}}],
              agent={"backend": "mock", "settings": {"change_file": "code.py", "change_content": "# {iteration}",
                                                  "notes_content": "tried {iteration}"}})
    first = Harness(project, new=True, group="g")
    first.run(max_iterations=1, report=False)
    stranger = Harness(project, new=True, land_mode="manual")
    stranger.run(max_iterations=1, report=False)
    second = Harness(project, new=True, group="g")
    seen = {}
    watch_agent(second, monkeypatch, seen)
    second.run(max_iterations=1, report=False)

    one = first.paths.run_id
    assert seen["runs"] == [one]
    assert seen["member_files"] == {one: ["history.jsonl", "notes", "run.json"]}
    assert not seen["project_state"]
    assert f"refs/autohelix/{one}" in seen["refs"] and second.paths.ref in seen["refs"]
    assert stranger.paths.ref not in seen["refs"] and any(r.startswith("refs/heads/") for r in seen["refs"])
    line = (f"`.autohelix-iteration/runs/{one}/` (1 iteration, latest accepted score 3.5, finished; "
            f"code at `autohelix/{one}`)")
    assert "Other runs in group `g`" in seen["prompt"] and line in seen["prompt"]
    assert "name that run in your summary" in seen["prompt"]
    assert seen["prompt_file"] == seen["prompt"]
    # The first run (alone in its group then) was not told about anyone.
    first_prompt = next(first.paths.logs.glob("iter-1/call-*-agent/prompt.txt")).read_text()
    assert "Other runs in group" not in first_prompt

    # Archiving is how a run leaves its group.
    archive(first.paths)
    third = Harness(project, run_id=second.paths.run_id)
    seen.clear()
    watch_agent(third, monkeypatch, seen)
    third.run(max_iterations=2, report=False)
    assert seen["runs"] == [] and f"refs/autohelix/{one}" not in seen["refs"]
    assert "Other runs in group" not in seen["prompt"]


def test_group_and_landing_mode_are_stored_kept_and_changed(project):  # noqa: F811
    plain, _ = started(project)
    assert identity(plain).get("land") == "auto" and "group" not in identity(plain)
    grouped, _ = started(project, "--group", "perf")
    run_id = grouped.run_id
    assert identity(grouped)["group"] == "perf" and identity(grouped)["land"] == "manual"

    result = cli(project, "resume", short_id(run_id), "-n", "1", "--no-report", "--group", "other")
    assert result.exit_code == 0, result.output
    assert "joins group other" in result.output
    assert identity(grouped)["group"] == "other" and identity(grouped)["land"] == "manual"
    result = cli(project, "resume", short_id(run_id), "-n", "1", "--no-report", "--group", "", "--land", "auto")
    assert result.exit_code == 0, result.output
    assert "leaves group other" in result.output and "  Landed " in result.output
    data = identity(grouped)
    assert "group" not in data and data["land"] == "auto"

    configure(project, land="manual")  # the config's land: is the default for new runs only
    result = cli(project, "resume", short_id(run_id), "-n", "1", "--no-report")
    assert result.exit_code == 0 and identity(grouped)["land"] == "auto" and "  Landed " in result.output
    fresh, _ = started(project)
    assert identity(fresh)["land"] == "manual"
    explicit, _ = started(project, "--group", "perf", "--land", "auto")
    assert identity(explicit)["land"] == "auto"

    bad = cli(project, "run", "--group", "Not A Name")
    assert bad.exit_code == 2 and "group must use lowercase" in bad.output


def test_config_land_keeps_unset_apart_from_auto():
    assert Config.from_dict({"goal": "g"}).land is None
    assert Config.from_dict({"goal": "g", "land": "auto"}).land == "auto"
    assert any("land must be" in issue.message for issue in Config.from_dict({"goal": "g", "land": "x"}).validate())


def test_legacy_runs_without_a_recorded_mode_follow_the_config(project):  # noqa: F811
    paths, _ = started(project, "--land", "manual")
    data = identity(paths)
    del data["land"]
    paths.identity.write_text(json.dumps(data))
    configure(project, land="manual")
    harness = Harness(project, run_id=paths.run_id)
    assert harness.land_mode == "manual"
    harness.close()


def test_group_runs_dont_warn_about_each_other_while_live(project):  # noqa: F811
    first, _ = started(project, "--group", "g")
    with RunLock(first.lock):
        _, result = started(project, "--group", "g")
    assert "is also running for this config" not in result.output
    with RunLock(first.lock):
        _, result = started(project)
    assert f"{short_id(first.run_id)} is also running for this config" in result.output


def test_dashboard_shows_the_group_after_the_id(project):  # noqa: F811
    grouped, _ = started(project, "--group", "perf")
    plain, _ = started(project, "--land", "manual")
    result = cli(project, "dashboard", "--once")
    assert result.exit_code == 0, result.output
    assert f"{grouped.run_id} (perf)" in result.output
    assert f"{plain.run_id} (" not in result.output
    rows = {row["id"]: row for row in json.loads(cli(project, "dashboard", "--json").output)["runs"]}
    assert rows[grouped.run_id]["group"] == "perf" and rows[grouped.run_id]["land"] == "manual"
    assert rows[plain.run_id]["group"] is None and rows[plain.run_id]["land"] == "manual"
    view = cli(project, "view", short_id(grouped.run_id), "--once")
    assert view.output.startswith(f"{grouped.run_id} (perf)")


def test_notes_are_saved_as_versions_and_delete_removes_them(project):  # noqa: F811
    configure(project, agent={"backend": "mock", "settings": {"change_file": "code.py",
                                                           "change_content": "# {iteration}",
                                                           "notes_content": "note {iteration}"}})
    paths, _ = started(project, "--group", "g")
    other, _ = started(project, "--group", "g")
    assert paths.notes.is_symlink() and os.readlink(paths.notes) == "notes.1"
    assert (paths.notes / "notes.md").read_text() == "note 1"
    result = cli(project, "resume", short_id(paths.run_id), "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert os.readlink(paths.notes) == "notes.2" and (paths.run_dir / "notes.1" / "notes.md").read_text() == "note 1"
    # The viewer reads through the link.
    page = json.loads(cli(project, "view", short_id(paths.run_id), "--json").output)
    assert page["run"]["id"] == paths.run_id
    from autohelix.viewer import RunData

    assert RunData(project, run_id=paths.run_id).state()["notes_files"] == ["notes.md"]
    result = cli(project, "delete", "--force", short_id(paths.run_id))
    assert result.exit_code == 0, result.output
    assert not os.path.lexists(paths.run_dir)
    assert (other.notes / "notes.md").read_text() == "note 1"  # the group's other run is untouched


def test_freeze_never_hashes_harness_material_into_the_users_objects(project, monkeypatch):  # noqa: F811
    """Seeded material (here a hint) and files the agent leaves in `.autohelix-iteration/`
    never become objects in the user's repository."""
    unique = {"hint": f"hint {os.urandom(8).hex()}", "scratch": f"scratch {os.urandom(8).hex()}\n"}
    harness = Harness(project, new=True, land_mode="manual")
    harness.paths.hints.write_text(unique["hint"] + "\n")
    real = harness.run_agent

    def agent(cwd, prompt, iteration):
        assert (cwd / ".autohelix-iteration" / "hints.md").read_text() == unique["hint"] + "\n"
        (cwd / ".autohelix-iteration" / "scratch.txt").write_text(unique["scratch"])
        return real(cwd, prompt, iteration)

    monkeypatch.setattr(harness, "run_agent", agent)
    harness.run(max_iterations=1, report=False)
    assert harness.history.load()[-1].accepted
    objects = git(project, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").stdout.split()
    for text in (unique["hint"] + "\n", unique["scratch"]):
        oid = subprocess.run(["git", "hash-object", "--stdin"], cwd=project, input=text, text=True,
                             capture_output=True, check=True).stdout.strip()
        assert oid not in objects


def test_git_lock_errors_are_retried_briefly(project, monkeypatch):  # noqa: F811
    real = subprocess.run
    calls = []

    def flaky(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            return subprocess.CompletedProcess(argv, 128, b"" if not kwargs.get("text") else "",
                                               "fatal: Unable to create '/x/.git/packed-refs.lock': File exists.")
        return real(argv, **kwargs)

    monkeypatch.setattr(workspace.subprocess, "run", flaky)
    monkeypatch.setattr(workspace, "_LOCK_RETRIES", (0, 0))
    head = git(project, "rev-parse", "HEAD").stdout.strip()
    assert len(calls) == 2 and len(head) == 40
    calls.clear()
    assert workspace.Repo(project).out("rev-parse", "HEAD") == head  # Repo.git retries the same way

    def failing(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "fatal: cannot lock ref 'refs/x': is at 1 but expected 2")

    calls.clear()
    monkeypatch.setattr(workspace.subprocess, "run", failing)
    with pytest.raises(RuntimeError, match="expected 2"):
        git(project, "update-ref", "refs/x", head)
    assert len(calls) == 1  # a CAS mismatch is not contention


def test_resuming_an_archived_member_says_it_rejoins_its_group(project):  # noqa: F811
    paths, _ = started(project, "--group", "g")
    assert cli(project, "archive", short_id(paths.run_id)).exit_code == 0
    result = cli(project, "resume", short_id(paths.run_id), "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert "resuming brings it back into group g" in " ".join(result.output.split())
    assert "archived" not in identity(paths)


def test_a_template_naming_the_old_copy_layout_is_flagged():
    from autohelix.prompt_template import DEFAULT_TEMPLATE, uses_old_layout

    assert not uses_old_layout(DEFAULT_TEMPLATE)
    assert uses_old_layout("write to {{ worktree }}/.autohelix/notes/iter-{{ iteration }}.md")


def test_a_new_member_doesnt_warn_about_unlanded_members(project):  # noqa: F811
    first, _ = started(project, "--group", "g")  # manual landing: its commit stays unlanded
    _, result = started(project, "--group", "g")
    assert "unlanded commit" not in result.output.split("  AutoHelix ·")[0] and "Previous run" not in result.output
    _, result = started(project)  # outside the group the warning stands
    assert "unlanded commit" in result.output.split("  AutoHelix ·")[0]  # the warning, before the header


def test_a_checks_copy_gets_the_complete_change_and_nothing_else(project):  # noqa: F811
    seen = project / "seen"
    configure(project, gates=[
        f"ls -A .autohelix-iteration > {seen}; grep -c '^+' .autohelix-iteration/candidate.diff >> {seen}"])
    started(project)
    listing = seen.read_text().split()
    assert listing[0] == "candidate.diff" and listing[1] != "0"  # iteration 1's change, not the baseline's


def test_a_broken_member_doesnt_stop_another_run(project):  # noqa: F811
    first, _ = started(project, "--group", "g")
    first.history.write_text("not json\n" + first.history.read_text())  # corrupt, not a torn last line
    second, result = started(project, "--group", "g")
    records = [json.loads(line) for line in second.history.read_text().splitlines()]
    assert [r["iteration"] for r in records] == [0, 1] and records[1]["accepted"]
    first.history.unlink()
    (first.run_dir / "notes.1").rename(first.run_dir / "gone")  # its notes link now dangles
    result = cli(project, "resume", short_id(second.run_id), "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output


def test_a_group_lands_manually_even_with_land_auto_in_the_config(project):  # noqa: F811
    configure(project, land="auto")
    paths, _ = started(project, "--group", "g")
    assert identity(paths)["land"] == "manual"
    paths, _ = started(project, "--group", "g", "--land", "auto")
    assert identity(paths)["land"] == "auto"


def test_unchanged_notes_are_hard_links_and_a_removed_notes_folder_stays_removed(project, monkeypatch):  # noqa: F811
    from autohelix import iteration

    paths, _ = started(project)
    (paths.notes / "keep.md").write_text("same\n")  # as if saved with iteration 1
    copy = type("C", (), {})()
    copy.working_dir = project / "fake-copy"
    notes = copy.working_dir / ".autohelix-iteration" / "notes"
    notes.mkdir(parents=True)
    (notes / "keep.md").write_text("same\n")
    (notes / "new.md").write_text("new\n")
    iteration.save_notes(paths, copy, 2)
    assert os.stat(paths.run_dir / "notes.1" / "keep.md").st_ino == os.stat(paths.notes / "keep.md").st_ino
    assert (paths.notes / "new.md").read_text() == "new\n"
    import shutil
    shutil.rmtree(notes)
    iteration.save_notes(paths, copy, 3)
    assert list(paths.notes.iterdir()) == []


def test_the_report_copy_lists_the_runs_own_ref(project, monkeypatch):  # noqa: F811
    from autohelix import workspace

    seen = {}
    real = workspace.Copy.create.__func__

    def create(cls, repo, path, **kwargs):
        seen["refs"] = kwargs.get("refs")
        return real(cls, repo, path, **kwargs)

    paths, _ = started(project)
    monkeypatch.setattr(workspace.Copy, "create", classmethod(create))
    configure(project, report=True)
    result = cli(project, "report", short_id(paths.run_id))
    assert result.exit_code == 0, result.output
    assert seen["refs"] == (paths.ref,)


def test_an_old_prompt_file_is_reported_as_no_longer_read(project):  # noqa: F811
    (project / ".autohelix" / "prompt.md").write_text("Old: {{ goal }}\n")
    _, result = started(project)
    assert ".autohelix/prompt.md is no longer read" in result.output
