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
    with pytest.raises(AutoHelixRunError, match="agent failed: timed out after 1s"):
        harness.run(max_iterations=1, report=False)
    record = History(project).load()[-1]
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
    with pytest.raises(AutoHelixRunError, match="agent failed: crashed"):
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
