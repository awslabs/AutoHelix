"""Tests for live run status and the `autohelix view` server."""

import hashlib
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from autohelix.agents import AgentRunResult
from autohelix.harness import AutoHelixRunError, Harness
from autohelix.run_status import RunStatus, read_run_status
from autohelix.viewer import make_server
from autohelix.runs import RunPaths, short_id
from tests.test_integration import integration_project  # noqa: F401  (fixture)


# ---------- run status ----------

def test_run_status_lifecycle(tmp_path, run_paths):
    status = RunStatus(tmp_path)
    status.phase("agent", 1)  # Before start: ignored, writes nothing.
    assert read_run_status(tmp_path) is None

    status.start(max_iterations=3)
    status.phase("agent", 2)
    data = read_run_status(tmp_path)
    assert (data["state"], data["iteration"], data["phase"]) == ("running", 2, "agent")

    status.stop("interrupted")
    status.stop("finished")  # First stop wins.
    assert read_run_status(tmp_path)["state"] == "interrupted"


def test_dead_process_reads_as_crashed(tmp_path, run_paths):
    status = RunStatus(tmp_path)
    status.start()
    data = json.loads(status.path.read_text())
    proc = subprocess.Popen(["true"])
    proc.wait()
    data["pid"] = proc.pid  # A pid that has exited.
    status.path.write_text(json.dumps(data))
    assert read_run_status(tmp_path)["state"] == "crashed"


def test_harness_records_finished_run(integration_project):  # noqa: F811
    Harness(integration_project).run(max_iterations=1)
    data = read_run_status(integration_project)
    assert data["state"] == "finished"
    assert data["iteration"] == 1


def test_harness_records_failed_run(integration_project, monkeypatch):  # noqa: F811
    harness = Harness(integration_project)

    def broken(*args, **kwargs):  # the harness itself failing, not the agent (that's a rejection)
        raise OSError("disk full")

    monkeypatch.setattr(harness, "run_agent", broken)
    with pytest.raises(AutoHelixRunError):
        harness.run(max_iterations=1)
    assert read_run_status(integration_project)["state"] == "failed"
    # A failed agent call is a rejected iteration: the run finishes normally.
    harness = Harness(integration_project)
    monkeypatch.setattr(harness, "run_agent", lambda *a, **k: AgentRunResult(success=False, exit_code=1))
    harness.run(max_iterations=2)
    assert read_run_status(integration_project)["state"] == "finished"


# ---------- viewer server ----------

