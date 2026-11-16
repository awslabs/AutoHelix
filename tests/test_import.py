"""`run --import` (a new run continuing from another's state), `resume` continuing only
its own run (a target branch moved past its tip, after a stop condition), and `--why`."""

import json
import os
import shutil

from autohelix.harness import Budget, Harness
from autohelix.runs import RunPaths, git, short_id
from tests.test_groups import cli, identity, started, watch_agent
from tests.test_runs import configure

TRACKER = {"name": "tracker", "metrics": {"score": "higher"},
           "command": 'echo "run $AUTOHELIX_RUN_ID"; '
                      'if [ -n "$AUTOHELIX_PREVIOUS" ]; then echo "previous $(head -1 "$AUTOHELIX_PREVIOUS/stdout.txt")"; '
                      'else echo "previous none"; fi; echo "##autohelix[score=1]"'}
OTHER = {"name": "other", "command": 'echo "previous ${AUTOHELIX_PREVIOUS:-none}"'}
MOCK = {"backend": "mock", "settings": {"change_file": "code.py", "change_content": "# {iteration}",
                                        "notes_content": "tried {iteration}"}}


def stdout(paths, iteration, name):
    return (paths.observe_entry(iteration, name) / "stdout.txt").read_text()


def snapshot(*roots):
    """Every file and link below `roots` (files themselves too): contents and mtimes."""
    found = {}
    for root in roots:
        for path in [root, *root.rglob("*")] if root.is_dir() else [root]:
            if path.is_symlink():
                found[str(path)] = ("link", os.readlink(path))
            elif path.is_file():
                found[str(path)] = (path.read_bytes(), path.stat().st_mtime_ns)
    return found


def test_import_shows_the_runs_state_and_continues_its_observations(project, monkeypatch):
    configure(project, observe=[TRACKER], agent=MOCK)
    parent, _ = started(project, "--land", "manual")
    stranger, _ = started(project, "--land", "manual")  # neither imported nor in a group: nothing of it in the copy
    configure(project, observe=[TRACKER, OTHER], agent=MOCK)  # the config moved on
    before = snapshot(parent.run_dir)
    child = Harness(project, new=True, import_run=parent.run_id, land_mode="manual")
    seen = {}
    watch_agent(child, monkeypatch, seen)
    notes = []
    real = child.run_agent

    def agent(cwd, prompt, iteration):
        state = cwd / ".autohelix-iteration"
        notes.append(sorted(p.name for p in (state / "notes").iterdir()))
        return real(cwd, prompt, iteration)

    monkeypatch.setattr(child, "run_agent", agent)
    child.run(Budget(until=1), report=False)

    data = identity(child.paths)
    assert data["import"] == {"run": parent.run_id} and "group" not in data
    # The baseline continues the parent's tracker (its last successful output), and an
    # entry the parent didn't have gets none.
    assert f"previous run {parent.run_id}" in stdout(child.paths, 0, "tracker")
    assert "previous none" in stdout(child.paths, 0, "other")
    # Later iterations continue the run's own.
    assert f"previous run {child.paths.run_id}" in stdout(child.paths, 1, "tracker")
    # The copy shows the parent's state read-only, and its code's ref.
    assert seen["runs"] == [parent.run_id]
    assert seen["member_files"] == {parent.run_id: ["history.jsonl", "notes", "run.json"]}
    assert f"refs/autohelix/{parent.run_id}" in seen["refs"]
    assert stranger.ref not in seen["refs"] and not any(stranger.run_id in r for r in seen["refs"])
    assert snapshot(parent.run_dir) == before  # read, never written
    line = (f"  - Imported (this run continues from {short_id(parent.run_id)}; its notes and history are where "
            f"that work left off):\n    `.autohelix-iteration/runs/{parent.run_id}/` (1 iteration, latest accepted "
            f"score 1, finished; code at `autohelix/{parent.run_id}`)")
    assert line in seen["prompt"]
    assert "- .autohelix-iteration/notes/ — yours: this run's notes, editable; numbered by this run's iterations" \
        in seen["prompt"]
    # The parent's commit didn't land (manual): the copy's code isn't its tip.
    assert (f"Your copy holds this run's code, not necessarily {short_id(parent.run_id)}'s: "
            f"`git diff HEAD autohelix/{parent.run_id}` shows what differs.") in seen["prompt"]
    assert "Group `" not in seen["prompt"]
    # Its own notes start empty, versioned from its iteration 1.
    assert notes == [[]]
    assert sorted(p.name for p in child.paths.notes_versions.iterdir()) == ["1"]
    # The metric reference is fresh: the baseline is its own.
    assert child.history.load()[0].metrics == {"score": 1.0}


