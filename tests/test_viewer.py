"""Tests for live run status and the `autohelix view` server."""

import hashlib
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from autohelix.agents import AgentRunResult
from autohelix.harness import Budget, AutoHelixRunError, Harness
from autohelix.run_status import RunStatus, read_run_status
from autohelix.runs import RunLock
from autohelix.viewer import make_server
from autohelix.runs import RunPaths, short_id
from tests.test_integration import integration_project  # noqa: F401  (fixture)
from tests.helpers import latest_run


# ---------- run status ----------

def test_run_status_lifecycle(tmp_path, run_paths):
    status = RunStatus(tmp_path, latest_run(tmp_path).run_id)
    status.lock = RunLock(latest_run(tmp_path).lock)  # a running segment holds the run's lock
    status.phase("agent", 1)  # Before start: ignored, writes nothing.
    assert read_run_status(tmp_path, latest_run(tmp_path).run_id) is None

    status.start(max_iterations=3)
    status.phase("agent", 2)
    data = read_run_status(tmp_path, latest_run(tmp_path).run_id)
    assert (data["state"], data["iteration"], data["phase"]) == ("running", 2, "agent")

    status.stop("interrupted")
    status.stop("finished")  # First stop wins.
    assert read_run_status(tmp_path, latest_run(tmp_path).run_id)["state"] == "interrupted"


def test_dead_process_reads_as_crashed(tmp_path, run_paths):
    status = RunStatus(tmp_path, latest_run(tmp_path).run_id)
    status.start()
    data = json.loads(status.path.read_text())
    proc = subprocess.Popen(["true"])
    proc.wait()
    data["pid"] = proc.pid  # A pid that has exited.
    status.path.write_text(json.dumps(data))
    assert read_run_status(tmp_path, latest_run(tmp_path).run_id)["state"] == "crashed"


def test_a_running_status_is_running_only_while_its_lock_is_held(tmp_path, run_paths):
    """Liveness comes from the run lock, not the pid: a reused pid (any live process,
    another user's too) used to keep a dead run "running" forever."""
    status = RunStatus(tmp_path, run_paths.run_id)
    status.start()
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        data = json.loads(status.path.read_text())
        data["pid"] = sleeper.pid  # alive, but not the run's process
        status.path.write_text(json.dumps(data))
        assert read_run_status(tmp_path, run_paths.run_id)["state"] == "crashed"  # nobody holds the lock
        data["pid"] = 1  # a root process: the pid probe's PermissionError counted it alive
        status.path.write_text(json.dumps(data))
        assert read_run_status(tmp_path, run_paths.run_id)["state"] == "crashed"
        # Another process holding the lock: running, whatever the pid.
        run_paths.lock.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl, sys, time; f = open(sys.argv[1], 'a+'); "
                                   "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(30)",
                                   str(run_paths.lock)], stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline() == "held\n"
            assert read_run_status(tmp_path, run_paths.run_id)["state"] == "running"
        finally:
            holder.kill()
            holder.wait()
        assert read_run_status(tmp_path, run_paths.run_id)["state"] == "crashed"
        # This process holding the lock (a resume reading the segment it continues): only
        # its own pid is running.
        with RunLock(run_paths.lock):
            assert read_run_status(tmp_path, run_paths.run_id)["state"] == "crashed"
            data["pid"] = os.getpid()
            status.path.write_text(json.dumps(data))
            assert read_run_status(tmp_path, run_paths.run_id)["state"] == "running"
    finally:
        sleeper.kill()
        sleeper.wait()


def test_the_liveness_probe_creates_no_lock_file(tmp_path, run_paths):
    status = RunStatus(tmp_path, run_paths.run_id)
    status.start()
    assert read_run_status(tmp_path, run_paths.run_id)["state"] == "crashed"
    assert not run_paths.lock.exists()