@pytest.fixture
def viewer(integration_project):  # noqa: F811
    Harness(integration_project).run(max_iterations=2)
    root = RunPaths(integration_project).run_dir
    (root / "notes" / "iter-2.md").write_text("Tried **the obvious** thing.\n\n<script>alert(1)</script>")
    server = make_server(integration_project, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def get(url, host=None):
    request = urllib.request.Request(url, headers={"Host": host} if host else {})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_state_lists_iterations_and_status(viewer):
    status, body = get(viewer + "/api/state")
    state = json.loads(body)
    assert status == 200
    assert [r["iteration"] for r in state["iterations"]] == [0, 1, 2]
    assert all(r["accepted"] for r in state["iterations"])
    assert state["status"]["state"] == "finished"
    assert state["goal"].startswith("Test the integration")
    assert state["config"]["file"] == "autohelix.yaml"
    assert state["config"]["gates"] == ["true"]
    assert state["title"] == "Test the integration"
    assert "goal: Test the integration" in state["config"]["text"]
    # The run finished, so it wrote a summary (the mock agent's fixed text).
    assert state["report"]["html"] == "<p>Mock summary of the run.</p>\n"
    assert state["report"]["last_iteration"] == 2


def test_iteration_detail_has_notes_and_diff(viewer):
    detail = json.loads(get(viewer + "/api/iteration/2")[1])
    assert "<strong>the obvious</strong>" in detail["notes"]
    assert "<script>" not in detail["notes"]  # Raw HTML is escaped, not rendered.
    assert "Modified in iteration 2" in detail["diff"]
    assert [(g["text"], g["passed"]) for g in detail["gates"]] == [("true", True)]
    assert detail["observe"] == [] and detail["discarded"] == []
    assert isinstance(detail["agent"], list)


def test_static_assets_served(viewer):
    for path in ("/", "/viewer.js", "/viewer.css", "/logo-light.svg"):
        assert get(viewer + path)[0] == 200


def test_rejects_non_loopback_host(viewer):
    assert get(viewer + "/api/state", host="attacker.example")[0] == 403
    # SSH forwards may use any local port; only the host name matters.
    assert get(viewer + "/api/state", host="localhost:9999")[0] == 200


def test_no_arbitrary_file_routes(viewer):
    for path in ("/history.jsonl", "/../autohelix.yaml", "/api/iteration/..%2fhistory", "/api/file"):
        assert get(viewer + path)[0] == 404


def test_read_only(viewer):
    request = urllib.request.Request(viewer + "/api/state", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 501


def test_notes_list_and_note_route(viewer):
    state = json.loads(get(viewer + "/api/state")[1])
    assert "iter-2.md" in state["notes_files"]
    note = json.loads(get(viewer + "/api/notes/iter-2.md")[1])
    assert "<strong>the obvious</strong>" in note["html"]
    assert get(viewer + "/api/notes/..%2Fhistory.jsonl")[0] == 404
    assert get(viewer + "/api/notes/missing.md")[0] == 404


def test_artifacts_listed_and_served_safely(viewer, integration_project):  # noqa: F811
    obs = RunPaths(integration_project).observations / "iter-2" / "bench"
    obs.mkdir(parents=True, exist_ok=True)
    (obs / "page.html").write_text("<script>alert(1)</script>")
    (obs / "plot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    sections = json.loads(get(viewer + "/api/iteration/2")[1])["observe"]
    assert {"bench/page.html", "bench/plot.png"} <= {a["path"] for a in sections[0]["files"]}

    request = urllib.request.Request(viewer + "/api/artifact/2/bench/page.html")
    with urllib.request.urlopen(request) as response:
        assert response.headers["Content-Type"].startswith("text/plain")  # never rendered as HTML
    with urllib.request.urlopen(viewer + "/api/artifact/2/bench/plot.png") as response:
        assert response.headers["Content-Type"] == "image/png"
    assert get(viewer + "/api/artifact/2/..%2F..%2Fhistory.jsonl")[0] == 404
    assert get(viewer + "/api/artifact/2/bench/../../../history.jsonl")[0] == 404


def test_diff_is_relative_to_baseline(viewer):
    one = json.loads(get(viewer + "/api/iteration/1")[1])["diff"]
    two = json.loads(get(viewer + "/api/iteration/2")[1])["diff"]
    assert "Modified in iteration 1" in one and "Modified in iteration 2" not in one
    assert "+# Modified in iteration 1" in two and "+# Modified in iteration 2" in two


def test_running_iteration_shows_copy_diff(viewer, integration_project):  # noqa: F811
    from autohelix.runs import ensure_copies
    from autohelix.workspace import Copy, Repo

    paths = RunPaths(integration_project)
    ensure_copies(paths.copies)
    copy = Copy.create(Repo(integration_project), paths.copies / "iter-3-agent",
                       files="HEAD", head="HEAD")
    private_index = (copy.path / ".git" / "index").read_bytes()
    (copy.path / "hello.py").write_text('print("work in progress")\n')
    (copy.path / "brand_new.py").write_text("NEW = 1\n")  # untracked: plain git diff would miss it
    (copy.path / ".autohelix-iteration" / "notes").mkdir(parents=True)  # the harness's material
    (copy.path / ".autohelix-iteration" / "notes" / "iter-3.md").write_text("SEEDED NOTE\n")
    diff = json.loads(get(viewer + "/api/iteration/3")[1])["diff"]
    assert '+print("work in progress")' in diff
    assert "+NEW = 1" in diff
    assert "SEEDED NOTE" not in diff and ".autohelix-iteration" not in diff
    assert (copy.path / ".git" / "index").read_bytes() == private_index
    copy.remove()


def test_viewer_index_is_private_and_one_per_run(viewer, integration_project):  # noqa: F811
    from autohelix.runs import ensure_copies
    from autohelix.workspace import Copy, Repo, _private_index_dir

    paths = RunPaths(integration_project)
    ensure_copies(paths.copies)
    for n in (3, 4):
        copy = Copy.create(Repo(integration_project), paths.copies / f"iter-{n}-agent", files="HEAD", head="HEAD")
        get(viewer + f"/api/iteration/{n}")
        copy.remove()
    # Removed at process exit with the harness indexes; an earlier iteration's is dropped.
    run = hashlib.sha256(os.fsencode(paths.copies)).hexdigest()[:16]
    assert [p.name for p in _private_index_dir().glob(f"view-{run}-*")] == [f"view-{run}-iter-4-agent.index"]


@pytest.mark.parametrize("phase, shown", [("preparing checks", True), ("checks", True), ("review", True),
                                           ("agent", False), ("between iterations", False)])
def test_frozen_candidate_diff_only_once_this_iteration_froze(viewer, integration_project, phase, shown):  # noqa: F811
    # As if a segment were live: the pin holds iteration 2's tree; iteration 3 has no copy yet.
    paths = RunPaths(integration_project)
    tree = subprocess.run(["git", "rev-parse", f"{paths.ref}^{{tree}}"], cwd=integration_project,
                          capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "update-ref", paths.pin, tree], cwd=integration_project, check=True)
    status = json.loads(paths.status.read_text())
    paths.status.write_text(json.dumps({**status, "iteration": 3, "phase": phase}))
    diff = json.loads(get(viewer + "/api/iteration/3")[1])["diff"]
    assert (diff is not None and "Modified in iteration 2" in diff) is shown


def test_long_logs_are_clipped_visibly(tmp_path, monkeypatch, run_paths):
    import autohelix.viewer as viewer_module
    monkeypatch.setattr(viewer_module, "MAX_EVENTS", 5)
    log = run_paths.call_dir(1, "agent") / "agent.log"
    log.parent.mkdir(parents=True)
    line = json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}})
    log.write_text("\n".join([json.dumps({"type": "system", "subtype": "init"})] + [line] * 8) + "\n")
    from autohelix.history import History, IterationResult
    History(tmp_path).append(IterationResult(1, True, calls=[{"role": "agent", "usage": {},
                                                           "log": str(log.relative_to(run_paths.run_dir))}]))
    detail = viewer_module.RunData(tmp_path).iteration(1)
    assert len(detail["agent"]) == 5  # status events are skipped, oldest results dropped
    assert all(e["kind"] == "tool_result" for e in detail["agent"])
    assert detail["agent_clipped"] is True