def test_import_by_suffix_warns_when_the_config_differs(project):
    parent, _ = started(project)
    other = project / "autohelix" / "perf.yaml"
    other.parent.mkdir()
    shutil.copy(project / "autohelix.yaml", other)
    git(project, "add", ".")
    git(project, "commit", "-qm", "perf")
    result = cli(project, "run", "perf", "-n", "1", "--no-report", "--import", short_id(parent.run_id))
    assert result.exit_code == 0, result.output
    assert f"Run {short_id(parent.run_id)} used autohelix.yaml; this run uses autohelix/perf.yaml." in result.output
    same = cli(project, "run", "-n", "1", "--no-report", "--import", short_id(parent.run_id))
    assert same.exit_code == 0 and " used " not in same.output
    missing = cli(project, "run", "-n", "1", "--import", "zzzz")
    assert missing.exit_code == 1 and "No run 'zzzz'" in missing.output


def test_an_imported_group_member_is_shown_once_as_imported(project, monkeypatch):
    configure(project, agent=MOCK)
    parent, _ = started(project, "--group", "g")
    child = Harness(project, new=True, group="g", import_run=parent.run_id)
    seen = {}
    watch_agent(child, monkeypatch, seen)
    child.run(Budget(until=1), report=False)
    assert identity(child.paths)["group"] == "g" and identity(child.paths)["import"] == {"run": parent.run_id}
    assert seen["runs"] == [parent.run_id]
    assert "- Imported (this run continues from" in seen["prompt"] and "Group `g` members" not in seen["prompt"]


def test_a_deleted_imported_run_doesnt_stop_the_run(project, monkeypatch):
    configure(project, observe=[TRACKER], agent=MOCK)
    parent, _ = started(project)
    child = Harness(project, new=True, import_run=parent.run_id)
    shutil.rmtree(parent.run_dir)
    seen = {}
    watch_agent(child, monkeypatch, seen)
    child.run(Budget(until=1), report=False)
    assert "previous none" in stdout(child.paths, 0, "tracker")
    assert seen["runs"] == [] and "continues from" not in seen["prompt"] and "Where things are" not in seen["prompt"]
    assert child.history.load()[-1].accepted


def test_a_tracker_continues_from_the_import_until_it_succeeds_in_the_run(project):
    log = {"name": "log", "command": '[ "$AUTOHELIX_ITERATION" = 0 ] && exit 1; echo "run $AUTOHELIX_RUN_ID"; '
                                     'echo "previous $(head -1 "${AUTOHELIX_PREVIOUS:-/dev/null}/stdout.txt" 2>/dev/null)"'}
    configure(project, observe=[log], agent=MOCK)
    parent, _ = started(project, "--land", "manual")
    child, _ = started(project, "--land", "manual", "--import", short_id(parent.run_id), "-n", "2")
    assert not child.observe_entry(0, "log").joinpath("stdout.txt").read_text()  # failed at the baseline
    assert f"previous run {parent.run_id}" in stdout(child, 1, "log")  # so iteration 1 continues the import
    assert f"previous run {child.run_id}" in stdout(child, 2, "log")