def test_a_probe_holding_a_free_lock_for_a_moment_doesnt_refuse_the_run(tmp_path, run_paths):
    """`is_live` takes a free lock briefly; `RunLock` (resume, land, delete) tries again
    for a moment instead of saying the run is running. A lock held longer refuses."""
    import fcntl
    import threading

    run_paths.lock.parent.mkdir(parents=True, exist_ok=True)
    run_paths.lock.touch()
    probe = open(run_paths.lock)
    fcntl.flock(probe, fcntl.LOCK_EX)  # what the probe does, held across RunLock's first try
    timer = threading.Timer(0.02, probe.close)
    timer.start()
    try:
        with RunLock(run_paths.lock):
            pass
    finally:
        timer.join()
    with open(run_paths.lock) as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        with pytest.raises(RuntimeError, match="is running"):
            RunLock(run_paths.lock)


def test_harness_records_finished_run(integration_project):  # noqa: F811
    Harness(integration_project).run(Budget(until=1))
    data = read_run_status(integration_project, latest_run(integration_project).run_id)
    assert data["state"] == "finished"
    assert data["iteration"] == 1


def test_harness_records_failed_run(integration_project, monkeypatch):  # noqa: F811
    harness = Harness(integration_project)

    def broken(*args, **kwargs):  # the harness itself failing, not the agent (that's a rejection)
        raise OSError("disk full")

    monkeypatch.setattr(harness, "run_agent", broken)
    with pytest.raises(AutoHelixRunError):
        harness.run(Budget(until=1))
    assert read_run_status(integration_project, latest_run(integration_project).run_id)["state"] == "failed"
    # A failed agent call is a rejected iteration: the run finishes normally.
    harness = Harness(integration_project)
    monkeypatch.setattr(harness, "run_agent", lambda *a, **k: AgentRunResult(success=False, exit_code=1))
    harness.run(Budget(until=2))
    assert read_run_status(integration_project, latest_run(integration_project).run_id)["state"] == "finished"


# ---------- viewer server ----------

