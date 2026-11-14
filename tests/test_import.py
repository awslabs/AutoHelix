"""`run --import` (a new run continuing from another's state), `resume` continuing only
its own run (a target branch moved past its tip, after a stop condition), and `--why`."""

import json
import shutil

from autohelix.harness import Budget, Harness
from autohelix.runs import RunPaths, git, short_id
from tests.test_groups import cli, identity, started, watch_agent
from tests.test_runs import configure

TRACKER = {"name": "tracker", "metrics": {"score": "higher"},
           "command": 'echo "run $AUTOHELIX_RUN_ID"; '
                      'if [ -n "$AUTOHELIX_PREVIOUS" ]; then echo "previous $(head -1 "$AUTOHELIX_PREVIOUS/stdout.txt")"; '
                      'else echo "previous none"; fi; echo "score: 1"'}
OTHER = {"name": "other", "command": 'echo "previous ${AUTOHELIX_PREVIOUS:-none}"'}
MOCK = {"backend": "mock", "settings": {"change_file": "code.py", "change_content": "# {iteration}",
                                        "notes_content": "tried {iteration}"}}


def stdout(paths, iteration, name):
    return (paths.observe_entry(iteration, name) / "stdout.txt").read_text()


def test_import_shows_the_runs_state_and_continues_its_observations(project, monkeypatch):
    configure(project, observe=[TRACKER], agent=MOCK, land="manual")
    parent, _ = started(project)
    configure(project, observe=[TRACKER, OTHER], agent=MOCK, land="manual")  # the config moved on
    child = Harness(project, new=True, import_run=parent.run_id)
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
    line = (f"This run continues from {short_id(parent.run_id)}: its notes and history are in "
            f"`.autohelix-iteration/runs/{parent.run_id}/` (read-only)")
    assert line in seen["prompt"] and f"its code is at `autohelix/{parent.run_id}`" in seen["prompt"]
    assert "Other runs in group" not in seen["prompt"]
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
    assert "This run continues from" in seen["prompt"] and "Other runs in group" not in seen["prompt"]


def test_a_deleted_imported_run_doesnt_stop_the_run(project, monkeypatch):
    configure(project, observe=[TRACKER], agent=MOCK)
    parent, _ = started(project)
    child = Harness(project, new=True, import_run=parent.run_id)
    shutil.rmtree(parent.run_dir)
    seen = {}
    watch_agent(child, monkeypatch, seen)
    child.run(Budget(until=1), report=False)
    assert "previous none" in stdout(child.paths, 0, "tracker")
    assert seen["runs"] == [] and "continues from" not in seen["prompt"]
    assert child.history.load()[-1].accepted


def landed_then_committed(project):
    paths, _ = started(project)  # auto landing: main is at the run's tip
    (project / "user.py").write_text("x = 1\n")
    git(project, "add", "user.py")
    git(project, "commit", "-qm", "user work")
    return paths


def test_resume_refuses_when_the_target_moved_past_the_tip(project):
    paths = landed_then_committed(project)
    tip = git(project, "rev-parse", paths.ref).stdout.strip()
    before = {p.name: p.stat().st_mtime_ns for p in paths.run_dir.rglob("*") if p.is_file()}
    result = cli(project, "resume", short_id(paths.run_id), "-n", "1", "--no-report")
    assert result.exit_code == 1, result.output
    short = short_id(paths.run_id)
    assert (f"has 1 commit past run {short}'s tip (committed after it landed). A run continues its own code: "
            f"start a new run that continues from it, `autohelix run --import {short}`, or continue without "
            f"landing: `autohelix resume {short} --land manual`.") in " ".join(result.output.split())
    assert git(project, "rev-parse", paths.ref).stdout.strip() == tip
    assert {p.name: p.stat().st_mtime_ns for p in paths.run_dir.rglob("*") if p.is_file()} == before
    assert not paths.copies.exists()


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
    assert result.exit_code == 0, result.output
    assert "diverged" in result.output
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
    assert "Why: try the heap approach" in once
    assert f"continues from {paths.run_id}" in once
    view = cli(project, "view", short_id(paths.run_id), "--once").output
    assert "Why: try the heap approach" in view

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
