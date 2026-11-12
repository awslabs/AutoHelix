"""Review fixes for evaluation, rejected work and the prompt: command output that isn't
UTF-8, diffs independent of the user's git config, the latest record after a retried
baseline, kept trees of every kind of change, a timed-out or tampering agent,
agent-written text, and custom templates."""

import os
import subprocess
import time

import pytest
import yaml

from autohelix import runs
from autohelix.agents import AgentRunResult
from autohelix.checks import run_command
from autohelix.config import Config
from autohelix.harness import AutoHelixRunError, Harness
from autohelix.history import History, IterationResult
from autohelix.prompt_template import build_prompt_variables, load_template, render_template, unknown_variables
from autohelix.workspace import ITERATION_DIR


def _git(cwd, *args, check=True, env=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check,
                          env={**os.environ, **(env or {})}).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "T")
    (repo / ".gitignore").write_text(".autohelix/\n")
    return repo


def _project(repo, sub=None, files=None):
    project = repo / sub if sub else repo
    project.mkdir(exist_ok=True)
    (project / "hello.py").write_text('print("hello")\n')
    for name, content in (files or {}).items():
        (project / name).write_text(content)
    return project


def _configure(project, repo, settings=None, **sections):
    config = {"goal": "Harden", "agent": {"backend": "mock", "settings": {
        "change_file": "hello.py", "change_content": "# iteration {iteration}", **(settings or {})}}, **sections}
    (project / "autohelix.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "config")


def _refs(repo, owner):
    paths = getattr(owner, "paths", owner)  # a Harness or RunPaths
    return _git(repo, "for-each-ref", "--format=%(refname)", paths.rejected_refs).split()


class Agent:
    """Stands in for Harness.run_agent: `act(worktree, iteration)` changes the copy and
    may return an AgentRunResult; else the mock agent runs. Records each copy's
    rejected.patch and prompt."""

    def __init__(self, harness, act=None):
        self.harness, self.original, self.act, self.seen = harness, harness.run_agent, act, {}
        harness.run_agent = self

    def __call__(self, worktree, prompt, iteration):
        patch = worktree / ITERATION_DIR / "rejected.patch"
        self.seen[iteration] = {"patch": patch.read_bytes() if patch.exists() else None, "prompt": prompt,
                                "worktree": worktree}
        if patch.exists():
            self.seen[iteration]["applied"] = _apply_in_probe(worktree, patch)
        result = self.act(worktree, iteration) if self.act else None
        return result if result is not None else self.original(worktree, prompt, iteration)


def _apply_in_probe(worktree, patch):
    """Apply rejected.patch with `git apply` in a copy of the agent's copy; returns the
    tree the probe's files hash to (the harness's material left out), or the error."""
    copy_root = worktree
    while not (copy_root / ".git").exists():
        copy_root = copy_root.parent
    probe = copy_root.parent / f"probe-{time.monotonic_ns()}"
    subprocess.run(["cp", "-Rp", str(copy_root), str(probe)], check=True)
    cwd = probe / worktree.relative_to(copy_root)
    try:
        result = subprocess.run(["git", "apply", str(cwd / ITERATION_DIR / "rejected.patch")], cwd=cwd,
                                capture_output=True, text=True)
        if result.returncode:
            return f"apply failed: {result.stderr}"
        index = {"GIT_INDEX_FILE": str(probe.parent / f"{probe.name}.index")}
        state = (worktree.relative_to(copy_root) / ITERATION_DIR).as_posix()
        _git(probe, "add", "-A", "--", ".", env=index)
        _git(probe, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", state, env=index)
        return _git(probe, "write-tree", env=index)
    finally:
        subprocess.run(["rm", "-rf", str(probe)], check=True)


# --- 1. command output that isn't UTF-8 ------------------------------------------------


def test_invalid_utf8_output_is_kept_and_background_children_still_die(tmp_path):
    late = tmp_path / "late"
    result = run_command(f"printf 'bad \\377 bytes'; (sleep 1; touch {late}) >/dev/null 2>&1 & exit 3", tmp_path)
    assert not result.passed and result.return_code == 3
    assert result.output == "bad � bytes"
    time.sleep(2)
    assert not late.exists()


# --- 2. the latest record after a retried baseline -------------------------------------


def test_latest_is_the_last_written_of_tied_records(tmp_path):
    (tmp_path / ".autohelix").mkdir()
    history = History(tmp_path)
    history.append(IterationResult(iteration=0, accepted=False, reason="baseline failed: x"))
    history.append(IterationResult(iteration=0, accepted=True, metrics={"speed": 1.0}, reason="baseline"))
    assert history.latest().accepted and history.latest(before=0) is None
    variables = build_prompt_variables(Config(goal="g"), history, 1, tmp_path)
    rendered = render_template(load_template(Config(goal="g")), variables)
    assert variables["last"]["accepted"] and "was rejected" not in rendered
    assert "Last accepted: the baseline (iter-0)" in rendered


# --- 3. diffs independent of the user's git config --------------------------------------


@pytest.mark.parametrize("sub", [None, "sub"])
def test_patches_apply_whatever_the_git_config(repo, sub):
    for key, value in [("color.ui", "always"), ("color.diff", "always"), ("diff.noprefix", "true"),
                       ("diff.mnemonicPrefix", "true"), ("diff.relative", "true")]:
        _git(repo, "config", key, value)
    project = _project(repo, sub)
    seen = repo.parent / "candidate.diff"
    _configure(project, repo, gates=[f'cp .autohelix-iteration/candidate.diff {seen}; '
                                      '[ "$AUTOHELIX_ITERATION" != 1 ]'])
    harness = Harness(project, verbose=False, land_mode="manual")
    agent = Agent(harness)
    harness.run(max_iterations=2, report=False)
    records = History(project).load()
    assert [r.accepted for r in records] == [True, False, True]
    prefix = f"{sub}/" if sub else ""
    candidate = seen.read_text()
    assert "\x1b" not in candidate and f"diff --git a/{prefix}hello.py b/{prefix}hello.py" in candidate
    patch = agent.seen[2]["patch"].decode()
    assert "\x1b" not in patch and f"--- a/{prefix}hello.py" in patch
    assert agent.seen[2]["applied"] == records[1].tree  # git apply in the copy reproduced it


# --- 4. an observe entry changing the candidate ----------------------------------------


def test_observe_entry_that_modifies_the_candidate_is_recorded_failed(repo):
    project = _project(repo)
    _configure(project, repo, observe=[{"name": "bench", "command": '[ "$AUTOHELIX_ITERATION" = 0 ] || echo x >> hello.py'}])
    Harness(project, verbose=False, land_mode="manual").run(max_iterations=1, report=False)
    record = History(project).load()[-1]
    assert not record.accepted and record.reason.startswith("checks modified candidate files: ")
    assert record.observe[0]["ok"] is False and record.observe[0]["error"] == record.reason


# --- 6, 7: kept trees and rejected.patch -------------------------------------------------


def test_every_kind_of_change_round_trips_through_rejected_patch(repo):
    project = _project(repo, files={"tool.sh": "echo tool\n", "old.txt": "going\n"})
    _configure(project, repo, gates=['[ "$AUTOHELIX_ITERATION" != 1 ]'])

    def act(worktree, iteration):
        if iteration == 1:
            (worktree / "img.bin").write_bytes(bytes(range(256)) * 4)
            os.symlink("hello.py", worktree / "link")
            os.chmod(worktree / "tool.sh", 0o755)
            (worktree / "old.txt").unlink()

    harness = Harness(project, verbose=False, land_mode="manual")
    agent = Agent(harness, act)
    harness.run(max_iterations=2, report=False)
    rejected = History(project).load()[1]
    assert not rejected.accepted
    listing = _git(repo, "ls-tree", rejected.tree)
    assert "120000" in listing and "100755" in listing and "img.bin" in listing and "old.txt" not in listing
    patch = agent.seen[2]["patch"]
    assert b"GIT binary patch" in patch and b"deleted file mode" in patch and b"new mode 100755" in patch
    assert agent.seen[2]["applied"] == rejected.tree


def test_a_too_large_rejected_change_is_named_not_written(repo, monkeypatch):
    monkeypatch.setattr("autohelix.iteration.REJECTED_PATCH_LIMIT", 10)
    project = _project(repo)
    _configure(project, repo, gates=['[ "$AUTOHELIX_ITERATION" != 1 ]'])
    harness = Harness(project, verbose=False, land_mode="manual")
    agent = Agent(harness)
    harness.run(max_iterations=2, report=False)
    rejected = History(project).load()[1]
    seen = agent.seen[2]
    assert seen["patch"] is None
    command = f"git diff {rejected.base} {rejected.tree}"
    assert f"too large for a patch file; see them with `{command}`" in seen["prompt"]
    # The copy reaches the rejected tree through its alternates (copy removed by now: use the repo's objects).
    assert "+# iteration 1" in _git(repo, *command.split()[1:])


def test_a_stale_kept_tree_under_an_accepted_number_is_removed(repo):
    project = _project(repo)
    _configure(project, repo)
    harness = Harness(project, verbose=False, land_mode="manual")
    _git(repo, "update-ref", harness.paths.rejected_ref(1), _git(repo, "rev-parse", "HEAD^{tree}"))
    harness.run(max_iterations=1, report=False)
    assert History(project).load()[-1].accepted and _refs(repo, harness) == []


def test_a_timed_out_agent_is_kept_and_rejected_unchecked(repo, tmp_path):
    project = _project(repo)
    ran = tmp_path / "gate-ran"
    _configure(project, repo, gates=[f'[ "$AUTOHELIX_ITERATION" = 0 ] || touch {ran}'])

    def act(worktree, iteration):
        (worktree / "hello.py").write_text("half done\n")
        return AgentRunResult(success=False, exit_code=-1, error="timed out after 1s")

    harness = Harness(project, verbose=False, land_mode="manual")
    Agent(harness, act)
    harness.run(max_iterations=1, report=False)
    record = History(project).load()[-1]
    assert record.reason == "Iteration 1 agent failed: timed out after 1s"
    assert not record.accepted and record.gates == [] and not ran.exists()
    assert _refs(repo, harness) == [record.rejected_ref]
    assert _git(repo, "show", f"{record.tree}:hello.py") == "half done"


def test_a_failed_agent_that_replaced_its_material_by_a_symlink(repo, tmp_path):
    project = _project(repo)
    _configure(project, repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "commit_summary.txt").write_text("planted summary\n")

    def act(worktree, iteration):
        state = worktree / ITERATION_DIR
        subprocess.run(["rm", "-rf", str(state)], check=True)
        os.symlink(elsewhere, state)
        (worktree / "hello.py").write_text("changed\n")
        return AgentRunResult(success=False, exit_code=1, error="crashed")

    harness = Harness(project, verbose=False, land_mode="manual")
    Agent(harness, act)
    harness.run(max_iterations=1, report=False)
    record = History(project).load()[-1]
    assert record.summary is None and record.rejected_ref
    assert ITERATION_DIR not in _git(repo, "ls-tree", "--name-only", record.tree).split()


def test_delete_leaves_runs_whose_id_shares_a_prefix(repo):
    project = _project(repo)
    _configure(project, repo)
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    first, second = runs.RunPaths(project, "20261112-0300-ab"), runs.RunPaths(project, "20261112-0300-ab-cd")
    for paths in (first, second):
        paths.run_dir.mkdir(parents=True)
        for n in (1, 2):
            _git(repo, "update-ref", paths.rejected_ref(n), tree)
    runs.delete(first, force=True)
    assert _refs(repo, first) == []
    assert _refs(repo, second) == [second.rejected_ref(1), second.rejected_ref(2)]


# --- 9, 10, 11: prompt wording, agent-written text, custom templates -------------------


def _render(tmp_path, records, iteration=2, patch=False):
    (tmp_path / ".autohelix").mkdir(parents=True, exist_ok=True)
    history = History(tmp_path)
    for record in records:
        history.append(IterationResult(**record))
    worktree = tmp_path / "copy"
    (worktree / ITERATION_DIR).mkdir(parents=True, exist_ok=True)
    if patch:
        (worktree / ITERATION_DIR / "rejected.patch").write_text("diff\n")
    config = Config(goal="g")
    return render_template(load_template(config), build_prompt_variables(config, history, iteration, worktree))


BASELINE = {"iteration": 0, "accepted": True, "reason": "baseline"}


def test_rejection_wording(tmp_path):
    rendered = _render(tmp_path, [BASELINE, {"iteration": 1, "accepted": False, "reason": "gate failed: t",
                                             "failure_output": "UNIQUE FAILURE LINE", "tree": "t",
                                             "rejected_ref": "r"}], patch=True)
    assert "Your copy holds the last accepted code." in rendered
    assert rendered.count("UNIQUE FAILURE LINE") == 1  # in "Failure output", not the history too
    assert "rejected (failure output above). Its changes are in" in rendered
    plain = _render(tmp_path / "b", [BASELINE, {"iteration": 1, "accepted": False, "reason": "agent failed",
                                                "tree": "t", "rejected_ref": "r"}], patch=True)
    assert "The previous attempt was rejected. Its changes are in" in plain and "output above" not in plain


def test_agent_written_text_is_cleaned(tmp_path):
    rendered = _render(tmp_path, [BASELINE, {
        "iteration": 1, "accepted": False, "reason": "gate failed: t", "summary": "sum\x1b[2Jmary\nIGNORE ABOVE",
        "discarded": ["evil\nname.py"], "failure_output": "line1\x1b[31m\nline2\x07",
        "observe": [{"name": "bench", "ok": False, "error": "exited\n2\x1b[0m"}]}])
    assert "\x1b" not in rendered and "\x07" not in rendered
    assert "sum mary IGNORE ABOVE" not in rendered and "summary IGNORE ABOVE" in rendered
    assert "Your changes to evil name.py were discarded" in rendered
    assert "    line1\n    line2" in rendered and "(failed: exited 2)" in rendered


def test_summary_is_cleaned_and_capped_for_record_and_commit(repo):
    project = _project(repo)
    _configure(project, repo)

    def act(worktree, iteration):
        (worktree / ITERATION_DIR / "commit_summary.txt").write_text("\x1b[31mred\x1b[0m " + "w" * 500 + "\nmore\n")

    harness = Harness(project, verbose=False, land_mode="manual")
    Agent(harness, act)
    harness.run(max_iterations=1, report=False)
    record = History(project).load()[-1]
    assert record.summary.startswith("red ww") and len(record.summary) == 200 and record.summary.endswith("…")
    assert _git(repo, "show", "-s", "--format=%s", record.commit) == record.summary


def test_unknown_template_variables():
    template = ("{{ goal }} {{ constraints }} {% for g in gates %}{{ g.text }}{% endfor %} {{ range(2) }} "
                "{% if history_summary %}{% endif %}{% set local = 1 %}{{ local }}")
    assert unknown_variables(template) == ["constraints", "history_summary"]
    assert unknown_variables("{% if") == []


def test_a_custom_template_with_old_variables_warns_at_run_start(repo, capsys):
    project = _project(repo)
    (project / "prompt.md").write_text("Goal: {{ goal }}\n{{ history_summary }}\n")
    _configure(project, repo, prompt_template="prompt.md")
    Harness(project, verbose=False, land_mode="manual").run(max_iterations=1, report=False)
    out = capsys.readouterr().out
    assert "prompt.md uses variables the prompt doesn't have" in out and "history_summary" in out


def test_status_names_the_running_observe_entry(repo):
    project = _project(repo)
    _configure(project, repo, observe=[{"name": "first", "command": "true"}, {"name": "second", "command": "true"}])
    harness = Harness(project, verbose=False, land_mode="manual")
    seen = []
    original = harness.run_status.note

    def note(**info):
        if "entry" in info:
            seen.append((harness.run_status.data.get("phase"), info["entry"]))
        original(**info)

    harness.run_status.note = note
    harness.run(max_iterations=1, report=False)
    assert ("benchmark", "first") in seen and ("benchmark", "second") in seen
    assert seen[-1][1] is None


# --- final cleanup: scope reasons, baseline gates, unreported metrics, stop reasons ---


def test_frozen_discards_are_said_apart(repo):
    project = _project(repo)
    (project / "fixtures").mkdir()
    (project / "fixtures" / "data.txt").write_text("fixed\n")
    _configure(project, repo, scope={"editable": ["hello.py", "fixtures/"], "frozen": ["fixtures/"]})

    def act(worktree, iteration):
        (worktree / "fixtures" / "data.txt").write_text("edited\n")
        (worktree / "notes.txt").write_text("not editable\n")

    harness = Harness(project, verbose=False, land_mode="manual")
    agent = Agent(harness, act)
    harness.run(max_iterations=2, report=False)
    record = History(project).load()[1]
    assert record.discarded == ["fixtures/data.txt", "notes.txt"]
    assert record.discarded_frozen == ["fixtures/data.txt"]
    prompt = agent.seen[2]["prompt"]
    assert "Your changes to notes.txt were discarded: outside the editable scope." in prompt
    assert "Your changes to fixtures/data.txt were discarded: frozen." in prompt


def test_a_rejection_that_changed_nothing_keeps_no_tree(repo):
    project = _project(repo)
    _configure(project, repo, settings={"change_file": "elsewhere.txt"}, scope={"editable": ["hello.py"]},
               gates=['[ "$AUTOHELIX_ITERATION" != 1 ]'])
    harness = Harness(project, verbose=False, land_mode="manual")
    agent = Agent(harness)
    harness.run(max_iterations=2, report=False)
    record = History(project).load()[1]
    assert not record.accepted and record.discarded == ["elsewhere.txt"]
    assert record.rejected_ref is None and _refs(repo, harness) == []
    assert agent.seen[2]["patch"] is None and "rejected.patch" not in agent.seen[2]["prompt"]


def test_console_notes_for_baseline_gates_unreported_metrics_and_discards():
    from autohelix.display import Transcript

    transcript = Transcript(Config.from_dict({"goal": "g", "gates": ["pytest -q"]}), 100)
    baseline = IterationResult(iteration=0, accepted=True, gates=[{"text": "pytest -q", "kind": "command",
                                                                    "passed": False}])
    [_, note] = transcript.baseline(baseline)
    assert note.plain.strip() == "gate failed at the baseline: pytest -q"
    record = IterationResult(iteration=1, accepted=True, discarded=[f"f{i}.py" for i in range(5)],
                             discarded_frozen=["f0.py"],
                             observe=[{"name": "bench", "ok": True, "metric_errors": {"speed": "hint"}}])
    lines = [line.plain.strip() for line in transcript.row(record, {}, {})]
    assert "observe bench reported no speed" in lines
    assert "discarded 5 out-of-scope changes: f0.py (frozen), f1.py, f2.py, … (5 paths)" in lines


def test_view_notes_for_baseline_gates_unreported_metrics_and_discards():
    from autohelix.run_table import _summary

    _, more = _summary({"iteration": 0, "accepted": True, "gates": [
        {"text": "pytest -q", "kind": "command", "passed": False}]})
    assert more == ["! gate failed at the baseline: pytest -q"]
    _, more = _summary({"iteration": 2, "accepted": True, "summary": "s", "discarded": ["a", "b"],
                        "discarded_frozen": ["b"], "observe": [{"name": "bench", "ok": True,
                                                                "metric_errors": {"speed": "hint"}}]})
    assert more == ["! observe bench reported no speed", "! discarded, outside the scope: a, b (frozen)"]


def test_an_unreported_metric_keeps_its_hint_and_reaches_the_prompt(tmp_path):
    from autohelix.checks import parse_structured_metrics, run_observe_entry
    from autohelix.config import ObserveEntry

    assert parse_structured_metrics("##autohelix[lines=      12 ]") == {"lines": 12.0}
    entry = ObserveEntry(name="bench", command="echo done", metrics={"speed": "higher"})
    record = run_observe_entry(entry, tmp_path).record()
    assert record["ok"] and "##autohelix[speed=" in record["metric_errors"]["speed"]
    rendered = _render(tmp_path, [BASELINE, {"iteration": 1, "accepted": True, "observe": [record]}])
    assert "  bench: .autohelix-iteration/observations/iter-1/bench/ (reported no speed: print" in rendered


def test_a_jq_style_last_line_is_not_the_stop_reason(tmp_path):
    from autohelix.checks import run_stop_checks

    config = Config.from_dict({"goal": "g", "stop": ["echo true"]})
    [result] = run_stop_checks(config, tmp_path)
    assert result.stop and result.reason == "echo true"


def test_header_lines_are_cut_to_the_width():
    from autohelix.display import header_lines

    config = Config.from_dict({"goal": "g", "gates": [f"pytest tests/test_{i}.py" for i in range(10)]})
    lines = header_lines(config, "20261112-1200-abcd", "proj", 3, 60)
    assert all(len(line.plain) <= 60 for line in lines) and lines[3].plain.endswith("…")


def test_baseline_missing_metric_names_the_entry(repo):
    project = _project(repo)
    _configure(project, repo, observe=[{"name": "bench", "command": "echo done", "metrics": {"speed": "higher"}}])
    with pytest.raises(RuntimeError, match=r"observe entry `bench` exited 0 but reported no `speed`"):
        Harness(project, verbose=False).run(max_iterations=1, report=False)


def test_run_start_warnings_wrap_under_their_label(repo, capsys):
    project = _project(repo)
    (project / ".autohelix").mkdir(exist_ok=True)
    (project / ".autohelix" / "prompt.md").write_text("old\n")
    (project / "prompt.md").write_text("Goal: {{ goal }}; notes in .autohelix/notes/iter-1.md\n")
    _configure(project, repo, prompt_template="prompt.md")
    Harness(project, verbose=False, land_mode="manual").run(max_iterations=1, report=False)
    out = capsys.readouterr().out
    text = " ".join(out.split())
    assert ".autohelix/prompt.md is no longer read" in text
    assert "prompt.md names .autohelix/ paths in the agent's copy" in text
    assert all(not line.strip() or line.startswith("  ") for line in out.splitlines()[:12])


# --- agent failures continue the loop ---------------------------------------------------


def _failing(iterations, error="crashed"):
    """An `act` that fails the agent call on the given iterations (after editing hello.py)."""
    def act(worktree, iteration):
        if iteration in iterations:
            (worktree / "hello.py").write_text(f"attempt {iteration}\n")
            return AgentRunResult(success=False, exit_code=1, error=error)
    return act


def test_a_timeout_then_an_accept_with_its_patch(repo):
    from autohelix.harness import MAX_CONSECUTIVE_AGENT_FAILURES  # noqa: F401 - the documented constant

    project = _project(repo)
    _configure(project, repo)
    harness = Harness(project, verbose=False, land_mode="manual")
    agent = Agent(harness, _failing({1}, error="timed out after 30 minutes"))
    harness.run(max_iterations=2, report=False)
    records = History(project).load()
    assert [(r.iteration, r.accepted) for r in records] == [(0, True), (1, False), (2, True)]
    timed_out = records[1]
    assert timed_out.agent_error == "timed out after 30 minutes" and timed_out.gates == []
    assert timed_out.rejected_ref and timed_out.cost is not None
    patch = agent.seen[2]["patch"]
    assert patch and b"+attempt 1" in patch and "rejected.patch" in agent.seen[2]["prompt"]


def test_three_agent_failures_in_a_row_stop_the_run_and_land_accepted_work(repo):
    from autohelix.run_status import read_run_status

    project = _project(repo)
    _configure(project, repo, scope={"editable": ["hello.py"]})
    harness = Harness(project, verbose=False)  # auto landing
    Agent(harness, _failing({2, 3, 4}))
    harness.run(max_iterations=10, report=False)
    records = History(project).load()
    assert [(r.iteration, r.accepted) for r in records] == [(0, True), (1, True), (2, False), (3, False), (4, False)]
    reason = read_run_status(project)["stop_reason"]
    assert reason["kind"] == "agent" and reason["detail"] == "the agent failed 3 times in a row: crashed"
    assert _git(repo, "show", "main:hello.py").endswith("# iteration 1")  # iteration 1 landed


def test_failures_that_are_not_consecutive_dont_stop_the_run(repo):
    from autohelix.run_status import read_run_status

    project = _project(repo)
    _configure(project, repo, scope={"editable": ["hello.py"]})
    harness = Harness(project, verbose=False, land_mode="manual")
    Agent(harness, _failing({1, 3, 4}))
    harness.run(max_iterations=4, report=False)
    records = History(project).load()
    assert [r.accepted for r in records] == [True, False, True, False, False]
    assert read_run_status(project)["stop_reason"]["detail"] == "iteration budget reached (4)"