@pytest.fixture
def viewer(integration_project):  # noqa: F811
    Harness(integration_project).run(Budget(until=2))
    root = latest_run(integration_project).run_dir
    (root / "notes" / "iter-2.md").write_text("Tried **the obvious** thing.\n\n<script>alert(1)</script>")
    server = make_server(integration_project, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield Viewer(f"http://127.0.0.1:{server.server_port}", latest_run(integration_project).run_id)
    server.shutdown()
    server.server_close()


class Viewer(str):
    """The server's base URL; `.api` is the run's routes, `/api/run/<id>` (the only ones)."""

    def __new__(cls, base: str, run_id: str):
        url = super().__new__(cls, base)
        url.api = f"{base}/api/run/{run_id}"
        return url


def get(url, host=None):
    request = urllib.request.Request(url, headers={"Host": host} if host else {})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_state_lists_iterations_and_status(viewer):
    status, body = get(viewer.api + "/state")
    state = json.loads(body)
    assert status == 200
    assert [r["iteration"] for r in state["iterations"]] == [0, 1, 2]
    assert all(r["accepted"] for r in state["iterations"])
    assert state["status"]["state"] == "finished"
    assert state["goal"].startswith("Test the integration")
    assert state["config"]["file"] == "autohelix.yaml"
    assert state["config"]["gates"] == ["true"]
    assert state["goal"].startswith("Test the integration")
    assert "goal: Test the integration" in state["config"]["text"]
    # The run finished, so it wrote a summary (the mock agent's fixed text).
    assert state["report"]["html"] == "<p>Mock summary of the run.</p>\n"
    assert state["report"]["last_iteration"] == 2


def test_iteration_detail_has_notes_and_diff(viewer):
    detail = json.loads(get(viewer.api + "/iteration/2")[1])
    assert "<strong>the obvious</strong>" in detail["notes"]
    assert "<script>" not in detail["notes"]  # Raw HTML is escaped, not rendered.
    assert "Modified in iteration 2" in detail["diff"]  # vs baseline
    assert "Modified in iteration 2" in json.loads(get(viewer.api + "/iteration/2/diff")[1])["diff"]  # its own
    assert [(g["text"], g["passed"]) for g in detail["gates"]] == [("true", True)]
    assert detail["observe"] == [] and detail["discarded"] == []
    assert isinstance(detail["agent"], list)


def test_static_assets_served(viewer):
    for path in ("/", "/viewer.js", "/viewer.css", "/logo-light.svg"):
        assert get(viewer + path)[0] == 200


def test_rejects_non_loopback_host(viewer):
    assert get(viewer.api + "/state", host="attacker.example")[0] == 403
    # SSH forwards may use any local port; only the host name matters.
    assert get(viewer.api + "/state", host="localhost:9999")[0] == 200


def test_no_arbitrary_file_routes(viewer):
    for path in ("/history.jsonl", "/../autohelix.yaml", "/api/iteration/..%2fhistory", "/api/file"):
        assert get(viewer + path)[0] == 404
    for path in ("/iteration/..%2fhistory", "/file", "/history.jsonl"):
        assert get(viewer.api + path)[0] == 404


def test_read_only(viewer):
    request = urllib.request.Request(viewer.api + "/state", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 501


def test_notes_list_and_note_route(viewer):
    state = json.loads(get(viewer.api + "/state")[1])
    assert "iter-2.md" in state["notes_files"]
    note = json.loads(get(viewer.api + "/notes/iter-2.md")[1])
    assert "<strong>the obvious</strong>" in note["html"]
    assert get(viewer.api + "/notes/..%2Fhistory.jsonl")[0] == 404
    assert get(viewer.api + "/notes/missing.md")[0] == 404


def test_artifacts_listed_and_served_safely(viewer, integration_project):  # noqa: F811
    obs = latest_run(integration_project).observe_entry(2, "bench")
    obs.mkdir(parents=True, exist_ok=True)
    (obs / "page.html").write_text("<script>alert(1)</script>")
    (obs / "plot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    sections = json.loads(get(viewer.api + "/iteration/2")[1])["observe"]
    assert {"bench/page.html", "bench/plot.png"} <= {a["path"] for a in sections[0]["files"]}

    request = urllib.request.Request(viewer.api + "/artifact/2/bench/page.html")
    with urllib.request.urlopen(request) as response:
        assert response.headers["Content-Type"].startswith("text/plain")  # never rendered as HTML
    with urllib.request.urlopen(viewer.api + "/artifact/2/bench/plot.png") as response:
        assert response.headers["Content-Type"] == "image/png"
    assert get(viewer.api + "/artifact/2/..%2F..%2Fhistory.jsonl")[0] == 404
    assert get(viewer.api + "/artifact/2/bench/../../../history.jsonl")[0] == 404


def test_each_iteration_shows_its_own_change(viewer):
    one = json.loads(get(viewer.api + "/iteration/1/diff")[1])["diff"]
    two = json.loads(get(viewer.api + "/iteration/2/diff")[1])["diff"]
    assert "+# Modified in iteration 1" in one and "Modified in iteration 2" not in one
    assert "+# Modified in iteration 2" in two and two != one


def test_running_iteration_shows_copy_diff(viewer, integration_project):  # noqa: F811
    from autohelix.runs import ensure_copies
    from autohelix.workspace import Copy, Repo

    paths = latest_run(integration_project)
    ensure_copies(paths.copies)
    copy = Copy.create(Repo(integration_project), paths.copies / "iter-3-agent",
                       files="HEAD", head="HEAD")
    private_index = (copy.path / ".git" / "index").read_bytes()
    (copy.path / "hello.py").write_text('print("work in progress")\n')
    (copy.path / "brand_new.py").write_text("NEW = 1\n")  # untracked: plain git diff would miss it
    (copy.path / ".autohelix-iteration" / "notes").mkdir(parents=True)  # the harness's material
    (copy.path / ".autohelix-iteration" / "notes" / "iter-3.md").write_text("SEEDED NOTE\n")
    diff = json.loads(get(viewer.api + "/iteration/3")[1])["diff"]
    assert '+print("work in progress")' in diff
    assert "+NEW = 1" in diff
    assert "SEEDED NOTE" not in diff and ".autohelix-iteration" not in diff
    assert (copy.path / ".git" / "index").read_bytes() == private_index
    copy.remove()


def test_live_diffs_write_nothing_into_the_users_objects(viewer, integration_project):  # noqa: F811
    from autohelix.runs import ensure_copies
    from autohelix.workspace import Copy, Repo

    def objects():
        git_dir = Path(subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=integration_project,
                                      capture_output=True, text=True, check=True).stdout.strip())
        return [p.relative_to(git_dir) for p in (git_dir / "objects").rglob("*") if p.is_file()]

    paths = latest_run(integration_project)
    ensure_copies(paths.copies)
    copy = Copy.create(Repo(integration_project), paths.copies / "iter-3-agent", files="HEAD", head="HEAD")
    before = objects()
    (copy.path / "hello.py").write_text('print("work in progress")\n')
    (copy.path / "big.bin").write_bytes(os.urandom(256 * 1024))  # untracked
    (copy.path / "odd [*] name.py").write_text("ODD = 1\n")  # not a pathspec
    for k in range(3):
        (copy.path / "brand_new.py").write_text(f"NEW = {k}\n")
        diff = json.loads(get(viewer.api + "/iteration/3")[1])["diff"]
        assert f"+NEW = {k}" in diff and '+print("work in progress")' in diff and "+ODD = 1" in diff
    (copy.path / "brand_new.py").unlink()  # gone again: no longer in the diff
    assert "brand_new.py" not in json.loads(get(viewer.api + "/iteration/3")[1])["diff"]
    # At most git's empty blob, which `add -N` records for a new file (once, 15 bytes).
    assert set(objects()) - set(before) <= {Path("objects/e6/9de29bb2d1d6434b8b29ae775ad8c2e48c5391")}
    copy.remove()


def test_viewer_index_is_private_and_one_per_run(viewer, integration_project):  # noqa: F811
    from autohelix.runs import ensure_copies
    from autohelix.workspace import Copy, Repo, _private_index_dir

    paths = latest_run(integration_project)
    ensure_copies(paths.copies)
    for n in (3, 4):
        copy = Copy.create(Repo(integration_project), paths.copies / f"iter-{n}-agent", files="HEAD", head="HEAD")
        get(viewer.api + f"/iteration/{n}")
        copy.remove()
    # Removed at process exit with the harness indexes; an earlier iteration's is dropped.
    run = hashlib.sha256(os.fsencode(paths.copies)).hexdigest()[:16]
    assert [p.name for p in _private_index_dir().glob(f"view-{run}-*")] == [f"view-{run}-iter-4-agent.index"]


@pytest.mark.parametrize("phase, shown", [("preparing checks", True), ("checks", True), ("review", True),
                                           ("agent", False), ("between iterations", False)])
def test_frozen_candidate_diff_only_once_this_iteration_froze(viewer, integration_project, phase, shown):  # noqa: F811
    # As if a segment were live: the pin holds iteration 2's tree; iteration 3 has no copy yet.
    paths = latest_run(integration_project)
    tree = subprocess.run(["git", "rev-parse", f"{paths.ref}^{{tree}}"], cwd=integration_project,
                          capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "update-ref", paths.pin, tree], cwd=integration_project, check=True)
    status = json.loads(paths.status.read_text())
    paths.status.write_text(json.dumps({**status, "iteration": 3, "phase": phase}))
    diff = json.loads(get(viewer.api + "/iteration/3")[1])["diff"]
    assert (diff is not None and "Modified in iteration 2" in diff) is shown


def test_long_logs_are_clipped_visibly(tmp_path, monkeypatch, run_paths):
    import autohelix.viewer as viewer_module
    monkeypatch.setattr(viewer_module, "MAX_EVENTS", 5)
    log = run_paths.agent_log(1)
    log.parent.mkdir(parents=True)
    line = json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}})
    log.write_text("\n".join([json.dumps({"type": "system", "subtype": "init"})] + [line] * 8) + "\n")
    from autohelix.history import History, IterationResult
    History(tmp_path, latest_run(tmp_path).run_id).append(IterationResult(1, True, calls=[{"role": "agent", "usage": {},
                                                           "log": str(log.relative_to(run_paths.run_dir))}]))
    detail = viewer_module.RunData(tmp_path, latest_run(tmp_path).run_id).iteration(1)
    assert len(detail["agent"]) == 5  # status events are skipped, oldest results dropped
    assert all(e["kind"] == "tool_result" for e in detail["agent"])
    assert detail["agent_clipped"] is True


