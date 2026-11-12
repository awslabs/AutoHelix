"""The viewer's config-v2 data: observations by entry, gates, summaries, discarded
paths, per-iteration diffs and titles (web API and terminal tables)."""

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest

from autohelix import run_table
from autohelix.live import fit, status_line
from autohelix.runs import RunPaths, write_json
from autohelix.viewer import MAX_TEXT, ProjectRuns, RunData, make_server, review_markdown, write_static


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
        with urllib.request.urlopen(url, timeout=20) as response:  # a hang fails, not blocks
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
    assert table[4].strip() == "! discarded, outside the scope: tests/test_sort.py"
    assert table[5].split()[:2] == ["2", "✗"] and table[5].endswith("gate failed: pytest -q")
    assert table[6].strip() == "Try radix sort"
    assert len(table) == 7

    # On a short terminal, an iteration leaves with the lines under it.
    head = lines.index("") + 2
    cut = fit(text, head + 3).splitlines()
    assert cut[head] == "  … 2 earlier iterations"
    assert [line.strip() for line in cut[head + 1:]] == [table[5].strip(), "Try radix sort"]


# ---------- hardening ----------

def test_symlinked_observations_folder_lists_and_serves_nothing(project, server):
    obs = RunPaths(project, "test").observations
    moved = project / "elsewhere"
    obs.rename(moved)
    os.symlink(moved, obs)
    bench, review = get_json(server + "/api/iteration/1")["observe"]  # still listed: the record names them
    assert (bench["stdout"], bench["files"], review["html"]) == (None, [], None)
    assert get(server + "/api/artifact/1/bench/results/deep/log.json")[0] == 404
    assert [s for s in get_json(server + "/api/iteration/0")["observe"] if s.get("legacy")] == []


def test_symlinked_entry_folder_shows_nothing(project, server):
    obs = RunPaths(project, "test").observations / "iter-1"
    (obs / "bench").rename(project / "bench-elsewhere")
    os.symlink(project / "bench-elsewhere", obs / "bench")
    bench = get_json(server + "/api/iteration/1")["observe"][0]
    assert (bench["stdout"], bench["files"]) == (None, [])
    assert get(server + "/api/artifact/1/bench/stdout.txt")[0] == 404


def test_hard_links_are_not_listed_or_served(project, server):
    bench = RunPaths(project, "test").observations / "iter-1" / "bench"
    os.link(project / "secret.txt", bench / "results" / "hard.txt")
    (bench / "stdout.txt").unlink()
    os.link(project / "secret.txt", bench / "stdout.txt")
    detail = get_json(server + "/api/iteration/1")["observe"][0]
    assert "bench/results/hard.txt" not in [f["path"] for f in detail["files"]]
    assert detail["stdout"] is None
    assert get(server + "/api/artifact/1/bench/results/hard.txt")[0] == 404
    assert get(server + "/api/artifact/1/bench/stdout.txt")[0] == 404
    assert get(server + "/api/artifact/1/bench/results/deep/log.json")[0] == 200


def test_notes_never_block_on_fifos_or_follow_symlinks(project, server):
    from autohelix.runs import ensure_copies

    paths = RunPaths(project, "test")
    version = paths.run_dir / "notes.1"
    version.mkdir()
    os.mkfifo(version / "iter-1.md")
    os.symlink(project / "secret.txt", version / "iter-2.md")
    (version / "ideas.md").write_text("# Ideas\n")
    os.symlink("notes.1", paths.run_dir / "notes")
    state = get_json(server + "/api/state")
    assert state["notes_files"] == ["ideas.md"]
    assert get_json(server + "/api/iteration/1")["notes"] is None
    assert get_json(server + "/api/iteration/2")["notes"] is None
    assert get(server + "/api/notes/iter-1.md")[0] == 404
    assert "<h1>Ideas</h1>" in get_json(server + "/api/notes/ideas.md")["html"]
    # A running iteration's note, read from the agent's copy.
    ensure_copies(paths.copies)
    notes = paths.copies / "iter-3-agent" / ".autohelix-iteration" / "notes"
    notes.mkdir(parents=True)
    os.mkfifo(notes / "iter-3.md")
    write_json(paths.status, {"state": "running", "pid": os.getpid(), "iteration": 3, "phase": "agent",
                              "phase_started_at": 1, "iteration_started_at": 1})
    assert get_json(server + "/api/iteration/3")["notes"] is None
    (notes / "iter-3.md").unlink()
    (notes / "iter-3.md").write_text("Working on **it**\n")
    assert "<strong>it</strong>" in get_json(server + "/api/iteration/3")["notes"]


