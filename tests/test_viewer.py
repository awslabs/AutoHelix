"""Tests for live run status and the `autohelix view` server."""

import json
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from autohelix.agents import AgentRunResult
from autohelix.harness import AutoHelixRunError, Harness
from autohelix.run_status import RunStatus, read_run_status
from autohelix.viewer import make_server
from autohelix.runs import RunPaths
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
    monkeypatch.setattr(harness, "run_agent", lambda *a, **k: AgentRunResult(success=False, exit_code=1))
    with pytest.raises(AutoHelixRunError):
        harness.run(max_iterations=1)
    assert read_run_status(integration_project)["state"] == "failed"


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
    assert state["config"]["checks"] == ["true"]
    assert "goal: Test the integration" in state["config"]["text"]
    # The run finished, so it wrote a summary (the mock agent's fixed text).
    assert state["report"]["html"] == "<p>Mock summary of the run.</p>\n"
    assert state["report"]["last_iteration"] == 2


def test_iteration_detail_has_notes_and_diff(viewer):
    detail = json.loads(get(viewer + "/api/iteration/2")[1])
    assert "<strong>the obvious</strong>" in detail["notes"]
    assert "<script>" not in detail["notes"]  # Raw HTML is escaped, not rendered.
    assert "Modified in iteration 2" in detail["diff"]
    assert detail["review"] is None
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
    obs = RunPaths(integration_project).observations / "iter-2"
    obs.mkdir(parents=True, exist_ok=True)
    (obs / "page.html").write_text("<script>alert(1)</script>")
    (obs / "plot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    state = json.loads(get(viewer + "/api/state")[1])
    assert {"page.html", "plot.png"} <= {a["name"] for a in state["artifacts"]["2"]}

    request = urllib.request.Request(viewer + "/api/artifact/2/page.html")
    with urllib.request.urlopen(request) as response:
        assert response.headers["Content-Type"].startswith("text/plain")  # never rendered as HTML
    with urllib.request.urlopen(viewer + "/api/artifact/2/plot.png") as response:
        assert response.headers["Content-Type"] == "image/png"
    assert get(viewer + "/api/artifact/2/..%2F..%2Fhistory.jsonl")[0] == 404


def test_diff_is_relative_to_baseline(viewer):
    one = json.loads(get(viewer + "/api/iteration/1")[1])["diff"]
    two = json.loads(get(viewer + "/api/iteration/2")[1])["diff"]
    assert "Modified in iteration 1" in one and "Modified in iteration 2" not in one
    assert "+# Modified in iteration 1" in two and "+# Modified in iteration 2" in two


def test_running_iteration_shows_copy_diff(viewer, integration_project):  # noqa: F811
    from autohelix.workspace import Copy, Repo

    paths = RunPaths(integration_project)
    paths.worktrees.mkdir(parents=True, exist_ok=True)
    copy = Copy.create(Repo(integration_project), paths.worktrees / "iter-3-agent",
                       files="HEAD", head="HEAD")
    private_index = (copy.path / ".git" / "index").read_bytes()
    (copy.path / "hello.py").write_text('print("work in progress")\n')
    (copy.path / "brand_new.py").write_text("NEW = 1\n")  # untracked: plain git diff would miss it
    diff = json.loads(get(viewer + "/api/iteration/3")[1])["diff"]
    assert '+print("work in progress")' in diff
    assert "+NEW = 1" in diff
    assert (copy.path / ".git" / "index").read_bytes() == private_index
    copy.remove()


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
    (RunPaths(integration_project).worktrees / "iter-9").mkdir(parents=True)
    get(viewer + "/api/iteration/9")
    get(viewer + "/api/iteration/2")
    assert not list(integration_project.rglob("PWNED*"))


def test_preflight_failure_is_not_recorded_as_a_run(integration_project):  # noqa: F811
    (integration_project / "autohelix.yaml").write_text(
        (integration_project / "autohelix.yaml").read_text().replace("type: mock", "type: claude\n  command: /nonexistent/agent"))
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
    assert routes["/api/state"]["artifacts"] == {}
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
        assert (one["lifecycle"], one["status"]["state"]) == ("open", "finished")
        progress = one["progress"]
        assert (progress["iterations"], progress["max_iterations"], progress["max_cost_usd"]) == (2, 2, None)
        assert progress["seconds"] > 0
        assert one["landing"] == {"commits": 0, "fast_forward": True}  # auto-landed at the end of its segment
        assert two["landing"] == {"commits": 1, "fast_forward": True}  # manual: ready to land
        assert two["accepted"] == 1 and two["metrics"] is None
        assert two["config"]["file"] == "autohelix.yaml"

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