def test_idle_connection_does_not_block_other_requests(viewer):
    import socket
    host, port = viewer.removeprefix("http://").split(":")
    idle = socket.create_connection((host, int(port)))  # like a browser preconnect that sends nothing
    try:
        assert get(viewer.api + "/state")[0] == 200
    finally:
        idle.close()


def test_tampered_commit_is_never_passed_to_git(viewer, integration_project):  # noqa: F811
    history = latest_run(integration_project).history
    rows = [json.loads(line) for line in history.read_text().splitlines()]
    rows[1]["commit"] = "--output=PWNED"
    history.write_text("".join(json.dumps(r) + "\n" for r in rows))
    (latest_run(integration_project).copies / "iter-9").mkdir(parents=True)
    get(viewer.api + "/iteration/9")
    get(viewer.api + "/iteration/2")
    assert not list(integration_project.rglob("PWNED*"))


def test_preflight_failure_is_not_recorded_as_a_run(integration_project):  # noqa: F811
    (integration_project / "autohelix.yaml").write_text(
        (integration_project / "autohelix.yaml").read_text().replace("backend: mock", "backend: claude\n  command: /nonexistent/agent"))
    subprocess.run(["git", "commit", "-qam", "Use a missing agent"], cwd=integration_project, check=True)
    with pytest.raises(RuntimeError, match="Preflight checks failed"):
        Harness(integration_project)
    assert read_run_status(integration_project, latest_run(integration_project).run_id) is None
    assert not list(RunPaths(integration_project).runs.glob("*/run.json"))