def test_idle_connection_does_not_block_other_requests(viewer):
    import socket
    host, port = viewer.removeprefix("http://").split(":")
    idle = socket.create_connection((host, int(port)))  # like a browser preconnect that sends nothing
    try:
        assert get(viewer + "/api/state")[0] == 200
    finally:
        idle.close()


def test_tampered_commit_is_never_passed_to_git(viewer, integration_project):  # noqa: F811
    history = RunPaths(integration_project).history
    rows = [json.loads(line) for line in history.read_text().splitlines()]
    rows[1]["commit"] = "--output=PWNED"
    history.write_text("".join(json.dumps(r) + "\n" for r in rows))
    (RunPaths(integration_project).copies / "iter-9").mkdir(parents=True)
    get(viewer + "/api/iteration/9")
    get(viewer + "/api/iteration/2")
    assert not list(integration_project.rglob("PWNED*"))


def test_preflight_failure_is_not_recorded_as_a_run(integration_project):  # noqa: F811
    (integration_project / "autohelix.yaml").write_text(
        (integration_project / "autohelix.yaml").read_text().replace("backend: mock", "backend: claude\n  command: /nonexistent/agent"))
    subprocess.run(["git", "commit", "-qam", "Use a missing agent"], cwd=integration_project, check=True)
    with pytest.raises(RuntimeError, match="Preflight checks failed"):
        Harness(integration_project)
    assert read_run_status(integration_project) is None
    assert not (RunPaths(integration_project).root / "current").exists()


