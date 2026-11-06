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
from tests.test_integration import integration_project  # noqa: F401  (fixture)


# ---------- run status ----------

def test_run_status_lifecycle(tmp_path):
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


def test_dead_process_reads_as_crashed(tmp_path):
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
    root = integration_project / ".autohelix"
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
    obs = integration_project / ".autohelix" / "observations" / "iter-2"
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


def test_running_iteration_shows_worktree_diff(viewer, integration_project):  # noqa: F811
    worktree = integration_project / ".autohelix" / "worktrees" / "iter-3"
    subprocess.run(["git", "worktree", "add", "--detach", str(worktree)], cwd=integration_project,
                   check=True, capture_output=True)
    (worktree / "hello.py").write_text('print("work in progress")\n')
    (worktree / "brand_new.py").write_text("NEW = 1\n")  # untracked: plain git diff would miss it
    diff = json.loads(get(viewer + "/api/iteration/3")[1])["diff"]
    assert '+print("work in progress")' in diff
    assert "+NEW = 1" in diff


def test_long_logs_are_clipped_visibly(tmp_path, monkeypatch):
    import autohelix.viewer as viewer_module
    monkeypatch.setattr(viewer_module, "MAX_EVENTS", 5)
    log = tmp_path / ".autohelix" / "logs" / "iter-1" / "agent.log"
    log.parent.mkdir(parents=True)
    line = json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}})
    log.write_text("\n".join([json.dumps({"type": "system", "subtype": "init"})] + [line] * 8) + "\n")
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
    history = integration_project / ".autohelix" / "history.jsonl"
    rows = [json.loads(line) for line in history.read_text().splitlines()]
    rows[1]["commit"] = "--output=PWNED"
    history.write_text("".join(json.dumps(r) + "\n" for r in rows))
    (integration_project / ".autohelix" / "worktrees" / "iter-9").mkdir(parents=True)
    get(viewer + "/api/iteration/9")
    get(viewer + "/api/iteration/2")
    assert not list(integration_project.rglob("PWNED*"))


def test_preflight_failure_is_not_recorded_as_a_run(integration_project):  # noqa: F811
    (integration_project / "autohelix.yaml").write_text(
        (integration_project / "autohelix.yaml").read_text().replace("type: mock", "type: claude\n  command: /nonexistent/agent"))
    subprocess.run(["git", "commit", "-qam", "Use a missing agent"], cwd=integration_project, check=True)
    harness = Harness(integration_project)
    preflight = []
    original = harness.console.print
    harness.console.print = lambda *a, **k: (preflight.append(str(a)), original(*a, **k))
    harness.run(max_iterations=1)
    assert any("Preflight checks failed" in line for line in preflight)
    assert read_run_status(integration_project) is None