def _snapshot(page: str) -> dict:
    start = page.index("window.AUTOHELIX_SNAPSHOT = ") + len("window.AUTOHELIX_SNAPSHOT = ")
    return json.loads(page[start:page.index(";</script>", start)])


def test_stopped_run_saves_static_page(integration_project):  # noqa: F811
    (integration_project / "hello.py").write_text('print("</script><b>")\n')  # text that must not break out
    subprocess.run(["git", "commit", "-qam", "tricky"], cwd=integration_project)
    Harness(integration_project).run(Budget(until=2))
    page = latest_run(integration_project).report_html.read_text()
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
    assert page.count("</script>") == 3  # the data, common.js and viewer.js


def test_interrupted_run_still_saves_static_page(integration_project, monkeypatch):  # noqa: F811
    harness = Harness(integration_project)
    monkeypatch.setattr(harness, "run_iteration", lambda n: (_ for _ in ()).throw(KeyboardInterrupt))
    harness.run(Budget(until=1))
    page = latest_run(integration_project).report_html.read_text()
    state = _snapshot(page)["routes"]["/api/state"]
    assert state["status"]["state"] == "interrupted"
    assert state["report"] is None


# ---------- dashboard ----------

def test_dashboard_lists_runs_and_serves_each_run(integration_project):  # noqa: F811
    first = Harness(integration_project)
    first.run(Budget(until=2), report=False)
    second = Harness(integration_project, new=True, name="second", land_mode="manual")
    second.run(Budget(until=1, cost_usd=5, time_seconds=3600), report=False)
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
        assert progress["seconds"] > 0 and progress["max_seconds"] is None
        # Budget figures come from the run's status (its latest segment's flags: iterations
        # and cost as run totals, time as the segment's own), never from the config.
        assert (two["progress"]["max_iterations"], two["progress"]["max_cost_usd"],
                two["progress"]["max_seconds"]) == (1, 5, 3600)
        assert (two["status"]["max_cost_usd"], two["status"]["max_time_seconds"]) == (5, 3600)
        assert two["status"]["segment_started_at"] <= two["status"]["stopped_at"]
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

        # Each run has its own page and routes.
        assert b"viewer.js" in get(base + f"/run/{first.paths.run_id}/")[1]
        state = json.loads(get(base + f"/api/run/{first.paths.run_id}/state")[1])
        assert state["run_id"] == first.paths.run_id and len(state["iterations"]) == 3
        assert get(base + "/api/state")[0] == 404  # no unprefixed "default run" routes
        detail = json.loads(get(base + f"/api/run/{first.paths.run_id}/iteration/2/diff")[1])
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

    return CliRunner(env={"COLUMNS": "200"}).invoke(main, list(map(str, args)))  # long paths fit on a line


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
    first.run(Budget(until=2), report=False)
    second = Harness(integration_project, new=True, land_mode="manual")
    second.run(Budget(until=1), report=False)
    gone = Harness(integration_project, new=True, land_mode="manual")
    gone.run(Budget(until=1), report=False)
    archived = _cli("archive", gone.paths.run_id, "-p", integration_project)
    assert archived.exit_code == 0, archived.output
    return integration_project, first.paths.run_id, second.paths.run_id, gone.paths.run_id