def _snapshot(page: str) -> dict:
    start = page.index("window.AUTOHELIX_SNAPSHOT = ") + len("window.AUTOHELIX_SNAPSHOT = ")
    return json.loads(page[start:page.index(";</script>", start)])


def test_stopped_run_saves_static_page(integration_project):  # noqa: F811
    (integration_project / "hello.py").write_text('print("</script><b>")\n')  # text that must not break out
    subprocess.run(["git", "commit", "-qam", "tricky"], cwd=integration_project)
    Harness(integration_project).run(max_iterations=2)
    page = (RunPaths(integration_project).output / "report.html").read_text()
    for ref in ('src="/viewer.js"', 'href="/viewer.css"', 'src="/logo-light.svg"', 'href="/logo-light.svg"'):
        assert ref not in page  # everything is inlined
    data = _snapshot(page)
    routes = data["routes"]
    assert routes["/api/state"]["status"]["state"] == "finished"
    assert routes["/api/state"]["report"]["last_iteration"] == 2
    assert set(routes) >= {"/api/state", "/api/iteration/0", "/api/iteration/1", "/api/iteration/2"}
    assert all(routes[f"/api/iteration/{n}"]["agent"] == [] for n in (0, 1, 2))
    # One diff: baseline to the final code, on the last accepted iteration.
    assert routes["/api/iteration/1"]["diff"] is None
    assert "hello.py" in routes["/api/iteration/2"]["diff"]
    assert data["logos"]["dark"].startswith("data:image/svg+xml,%3Csvg")
    assert data["saved_at"] > 0  # shown as "Run report · saved HH:MM"
    assert len(page) < 300_000  # mostly the two logos; this run's data is small
    assert page.count("</script>") == 2  # the data script and the viewer script


def test_interrupted_run_still_saves_static_page(integration_project, monkeypatch):  # noqa: F811
    harness = Harness(integration_project)
    monkeypatch.setattr(harness, "run_iteration", lambda n: (_ for _ in ()).throw(KeyboardInterrupt))
    harness.run(max_iterations=1)
    page = (RunPaths(integration_project).output / "report.html").read_text()
    state = _snapshot(page)["routes"]["/api/state"]
    assert state["status"]["state"] == "interrupted"
    assert state["report"] is None


# ---------- dashboard ----------

