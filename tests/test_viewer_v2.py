"""The viewer's config-v2 data: observations by entry, gates, summaries, discarded
paths, per-iteration diffs and titles (web API and terminal tables)."""

import dataclasses
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from autohelix import run_table
from autohelix.history import History, IterationResult
from autohelix.live import fit
from autohelix.runs import RunPaths, write_json
from autohelix.viewer import ProjectRuns, RunData, make_server, review_markdown

FUTURE = {"summary", "base", "tree", "discarded", "rejected_ref"}


@pytest.fixture(autouse=True)
def future_fields(monkeypatch):
    """Records keep the fields the harness is adding (summary, base, tree, discarded,
    rejected_ref) even while `IterationResult` doesn't declare them yet."""
    missing = FUTURE - {f.name for f in dataclasses.fields(IterationResult)}
    if not missing:
        return
    original = History.load

    def load(self):
        results = original(self)
        raw = {}
        if self.history_path.exists():
            for line in self.history_path.read_text().splitlines():
                if line.strip():
                    data = json.loads(line)
                    raw[data["iteration"]] = data
        for result in results:
            for name in missing:
                setattr(result, name, raw.get(result.iteration, {}).get(name))
        return results

    monkeypatch.setattr(History, "load", load)


def git(project, *args):
    return subprocess.run(["git", *args], cwd=project, capture_output=True, text=True, check=True).stdout.strip()


CONFIG = """\
goal: |
  Make the sort faster
  Keep the public API; details on the following lines.
gates:
  - pytest -q
  - metric: speed
    regress: 5%
observe:
  - name: bench
    command: python bench.py
    metrics: {speed: higher}
    capture: [results/]
  - name: review
    review: What is the most important next fix?
    metrics: {missing: lower}
"""