def test_runs_json_and_once(measured_project):
    from autohelix.viewer import ProjectRuns

    project, first, second, gone = measured_project
    data = json.loads(_cli("view", "-p", project, "--json").output)
    assert data.keys() == ProjectRuns(project).runs().keys()  # the page's payload, schema 1 (checked above)
    runs = {r["id"]: r for r in data["runs"]}
    assert set(runs) == {first, second, gone}  # archived runs included
    assert runs[first]["metrics"]["values"] == [
        {"name": "speed", "value": 120, "baseline": 100, "direction": "higher"}]
    assert runs[second]["landing"] == {"commits": 1, "fast_forward": True}

    result = _cli("view", "-p", project, "--once")
    assert result.exit_code == 0, result.output
    lines = [line.rstrip() for line in result.output.splitlines()]
    assert lines[0].startswith("  AutoHelix  v") and lines[0].endswith(" · dashboard · test_project")
    assert lines[1].startswith("  Project    ") and lines[1].endswith(" @ " + subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=project, capture_output=True, text=True).stdout.strip())
    assert lines[2].startswith("  Runs       2 runs") and lines[2].endswith("1 archived (not listed; see --json)")
    assert lines[4].split() == ["run", "state", "iter", "speed", "↑", "landing", "cost", "activity", "goal"]
    rows = {line.split()[0]: line for line in lines[5:]}
    assert set(rows) == {short_id(first), short_id(second)}  # one line each
    assert rows[short_id(first)].split()[1:5] == ["finished", "2/2", "120", "landed"]
    assert rows[short_id(first)].endswith("Test the integration")
    assert rows[short_id(second)].split()[1:6] == ["finished", "1/1", "130", "1", "to"]

    assert "no run has metric" in _cli("view", "-p", project, "--once", "--metric", "nope").output.lower()
    assert _cli("view", "-p", project, "--metric", "speed", "--json").exit_code == 2
    assert _cli("view", "-p", project, "--json", "--once").exit_code == 2