def test_dashboard_lists_runs_and_serves_each_run(integration_project):  # noqa: F811
    first = Harness(integration_project)
    first.run(max_iterations=2, report=False)
    second = Harness(integration_project, new=True, name="second", land_mode="manual")
    second.run(max_iterations=1, report=False)
    server = make_server(integration_project, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert b"dashboard.js" in get(base + "/")[1]
        listing = json.loads(get(base + "/api/runs")[1])
        runs = {r["id"]: r for r in listing["runs"]}
        assert set(runs) == {first.paths.run_id, second.paths.run_id}
        one, two = runs[first.paths.run_id], runs[second.paths.run_id]
        assert (one["archived"], one["status"]["state"]) == (False, "finished")
        progress = one["progress"]
        assert (progress["iterations"], progress["max_iterations"], progress["max_cost_usd"]) == (2, 2, None)
        assert progress["seconds"] > 0
        assert one["landing"] == {"commits": 0, "fast_forward": True}  # auto-landed at the end of its segment
        assert two["landing"] == {"commits": 1, "fast_forward": True}  # manual: ready to land
        assert two["accepted"] == 1 and two["metrics"] is None
        assert two["config_file"] == "autohelix.yaml" and two["goal"] == "Test the integration"
        assert listing["schema"] == 1 and listing["path"] == str(integration_project.resolve())

        # A user commit on the target: the manual run can no longer fast-forward.
        (integration_project / "user.txt").write_text("mine\n")
        subprocess.run(["git", "add", "user.txt"], cwd=integration_project, check=True)
        subprocess.run(["git", "commit", "-qm", "user work"], cwd=integration_project, check=True)
        runs = {r["id"]: r for r in json.loads(get(base + "/api/runs")[1])["runs"]}
        assert runs[second.paths.run_id]["landing"] == {"commits": 1, "fast_forward": False}
        assert runs[first.paths.run_id]["landing"]["commits"] == 0

        # Each run has its own page and routes, independent of `current`.
        assert b"viewer.js" in get(base + f"/run/{first.paths.run_id}/")[1]
        state = json.loads(get(base + f"/api/run/{first.paths.run_id}/state")[1])
        assert state["run_id"] == first.paths.run_id and len(state["iterations"]) == 3
        assert json.loads(get(base + "/api/state")[1])["run_id"] == second.paths.run_id
        detail = json.loads(get(base + f"/api/run/{first.paths.run_id}/iteration/2")[1])
        assert "Modified in iteration 2" in detail["diff"]

        for path in ("/run/missing-zzzz/", "/api/run/missing-zzzz/state", "/run/../", "/api/run/..%2f/state",
                     f"/api/run/{first.paths.run_id}/history.jsonl", f"/run/{first.paths.run_id}/x"):
            assert get(base + path)[0] == 404, path
        assert get(base + "/api/runs", host="attacker.example")[0] == 403
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_tolerates_an_unreadable_run(tmp_path, run_paths):
    from autohelix.viewer import ProjectRuns

    broken = RunPaths(tmp_path).runs / "bad-aaaa"
    broken.mkdir(parents=True)
    (broken / "run.json").write_text("{not json")
    rows = {r["id"]: r for r in ProjectRuns(tmp_path).runs()["runs"]}
    assert "error" in rows["bad-aaaa"]
    assert "error" not in rows["test"] and rows["test"]["landing"] is None  # no target branch


def test_dashboard_metrics_are_the_latest_accepted_not_the_best():
    from autohelix.history import IterationResult
    from autohelix.viewer import ProjectRuns

    records = [
        IterationResult(iteration=0, accepted=True, metrics={"speed": 100, "mem": 50}),
        IterationResult(iteration=1, accepted=True, metrics={"speed": 300, "mem": 60}),
        IterationResult(iteration=2, accepted=True, metrics={"speed": 200, "mem": 40, "extra": 1}),
        IterationResult(iteration=3, accepted=False, metrics={"speed": 900, "mem": 10}),
        IterationResult(iteration=4, accepted=True, reason="recovered"),  # no measurement
    ]
    m = ProjectRuns._metrics(records, {"mem": "lower", "speed": "higher"})
    assert m["iteration"] == 2
    assert [(v["name"], v["value"], v["baseline"]) for v in m["values"]] == [
        ("mem", 40, 50), ("speed", 200, 100), ("extra", 1, None)]  # declared order first
    assert ProjectRuns._metrics(records[:1], {})["iteration"] == 0
    assert ProjectRuns._metrics([], {}) is None


# ---------- --json / --once / live ----------

def _cli(*args):
    from click.testing import CliRunner
    from autohelix.cli import main

    return CliRunner().invoke(main, list(map(str, args)))


@pytest.fixture
def measured_project(integration_project):  # noqa: F811
    """Three runs with a metric: one finished and landed, one manual and unlanded, one archived."""
    config = integration_project / "autohelix.yaml"
    config.write_text(config.read_text() + """
observe:
  - name: bench
    command: python -c "import pathlib; print('##autohelix[speed=%d]' % (100 + 10 * pathlib.Path('hello.py').read_text().count('iteration')))"
    metrics:
      speed: higher
""")
    subprocess.run(["git", "commit", "-qam", "metric"], cwd=integration_project, check=True)
    first = Harness(integration_project)
    first.run(max_iterations=2, report=False)
    second = Harness(integration_project, new=True, land_mode="manual")
    second.run(max_iterations=1, report=False)
    gone = Harness(integration_project, new=True, land_mode="manual")
    gone.run(max_iterations=1, report=False)
    archived = _cli("archive", gone.paths.run_id, "-p", integration_project)
    assert archived.exit_code == 0, archived.output
    return integration_project, first.paths.run_id, second.paths.run_id, gone.paths.run_id


def test_dashboard_json_and_once(measured_project):
    from autohelix.viewer import ProjectRuns

    project, first, second, gone = measured_project
    data = json.loads(_cli("dashboard", "-p", project, "--json").output)
    assert data.keys() == ProjectRuns(project).runs().keys()  # the page's payload, schema 1 (checked above)
    runs = {r["id"]: r for r in data["runs"]}
    assert set(runs) == {first, second, gone}  # archived runs included
    assert runs[first]["metrics"]["values"] == [
        {"name": "speed", "value": 120, "baseline": 100, "direction": "higher"}]
    assert runs[second]["landing"] == {"commits": 1, "fast_forward": True}

    result = _cli("dashboard", "-p", project, "--once")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith(f"test_project  ") and lines[0].endswith(" @ " + subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=project, capture_output=True, text=True).stdout.strip())
    assert lines[1].startswith("2 runs") and lines[1].endswith("1 archived (not listed; see --json)")
    assert lines[3].split() == ["RUN", "STATUS", "ITER", "speed", "LANDING", "ACTIVE", "GOAL"]
    rows = {line.split()[0]: line for line in lines[4:] if not line.startswith(" ")}
    assert set(rows) == {first, second}
    assert rows[first].split()[1:5] == ["finished", "2/2", "120.0", "landed"]
    assert rows[first].endswith("Test the integration")
    assert rows[second].split()[1:7] == ["finished", "1/1", "130.0", "1", "commit", "to"]
    assert "  autohelix.yaml" in result.output

    assert "no run has metric" in _cli("dashboard", "-p", project, "--once", "--metric", "nope").output.lower()
    assert _cli("dashboard", "-p", project, "--metric", "speed", "--json").exit_code == 2
    assert _cli("dashboard", "-p", project, "--json", "--once").exit_code == 2