@pytest.fixture
def project(tmp_path, run_paths):
    """A git project with one run: a baseline, an accepted and a rejected iteration."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "T")
    (tmp_path / ".gitignore").write_text(".autohelix/\n")
    (tmp_path / "autohelix.yaml").write_text(CONFIG)
    (tmp_path / "sort.py").write_text("def sort(xs):\n    return sorted(xs)\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "start")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "sort.py").write_text("def sort(xs):\n    return list(reversed(xs))\n")
    git(tmp_path, "add", "sort.py")
    tree = git(tmp_path, "write-tree")  # an attempt's frozen tree, kept by no branch
    git(tmp_path, "reset", "-q", "--hard", base)
    write_json(run_paths.identity, {**json.loads(run_paths.identity.read_text()), "base_commit": base})

    records = [
        {"iteration": 0, "accepted": True, "reason": "baseline", "metrics": {"speed": 10.0},
         "gates": [{"text": "pytest -q", "kind": "command", "passed": True, "return_code": 0, "duration_s": 1.0},
                   {"text": "speed regress 5%", "kind": "metric", "passed": None}],
         "observe": [{"name": "bench", "ok": True, "return_code": 0, "error": None,
                      "metrics": {"speed": 10.0}, "duration_s": 2.0}]},
        {"iteration": 1, "accepted": True, "metrics": {"speed": 12.0}, "summary": "Use the built-in sort",
         "base": base, "tree": tree, "discarded": ["tests/test_sort.py"],
         "gates": [{"text": "pytest -q", "kind": "command", "passed": True, "return_code": 0, "duration_s": 1.0},
                   {"text": "speed regress 5%", "kind": "metric", "passed": True, "value": 12.0, "reference": 10.0}],
         "observe": [{"name": "bench", "ok": True, "return_code": 0, "error": None,
                      "metrics": {"speed": 12.0}, "duration_s": 2.0},
                     {"name": "review", "ok": False, "return_code": 2, "error": "exited 2",
                      "metrics": {}, "duration_s": 30.0}]},
        {"iteration": 2, "accepted": False, "reason": "gate failed: pytest -q", "failure_output": "1 failed",
         "summary": "Try radix sort", "base": base, "tree": "f" * 40,
         "rejected_ref": "refs/autohelix-rejected/test/2",
         "gates": [{"text": "pytest -q", "kind": "command", "passed": False, "return_code": 1,
                    "duration_s": 1.0, "output": "1 failed"},
                   {"text": "speed regress 5%", "kind": "metric", "passed": None}],
         "observe": []},
    ]
    run_paths.history.write_text("".join(json.dumps(r) + "\n" for r in records))

    obs = run_paths.observations
    (obs / "iter-1" / "bench" / "results" / "deep").mkdir(parents=True)
    (obs / "iter-1" / "bench" / "stdout.txt").write_text("##autohelix[speed=12]\nran 3 times\n")
    (obs / "iter-1" / "bench" / "stderr.txt").write_text("warning: slow disk\n")
    (obs / "iter-1" / "bench" / "results" / "deep" / "log.json").write_text('{"runs": 3}')
    (obs / "iter-1" / "bench" / "results" / "page.html").write_text("<script>alert(1)</script>")
    (obs / "iter-1" / "review").mkdir()
    (obs / "iter-1" / "review" / "stdout.txt").write_text(
        "**Fix** the `cmp` call.\n\n<b>raw</b>\n\n##autohelix [missing=0] quoted by the reviewer\n"
        "##autohelix[missing=3]\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET")
    os.symlink(secret, obs / "iter-1" / "bench" / "results" / "link.txt")
    os.symlink(tmp_path, obs / "iter-1" / "bench" / "results" / "outside")
    os.mkfifo(obs / "iter-1" / "bench" / "results" / "pipe")
    (obs / "iter-0" / "stdout_0.txt").parent.mkdir(parents=True)
    (obs / "iter-0" / "stdout_0.txt").write_text("old flat layout\n")
    os.symlink(obs / "iter-1", obs / "iter-7")  # an iteration folder that is a symlink
    return tmp_path


@pytest.fixture
def server(project):
    server = make_server(project, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, response.read(), response.headers.get("Content-Type")
    except urllib.error.HTTPError as error:
        return error.code, error.read(), None


def get_json(url):
    status, body, _ = get(url)
    assert status == 200, (url, body)
    return json.loads(body)


# ---------- web API ----------

def test_title_and_config_in_state(server):
    state = get_json(server + "/api/state")
    assert state["title"] == "Make the sort faster"
    assert state["config"]["gates"] == ["pytest -q", "speed regress 5%"]
    assert [(o["name"], o["review"]) for o in state["config"]["observe"]] == [("bench", False), ("review", True)]
    assert "reviewer" not in state["config"] and "artifacts" not in state
    one = state["iterations"][1]
    assert (one["summary"], one["reason"], one["discarded"]) == ("Use the built-in sort", None, ["tests/test_sort.py"])
    two = state["iterations"][2]
    assert (two["summary"], two["reason"]) == ("Try radix sort", "gate failed: pytest -q")
    assert [g["passed"] for g in two["gates"]] == [False, None]


def test_observe_sections_by_entry(server):
    detail = get_json(server + "/api/iteration/1")
    bench, review = detail["observe"]
    assert (bench["name"], bench["review"], bench["ok"], bench["metrics"]) == ("bench", False, True, {"speed": 12.0})
    assert bench["stdout"].endswith("ran 3 times\n") and bench["stderr"] == "warning: slow disk\n"
    # Nested captures, no symlinks or FIFOs; stdout/stderr are the entry's own output, not captures.
    assert [f["path"] for f in bench["files"]] == ["bench/results/page.html", "bench/results/deep/log.json"]
    assert bench["stdout_file"] == "bench/stdout.txt"
    assert (review["name"], review["review"], review["ok"], review["error"]) == ("review", True, False, "exited 2")
    assert "<strong>Fix</strong>" in review["html"] and "<b>" not in review["html"]  # raw HTML is text
    assert "missing=3" not in review["html"]  # the command's metric line is dropped
    assert "##autohelix [missing=0] quoted" in review["html"]  # the escaped prose is not a metric line
    assert detail["summary"] == "Use the built-in sort" and detail["discarded"] == ["tests/test_sort.py"]
    assert [g["passed"] for g in detail["gates"]] == [True, True]
    assert "review" not in detail and "reviewer" not in detail


def test_legacy_flat_observations_and_missing_folders(server):
    baseline = get_json(server + "/api/iteration/0")["observe"]
    assert [(s["name"], s.get("legacy", False)) for s in baseline] == [("bench", False), (None, True)]
    assert baseline[0]["ran"] and baseline[0]["stdout"] is None  # recorded, nothing stored
    assert [f["path"] for f in baseline[1]["files"]] == ["stdout_0.txt"]
    assert get(server + "/api/artifact/0/stdout_0.txt")[1] == b"old flat layout\n"
    assert get_json(server + "/api/iteration/2")["observe"] == []
    assert get_json(server + "/api/iteration/7")["observe"] == []  # a symlinked iteration folder is not read


def test_nested_files_served_only_inside_the_iteration(server):
    status, body, mime = get(server + "/api/artifact/1/bench/results/deep/log.json")
    assert (status, body) == (200, b'{"runs": 3}') and mime.startswith("text/plain")
    status, body, mime = get(server + "/api/artifact/1/bench/results/page.html")
    assert status == 200 and mime.startswith("text/plain")  # never rendered as HTML
    assert get(server + "/api/artifact/1/review/stdout.txt")[0] == 200
    for path in ("bench/results/link.txt",            # symlink to a file outside
                 "bench/results/outside/secret.txt",  # through a symlinked folder
                 "bench/results/pipe",                # a FIFO (would block a plain open)
                 "bench/results",                     # a folder
                 "bench/..%2F..%2F..%2F..%2Fsecret.txt", "bench/%2E%2E/%2E%2E/iter-0/stdout_0.txt",
                 "..%2Fiter-0%2Fstdout_0.txt", "%2Fetc%2Fpasswd", "bench//stdout.txt", "bench/./stdout.txt",
                 "bench/stdout.txt%00", "missing.txt"):
        assert get(server + f"/api/artifact/1/{path}")[0] == 404, path
    assert get(server + "/api/artifact/7/bench/stdout.txt")[0] == 404  # iter-7 is a symlink


def test_gates_pending_while_running(project, server):
    paths = RunPaths(project, "test")
    write_json(paths.status, {"state": "running", "pid": os.getpid(), "iteration": 3, "phase": "checks",
                              "phase_started_at": 1, "iteration_started_at": 1})
    detail = get_json(server + "/api/iteration/3")
    assert [(g["text"], g["kind"], g["passed"]) for g in detail["gates"]] == [
        ("pytest -q", "command", None), ("speed regress 5%", "metric", None)]
    assert detail["attempt"] is None


def test_attempt_diff(project, server):
    detail = get_json(server + "/api/iteration/1")
    assert detail["attempt"]["tree"] and detail["attempt"]["rejected_ref"] is None
    diff = get_json(server + "/api/iteration/1/diff")
    assert diff["error"] is None
    assert "-    return sorted(xs)" in diff["diff"] and "+    return list(reversed(xs))" in diff["diff"]
    # A rejected attempt whose tree is gone (its ref deleted): said so, not an error page.
    gone = get_json(server + "/api/iteration/2/diff")
    assert gone["diff"] is None and "tree is no longer in the repository" in gone["error"]
    assert get_json(server + "/api/iteration/2")["attempt"]["rejected_ref"] == "refs/autohelix-rejected/test/2"
    assert "no starting commit" in get_json(server + "/api/iteration/0/diff")["error"]
    assert get_json(server + "/api/iteration/9/diff")["error"] == "This iteration has no record yet."


def test_attempt_diff_never_passes_options_to_git(project, server):
    paths = RunPaths(project, "test")
    rows = [json.loads(line) for line in paths.history.read_text().splitlines()]
    rows[1]["base"], rows[1]["tree"] = "--output=PWNED", "HEAD~1"
    paths.history.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert "no starting commit" in get_json(server + "/api/iteration/1/diff")["error"]
    assert get_json(server + "/api/iteration/1")["attempt"] is None
    assert not list(project.rglob("PWNED*"))


def test_review_fallback_without_a_readable_config(project):
    (project / "autohelix.yaml").write_text("goal: [unclosed\n")
    sections = RunData(project, run_id="test").iteration(1)["observe"]
    # Without the config, an entry whose folder holds only stdout.txt reads as a review.
    assert [(s["name"], s["review"]) for s in sections] == [("bench", False), ("review", True)]
    assert "missing=3" not in sections[1]["html"]


def test_review_markdown_strips_only_metric_lines():
    html = review_markdown("Text ##autohelix[x=1] inline\n  ##autohelix[missing=2]  \n##autohelix[a=1]")
    assert html == "<p>Text ##autohelix[x=1] inline</p>\n"
    assert review_markdown("") is None


def test_snapshot_keeps_status_and_reviews_but_not_outputs(project):
    routes = RunData(project, run_id="test").snapshot()
    bench, review = routes["/api/iteration/1"]["observe"]
    assert bench["stdout"] is None and bench["files"] == [] and bench["metrics"] == {"speed": 12.0}
    assert "<strong>Fix</strong>" in review["html"]
    assert "/api/iteration/1/diff" not in routes


# ---------- run list and terminal ----------

def test_run_list_and_view_json(project):
    runs = ProjectRuns(project)
    row = runs.runs()["runs"][0]
    assert row["title"] == "Make the sort faster" and row["goal"].startswith("Make the sort faster\nKeep")
    data = runs.run("test")
    assert data["run"]["title"] == "Make the sort faster"
    rows = data["run"]["iterations"]
    assert rows[2]["gates"][0] == {"text": "pytest -q", "kind": "command", "passed": False, "return_code": 1,
                                   "duration_s": 1.0, "output": "1 failed"}
    assert rows[1]["observe"][1]["ok"] is False and rows[1]["discarded"] == ["tests/test_sort.py"]
    assert {"iteration", "accepted", "metrics", "summary", "cost", "cost_incomplete", "seconds",
            "reason", "gates", "observe", "discarded"} <= rows[0].keys()


def test_terminal_tables_show_gates_summaries_and_titles(project):
    runs = ProjectRuns(project)
    listing = run_table.format_runs(runs.runs()).splitlines()
    assert listing[3].split()[-1] == "GOAL" and listing[4].endswith("Make the sort faster")
    text = run_table.format_run(runs.run("test"))
    lines = text.splitlines()
    assert "Goal: Make the sort faster" in lines
    table = lines[lines.index("") + 1:]
    assert table[1].endswith("baseline")
    assert table[2].endswith("Use the built-in sort")
    assert table[3].strip() == "! observe review failed: exited 2"
    assert table[4].strip() == "! discarded, outside the editable scope: tests/test_sort.py"
    assert table[5].split()[:2] == ["2", "✗"] and table[5].endswith("gate failed: pytest -q")
    assert table[6].strip() == "Try radix sort"
    assert len(table) == 7

    # On a short terminal, an iteration leaves with the lines under it.
    head = lines.index("") + 2
    cut = fit(text, head + 3).splitlines()
    assert cut[head] == "  … 2 earlier iterations"
    assert [line.strip() for line in cut[head + 1:]] == [table[5].strip(), "Try radix sort"]