def test_diffs_ignore_the_users_diff_settings(project, server):
    from autohelix.runs import ensure_copies
    from autohelix.workspace import Copy, Repo

    for key, value in (("color.ui", "always"), ("color.diff", "always"), ("diff.noprefix", "true"),
                       ("diff.mnemonicPrefix", "true"), ("diff.relative", "true"),
                       ("diff.suppressBlankEmpty", "true")):
        git(project, "config", key, value)
    attempt = get_json(server + "/api/iteration/1/diff")["diff"]
    assert "\x1b" not in attempt and "diff --git a/sort.py b/sort.py" in attempt and "--- a/sort.py" in attempt

    paths = RunPaths(project, "test")
    base = json.loads(paths.identity.read_text())["base_commit"]
    tree = get_json(server + "/api/iteration/1")["attempt"]["tree"]
    commit = git(project, "commit-tree", tree, "-p", base, "-m", "accepted")
    accepted = RunData(project, run_id="test")._diff(base, commit)
    assert "\x1b" not in accepted and "diff --git a/sort.py b/sort.py" in accepted

    ensure_copies(paths.copies)
    copy = Copy.create(Repo(project), paths.copies / "iter-3-agent", files="HEAD", head="HEAD")
    (copy.path / "sort.py").write_text("def sort(xs):\n    return xs\n")
    write_json(paths.status, {"state": "running", "pid": os.getpid(), "iteration": 3, "phase": "agent",
                              "phase_started_at": 1, "iteration_started_at": 1})
    live = get_json(server + "/api/iteration/3")["diff"]
    assert "\x1b" not in live and "diff --git a/sort.py b/sort.py" in live and "+    return xs" in live
    copy.remove()


def test_large_diffs_are_cut_with_a_note_and_the_cache_is_bounded(project, monkeypatch):
    import autohelix.viewer as viewer_module

    monkeypatch.setattr(viewer_module, "MAX_TEXT", 100)
    monkeypatch.setattr(viewer_module, "DIFF_CACHE", 2)
    data = RunData(project, run_id="test")
    diff = data.attempt_diff(1)
    assert diff["error"] is None
    assert diff["diff"].endswith("\n[… diff cut at 0 KB …]\n") and len(diff["diff"].encode()) < 100 + 40
    base = json.loads(RunPaths(project, "test").identity.read_text())["base_commit"]
    tree = data.records()[1].tree
    for a, b in ((base, tree), (tree, base), (base, base)):
        data._cached_diff(a, b)
    assert list(data._diffs) == [f"{tree}:{base}", f"{base}:{base}"]


def test_terminal_cells_carry_no_control_characters(project):
    paths = RunPaths(project, "test")
    rows = [json.loads(line) for line in paths.history.read_text().splitlines()]
    rows[1]["summary"] = "ok\n   9  ✓  fake row\x1b[31m"
    rows[1]["discarded"] = ["a\nb.py", "c\x9bd"]
    rows[2]["gates"][0]["text"] = "pytest\x1b]0;title\x07 -q"
    rows[1]["observe"][1]["error"] = "exited\r\n2"
    paths.history.write_text("".join(json.dumps(r) + "\n" for r in rows))
    (project / "autohelix.yaml").write_text(CONFIG.replace("goal: |\n  Make the sort faster\n",
                                                           'goal: "Make \\e[2J it faster"\n#'))
    runs = ProjectRuns(project)
    for text in (run_table.format_run(runs.run("test")), run_table.format_runs(runs.runs())):
        assert not run_table.CONTROL.search(text.replace("\n", ""))
    lines = run_table.format_run(runs.run("test")).splitlines()
    table = lines[lines.index("") + 1:]
    assert sum(1 for line in table[1:] if line[:4].strip()) == 3  # no fake row
    assert "Goal: Make �[2J it faster" in lines
    stop = {"state": "finished", "stopped_at": time.time(), "stop_reason": {"detail": "done\x1b[2J"}}
    assert "\x1b" not in status_line(stop)


def test_report_page_loads_nothing(project):
    page = write_static(project, run_id="test").read_text()
    head = page[:page.index("</head>")]
    assert ('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            "script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:\">") in head
    assert head.index("Content-Security-Policy") < head.index("<style>")


def test_running_iteration_marks_only_the_entry_running(project, server):
    paths = RunPaths(project, "test")
    (paths.observations / "iter-3" / "bench").mkdir(parents=True)
    (paths.observations / "iter-3" / "bench" / "stdout.txt").write_text("done\n")
    status = {"state": "running", "pid": os.getpid(), "iteration": 3, "phase": "benchmark",
              "phase_started_at": 1, "iteration_started_at": 1}
    write_json(paths.status, status)
    sections = get_json(server + "/api/iteration/3")["observe"]
    assert [(s["name"], s["running"]) for s in sections] == [("bench", False)]
    write_json(paths.status, {**status, "entry": "review"})
    sections = get_json(server + "/api/iteration/3")["observe"]
    assert [(s["name"], s["running"], s["stdout"]) for s in sections] == [
        ("bench", False, "done\n"), ("review", True, None)]


def test_config_is_parsed_again_only_when_its_file_changes(project, monkeypatch):
    import autohelix.viewer as viewer_module

    calls = []
    real = viewer_module.load_config
    monkeypatch.setattr(viewer_module, "load_config", lambda *a, **k: calls.append(1) or real(*a, **k))
    data = RunData(project, run_id="test")
    data.state()
    data.state()
    data.iteration(1)
    assert len(calls) == 1
    config = project / "autohelix.yaml"
    config.write_text(config.read_text().replace("Make the sort faster", "Make the sort much faster"))
    assert data.state()["title"] == "Make the sort much faster" and len(calls) == 2