def test_view_json_and_once(measured_project, monkeypatch):
    project, first, second, gone = measured_project
    data = json.loads(_cli("view", first, "-p", project, "--json").output)
    assert data["project"] == "test_project"
    run = data["run"]
    assert run["id"] == first and run["goal"] == "Test the integration"
    assert [(r["iteration"], r["accepted"], r["metrics"]) for r in run["iterations"]] == [
        (0, True, {"speed": 100}), (1, True, {"speed": 110}), (2, True, {"speed": 120})]
    # The default run is the selected one.
    assert json.loads(_cli("view", "-p", project, "--json").output)["run"]["id"] == RunPaths(project).run_id

    text = _cli("view", first, "-p", project, "--once").output
    lines = text.splitlines()
    assert lines[0] == f"{first} · finished"
    assert lines[1].startswith("autohelix.yaml · ") and lines[1].endswith(f"iterations 2/2 · landed ({data['branch']})")
    assert "Stopped: iteration budget reached (2)" in text
    assert "Goal: Test the integration" in text
    assert "Latest accepted (iteration 2): speed 120.0 ↑ 20.0%" in text
    table = lines[lines.index("") + 1:]
    assert table[0].split() == ["ITER", "OK", "speed", "COST", "TIME", "SUMMARY"]
    assert table[1].split()[:3] == ["0", "✓", "100.0"] and table[1].endswith("baseline")

    missing = _cli("view", "missing-zzzz", "-p", project, "--json")
    assert missing.exit_code == 1 and "missing-zzzz" in missing.output

    # --config is relative to the shell's folder, shown relative to the project.
    other = project / "other.yaml"
    other.write_text((project / "autohelix.yaml").read_text().replace("Test the integration", "  Other goal\n"))
    monkeypatch.chdir(project.parent)
    run = json.loads(_cli("view", first, "-p", project.name, "--json", "--config", f"{project.name}/other.yaml").output)["run"]
    assert (run["config_file"], run["goal"]) == ("other.yaml", "Other goal")
    assert run["metrics"]["values"][0]["direction"] == "higher"

    other.write_text((project / "autohelix.yaml").read_text().replace("Test the integration", '" "'))
    printed = _cli("view", first, "-p", project.name, "--once", "--config", f"{project.name}/other.yaml")
    assert printed.exit_code == 0 and "Goal:" not in printed.output