def test_a_corrupt_imported_history_doesnt_stop_the_run(project):
    configure(project, observe=[TRACKER], agent=MOCK)
    parent, _ = started(project)
    parent.history.write_text("not json\n" + parent.history.read_text())
    child, result = started(project, "--import", short_id(parent.run_id))
    assert "previous none" in stdout(child, 0, "tracker")
    assert [r["accepted"] for r in map(json.loads, child.history.read_text().splitlines())] == [True, True]


def test_an_imported_run_json_without_a_config_is_refused_cleanly(project):
    parent, _ = started(project)
    data = identity(parent)
    del data["config_path"]
    parent.identity.write_text(json.dumps(data))
    result = cli(project, "run", "-n", "1", "--import", short_id(parent.run_id))
    assert result.exit_code == 1 and "names no config file; it can't be imported" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def landed_then_committed(project):
    paths, _ = started(project)  # auto landing: main is at the run's tip
    (project / "user.py").write_text("x = 1\n")
    git(project, "add", "user.py")
    git(project, "commit", "-qm", "user work")
    return paths


def test_resume_refuses_when_the_target_moved_past_the_tip(project):
    paths = landed_then_committed(project)
    tip = git(project, "rev-parse", paths.ref).stdout.strip()
    state = (project / ".autohelix", project / ".git" / "info" / "exclude")
    before = snapshot(*state)
    result = cli(project, "resume", short_id(paths.run_id), "-n", "1", "--no-report")
    assert result.exit_code == 1, result.output
    short = short_id(paths.run_id)
    assert (f"has 1 commit past run {short}'s tip (committed after its tip). A run continues its own code: "
            f"start a new run that continues from it, `autohelix run --import {short}`, or continue without "
            f"landing: `autohelix resume {short} --land manual`.") in " ".join(result.output.split())
    assert git(project, "rev-parse", paths.ref).stdout.strip() == tip
    assert snapshot(*state) == before  # nothing written: no status, log, copies record or excludes
    assert not paths.copies.exists()
    # A run elsewhere says so first: `--land manual` would only lead there.
    from autohelix.runs import RunLock

    with RunLock(paths.lock):
        result = cli(project, "resume", short, "-n", "1", "--no-report")
    assert result.exit_code == 1 and f"Run {short} is running." in result.output
    assert "past run" not in result.output


def test_the_refusal_says_when_the_run_never_accepted_a_change(project):
    configure(project, agent={"backend": "mock", "settings": {"change_file": ".autohelix-iteration/x"}})
    paths, _ = started(project)  # a no-op: the tip is still the base commit
    assert git(project, "rev-parse", paths.ref).stdout.strip() == identity(paths)["base_commit"]
    (project / "user.py").write_text("x = 1\n")
    git(project, "add", "user.py")
    git(project, "commit", "-qm", "user work")
    result = cli(project, "resume", short_id(paths.run_id), "-n", "1", "--no-report")
    assert result.exit_code == 1, result.output
    assert (f"(committed since run {short_id(paths.run_id)} started; it hasn't accepted any changes)"
            in " ".join(result.output.split()))


def test_a_manual_resume_continues_from_its_own_tip(project):
    paths = landed_then_committed(project)
    tip = git(project, "rev-parse", paths.ref).stdout.strip()
    result = cli(project, "resume", short_id(paths.run_id), "-n", "1", "--no-report", "--land", "manual")
    assert result.exit_code == 0, result.output
    assert f"past run {short_id(paths.run_id)}'s tip; it continues from its own tip" in " ".join(result.output.split())
    new_tip = git(project, "rev-parse", paths.ref).stdout.strip()
    assert git(project, "rev-parse", f"{new_tip}^").stdout.strip() == tip  # built on the run's tip, not main
    assert git(project, "show", f"{new_tip}:user.py", check=False).returncode  # without the user's commit
    # Auto landing again: diverged, said as before, nothing lost.
    result = cli(project, "resume", short_id(paths.run_id), "-n", "1", "--no-report", "--land", "auto")
    assert result.exit_code == 1, result.output  # a landing that failed is an error
    assert "landing failed: Cannot land: main diverged" in " ".join(result.output.split())
    assert git(project, "show", "HEAD:user.py").returncode == 0