def test_view_json_and_once(measured_project, monkeypatch):
    from tests.test_live import _stop_after

    project, first, second, gone = measured_project
    data = json.loads(_cli("view", first, "-p", project, "--json").stdout)
    assert data["project"] == "test_project"
    run = data["run"]
    assert run["id"] == first and run["goal"] == "Test the integration"
    assert [(r["iteration"], r["accepted"], r["metrics"]) for r in run["iterations"]] == [
        (0, True, {"speed": 100}), (1, True, {"speed": 110}), (2, True, {"speed": 120})]
    # --terminal without an id: the most recently active run that isn't archived (not
    # `gone`, the last one started), named on stderr.
    _stop_after(monkeypatch)
    chosen = _cli("view", "--terminal", "-p", project)
    assert f"Run {short_id(second)} (the most recently active run" in chosen.stderr
    assert f"run {short_id(second)} · " in chosen.stdout

    text = _cli("view", first, "-p", project, "--once").output
    lines = [line.rstrip() for line in text.splitlines()]
    assert lines[0].startswith("  AutoHelix  v") and lines[0].endswith(f" · run {short_id(first)} · test_project · finished")
    assert "  Goal       Test the integration" in lines
    rows = lines[lines.index(next(line for line in lines if line.lstrip().startswith("time"))) + 1:]
    assert rows[0].split() == ["·", "base", "100"]
    assert rows[1].split()[:2] == ["✓", "1"] and rows[1].split()[-2:] == ["110", "+10%"]
    assert "  Done       iteration budget reached (2)" in lines
    assert "  speed      100 → 120  +20%  iter 2" in lines
    assert f"  landed     2 commits on {data['branch']}" in lines
    assert f"  next       autohelix resume {short_id(first)}" in lines
    manual = [line.rstrip() for line in _cli("view", second, "-p", project, "--once").output.splitlines()]
    assert f"  unlanded   1 commit on autohelix/{second} (manual landing)" in manual  # as run says it
    assert f"  next       autohelix resume {short_id(second)} · autohelix land {short_id(second)}" in manual

    missing = _cli("view", "missing-zzzz", "-p", project, "--json")
    assert missing.exit_code == 1 and "missing-zzzz" in missing.output

    # The table reads the run's stored config, not the file as it is now.
    config = project / "autohelix.yaml"
    config.write_text(config.read_text().replace("Test the integration", '" "'))
    printed = _cli("view", first, "-p", project, "--once")
    assert printed.exit_code == 0 and "  Goal       Test the integration\n" in printed.output
    stored = RunPaths(project, first).config
    stored.chmod(0o644)  # stored read-only
    stored.write_text(stored.read_text().replace("Test the integration", '" "'))
    printed = _cli("view", first, "-p", project, "--once")
    assert printed.exit_code == 0 and "Goal" not in printed.output


def test_table_shows_the_iteration_under_way():
    from autohelix.run_table import Saved, format_run, format_runs

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
    assert listing[1] == "  Project    /x · detached @ abc1234"
    assert listing[5].split() == ["r1", "running", "3/5", "—", "$1.50+", "gates", "30s"]  # activity: what it is doing
    assert len(listing) == 6  # one line per run
    one = format_run({**header, "run": run}, Saved(), now=1000).splitlines()
    assert one[0].endswith(" · run r1 · p · running")
    assert one[-1].split() == ["⠋", "3", "1m", "40s", "gates"]  # the iteration's own time so far


def test_live_view_shows_the_run_table_and_where_it_stopped(measured_project, monkeypatch):
    from tests.test_live import _stop_after

    project, first, second, gone = measured_project
    monkeypatch.setenv("LINES", "60")  # room for the run's screen and the log under it
    _stop_after(monkeypatch, polls=2)
    result = _cli("view", short_id(first), "--terminal", "-p", project)
    assert result.exit_code == 0, result.output
    assert f"run {short_id(first)} · test_project · finished" in result.output
    assert "  Goal       Test the integration" in result.output
    assert "Done       iteration budget reached (2)" in result.output and "Iteration 2 · agent" in result.output
    assert "Finished at " in result.output and "Stopped watching. The run is unaffected." in result.output


def test_pin_is_announced_before_the_checks_copy(integration_project, monkeypatch):  # noqa: F811
    phases = []
    original = RunStatus.phase
    monkeypatch.setattr(RunStatus, "phase", lambda self, phase, iteration=None: (
        phases.append(phase), original(self, phase, iteration))[1])
    Harness(integration_project).run(Budget(until=1), report=False)
    assert phases.index("agent") < phases.index("preparing checks") < phases.index("checks")