def test_table_shows_the_iteration_under_way():
    from autohelix.run_table import format_run, format_runs

    run = {"id": "r1", "config_file": "a.yaml", "accepted": 1, "target_branch": None,
           "landing": None, "last_activity": 990, "metrics": None, "goal": None,
           "status": {"state": "running", "phase": "checks", "iteration": 3,
                      "iteration_started_at": 900, "phase_started_at": 970},
           "progress": {"iterations": 2, "max_iterations": 5, "cost_usd": 1.5, "cost_incomplete": True,
                        "seconds": 60, "max_cost_usd": None, "max_seconds": None},
           "iterations": [{"iteration": 0, "accepted": True, "metrics": {}, "summary": None,
                           "cost": None, "cost_incomplete": False, "seconds": None}]}
    header = {"schema": 1, "project": "p", "host": "h", "path": "/x", "path_display": "/x", "branch": None,
              "commit": "abc1234"}
    listing = format_runs({**header, "runs": [run]}, now=1000).splitlines()
    assert listing[0] == "p  h:/x · detached @ abc1234"
    assert listing[4].split() == ["r1", "running", "·", "checks", "30s", "3/5", "—", "now"]
    assert listing[5].split() == ["a.yaml", "2m", "40s", "·", "$1.50+"]  # 60s done + 100s under way
    one = format_run({**header, "run": run}, now=1000).splitlines()
    assert one[0] == "r1 · running · checks 30s · iteration 3"
    assert one[-1].split() == ["3", "…", "running", "·", "checks", "30s"]


def test_live_view_shows_the_run_table_and_where_it_stopped(measured_project, monkeypatch):
    from tests.test_live import _stop_after

    project, first, second, gone = measured_project
    _stop_after(monkeypatch, polls=2)
    result = _cli("view", short_id(first), "-p", project)
    assert result.exit_code == 0, result.output
    assert f"{first} · finished" in result.output and "Goal: Test the integration" in result.output
    assert "ITER  OK  speed" in result.output and "Iteration 2 · agent" in result.output
    assert "Finished at " in result.output and "Stopped watching. The run is unaffected." in result.output
    # With --config, the table reads that config, as --once does.
    other = project / "other.yaml"
    other.write_text((project / "autohelix.yaml").read_text().replace("Test the integration", "Other goal"))
    _stop_after(monkeypatch)
    result = _cli("view", first, "-p", project, "--config", other)
    assert result.exit_code == 0 and "Goal: Other goal" in result.output, result.output


def test_pin_is_announced_before_the_checks_copy(integration_project, monkeypatch):  # noqa: F811
    phases = []
    original = RunStatus.phase
    monkeypatch.setattr(RunStatus, "phase", lambda self, phase, iteration=None: (
        phases.append(phase), original(self, phase, iteration))[1])
    Harness(integration_project).run(max_iterations=1, report=False)
    assert phases.index("agent") < phases.index("preparing checks") < phases.index("checks")