def test_resume_after_a_stop_condition_says_it_continues(project):
    configure(project, stop=["echo 'score reached 3'; exit 0"])
    paths, result = started(project)
    assert "stop condition met" in result.output
    result = cli(project, "resume", short_id(paths.run_id), "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert (f"Run {short_id(paths.run_id)} stopped because score reached 3; it stops again after the next "
            "accepted iteration if that still holds.") in " ".join(result.output.split())
    assert [r["iteration"] for r in map(json.loads, paths.history.read_text().splitlines())] == [0, 1]


def test_why_is_stored_kept_and_shown(project):
    paths, _ = started(project, "--why", "  try the\nheap approach  ")
    assert identity(paths)["why"] == "try the heap approach"
    other, _ = started(project, "--import", short_id(paths.run_id), "--land", "manual")
    once = cli(project, "dashboard", "--once").output
    assert "why: try the heap approach" in once
    assert f"continues from {paths.run_id}" in once
    view = cli(project, "view", short_id(paths.run_id), "--once").output
    assert "  Notes      why: try the heap approach" in view

    short = short_id(other.run_id)
    assert cli(project, "archive", short, "--why", "superseded by k3f9").exit_code == 0
    assert identity(other)["archive_why"] == "superseded by k3f9" and identity(other)["archived"]
    assert cli(project, "archive", "--undo", short).exit_code == 0
    assert identity(other)["archive_why"] == "superseded by k3f9" and not identity(other).get("archived")
    assert cli(project, "archive", short, "--why", "merged by hand").exit_code == 0
    assert identity(other)["archive_why"] == "merged by hand"
    assert cli(project, "archive", short).exit_code == 0
    assert identity(other)["archive_why"] == "merged by hand"

    rows = {row["id"]: row for row in json.loads(cli(project, "dashboard", "--json").output)["runs"]}
    assert rows[paths.run_id]["why"] == "try the heap approach" and rows[paths.run_id]["import"] is None
    assert rows[other.run_id]["archive_why"] == "merged by hand"
    assert rows[other.run_id]["import"] == {"run": paths.run_id} and rows[other.run_id]["why"] is None

    assert cli(project, "run", "--why", "   ").exit_code == 2
    assert "use at most 300" in cli(project, "run", "--why", "x" * 400).output
    assert cli(project, "archive", short, "--undo", "--why", "x").exit_code == 2


def test_why_on_the_web_pages_is_text(project):
    from autohelix.viewer import ProjectRuns, RunData

    paths, _ = started(project, "--why", "<b>bold</b>")
    assert RunData(project, paths.run_id).state()["why"] == "<b>bold</b>"
    assert ProjectRuns(project).runs()["runs"][0]["why"] == "<b>bold</b>"
    data = identity(paths)
    data["why"], data["import"] = {"not": "text"}, "k3f9"  # untrusted run.json
    paths.identity.write_text(json.dumps(data))
    row = ProjectRuns(project).runs()["runs"][0]
    assert row["why"] is None and row["import"] is None
    from importlib.resources import files

    resources = files("autohelix").joinpath("viewer_resources")
    for name in ("dashboard.js", "viewer.js"):
        script = resources.joinpath(name).read_text()
        assert "run.why && `Why: ${run.why}`" in script  # set as textContent, never as HTML
    assert "className: 'status-sub run-why', textContent: notes" in resources.joinpath("dashboard.js").read_text()
    assert "setText('why', why)" in resources.joinpath("viewer.js").read_text()
