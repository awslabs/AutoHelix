"""Run ownership, checkpoint publication, recovery, and candidate correctness."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from autohelix.agents import AgentRunResult
from autohelix.checkpoint import CandidateChanged, ContentGuard
from autohelix.cli import main
from autohelix.harness import AutoHelixRunError, Harness
from autohelix.history import History
from autohelix.runs import RunLock, RunPaths, abandon, git, land, repair_history
from click.testing import CliRunner


@pytest.fixture
def project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-q")
    git(project, "config", "user.email", "test@example.com")
    git(project, "config", "user.name", "Test")
    (project / "code.py").write_text("print('baseline')\n")
    (project / "autohelix.yaml").write_text(yaml.safe_dump({
        "goal": "Make a checked change", "report": False,
        "agent": {"type": "mock", "settings": {
            "change_file": "code.py", "change_content": "# iteration {iteration}", "cost_usd": 0.1,
        }},
    }))
    git(project, "add", ".")
    git(project, "commit", "-qm", "baseline")
    (project / ".autohelix").mkdir()
    return project


def configure(project, **values):
    path = project / "autohelix.yaml"
    data = yaml.safe_load(path.read_text())
    data.update(values)
    path.write_text(yaml.safe_dump(data))
    git(project, "add", "autohelix.yaml")
    git(project, "commit", "-qm", "config")


def test_identity_selection_new_and_explicit_config(project):
    first = Harness(project, name="first")
    first.run(max_iterations=1, report=False)
    run_id = first.paths.run_id
    second = Harness(project)
    assert second.paths.run_id == run_id
    second.close()
    named = Harness(project, new=True, name="second")
    assert named.paths.run_id.startswith("second-")
    assert named.paths.run_id != run_id
    named.close()
    other = project / "other.yaml"
    other.write_text((project / "autohelix.yaml").read_text())
    git(project, "add", ".")
    git(project, "commit", "-qm", "other config")
    third = Harness(project, config_file=other)
    assert third.paths.run_id.startswith("other-")
    third.close()
    explicit = Harness(project, run_id=run_id)
    assert explicit.paths.run_id == run_id
    assert explicit.config_file == project / "autohelix.yaml"
    explicit.close()


def test_directory_and_common_ref_id_collisions(project, monkeypatch):
    import autohelix.runs as runs

    suffixes = iter("aaaaaaaa" "bbbb" "cccc")
    monkeypatch.setattr(runs.secrets, "choice", lambda _: next(suffixes))
    retained = project / ".autohelix" / "runs" / "aaaa"
    retained.mkdir(parents=True)
    git(project, "update-ref", "refs/autohelix/bbbb", "HEAD")
    harness = Harness(project)
    assert harness.paths.run_id == "cccc"
    harness.close()
    assert retained.exists()


def test_accept_moves_only_run_ref_and_dirty_resume_preserves_index(project):
    harness = Harness(project, land_mode="manual")
    base = git(project, "rev-parse", "HEAD").stdout.strip()
    harness.run(max_iterations=1, report=False)
    assert git(project, "rev-parse", "HEAD").stdout.strip() == base
    assert git(project, "rev-parse", harness.paths.ref).stdout.strip() != base
    (project / "user.txt").write_text("staged user work")
    git(project, "add", "user.txt")
    (project / "code.py").write_text("dirty overlapping user edit")
    before = git(project, "diff", "--cached").stdout
    resumed = Harness(project, land_mode="manual")
    resumed.run(max_iterations=2, report=False)
    assert resumed.paths.run_id == harness.paths.run_id
    assert (project / "code.py").read_text() == "dirty overlapping user edit"
    assert git(project, "diff", "--cached").stdout == before
    assert [r.iteration for r in resumed.history.load()] == [0, 1, 2]
    assert all(r.accepted for r in resumed.history.load())


def test_lock_is_atomic_advisory_status_and_purge_keep_lock_inode(project):
    harness = Harness(project)
    paths = harness.paths
    with pytest.raises(RuntimeError, match="ownership lock"):
        RunLock(paths.lock)
    runner = CliRunner()
    for command in ("land", "abandon"):
        result = runner.invoke(main, [command, "--path", str(project)])
        assert result.exit_code == 1 and "ownership lock" in result.output
    harness.close()
    inode = paths.lock.stat().st_ino
    paths.runtime.mkdir(parents=True, exist_ok=True)
    paths.status.write_text(json.dumps({"state": "running", "pid": os.getpid()}))
    resumed = Harness(project)
    assert resumed.paths.run_id == paths.run_id
    resumed.close()
    abandon(paths, purge=True)
    assert paths.lock.exists() and paths.lock.stat().st_ino == inode
    with RunLock(paths.lock):
        pass


def test_process_death_releases_run_lock(project):
    harness = Harness(project)
    path = harness.paths.lock
    harness.close()
    process = subprocess.Popen(
        [sys.executable, "-u", "-c",
         "from pathlib import Path; from autohelix.runs import RunLock; import sys,time; "
         "lock=RunLock(Path(sys.argv[1])); print('owned',flush=True); time.sleep(60)", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == "owned"
        with pytest.raises(RuntimeError, match="ownership lock"):
            RunLock(path)
    finally:
        process.kill()
        process.wait()
    with RunLock(path):
        pass


@pytest.mark.parametrize("tail,valid", [
    (b'{"iteration":1,"accepted":true}', True),
    (b'{"iteration":1,"accepted":', False),
])
def test_history_tail_repair_preserves_valid_unterminated_record(tmp_path, tail, valid):
    path = tmp_path / "history.jsonl"
    first = b'{"iteration":0,"accepted":true}\n'
    path.write_bytes(first + tail)
    repair_history(path)
    assert path.read_bytes() == first + (tail + b"\n" if valid else b"")


@pytest.mark.parametrize("content", [
    b'broken\n{"iteration":1,"accepted":true}\n',
    b'{"iteration":0,"accepted":true}\nbroken\n',
    b'{"iteration":0,"accepted":true}\n{"not":"a record"}',
])
def test_history_corruption_is_not_repaired(tmp_path, content):
    path = tmp_path / "history.jsonl"
    path.write_bytes(content)
    with pytest.raises(RuntimeError, match="history record"):
        repair_history(path)
    assert path.read_bytes() == content


def test_recovery_after_ref_publication_and_torn_append(project, monkeypatch):
    harness = Harness(project, land_mode="manual")
    harness._capture_baseline()
    original = harness.history.append

    def crash(result):
        if result.iteration:
            with harness.paths.history.open("ab") as stream:
                stream.write(b'{"iteration":')
            raise OSError("crash after publication")
        original(result)

    monkeypatch.setattr(harness.history, "append", crash)
    with pytest.raises(AutoHelixRunError, match="crash after publication"):
        harness.run_iteration(1)
    paths = harness.paths
    assert not list(paths.worktrees.glob("iter-*"))
    tip = git(project, "rev-parse", paths.ref).stdout.strip()
    harness.close()
    resumed = Harness(project, land_mode="manual")
    recovered = resumed.history.load()[-1]
    assert recovered.accepted and recovered.iteration == 1
    assert recovered.reason == "recovered" and recovered.commit == tip
    assert recovered.cost.incomplete
    resumed.close()


def test_ref_cas_failure_never_records_acceptance(project, monkeypatch):
    import autohelix.iteration as iteration

    harness = Harness(project, land_mode="manual")
    base = harness.run_state.expected_tip
    real = iteration.git

    def fail_update(project, *args, **kwargs):
        if args[0] == "update-ref":
            raise RuntimeError(f"{harness.paths.ref} moved")
        return real(project, *args, **kwargs)

    monkeypatch.setattr(iteration, "git", fail_update)
    with pytest.raises(AutoHelixRunError, match="refs/autohelix"):
        harness.run_iteration(1)
    [record] = harness.history.load()
    assert not record.accepted and record.commit is None
    assert record.cost.cost_usd == 0.1
    assert git(project, "rev-parse", harness.paths.ref).stdout.strip() == base
    assert not list(harness.paths.worktrees.glob("iter-*"))
    harness.close()


def test_sigint_during_commit_defers_until_ref_and_history_agree(project, monkeypatch):
    from autohelix.checkpoint import PinnedGit

    harness = Harness(project, land_mode="manual")
    real = PinnedGit.run
    sent = False

    def interrupt(context, *args, **kwargs):
        nonlocal sent
        if args[0] == "commit-tree" and not sent:
            sent = True
            os.kill(os.getpid(), signal.SIGINT)
        return real(context, *args, **kwargs)

    monkeypatch.setattr(PinnedGit, "run", interrupt)
    with pytest.raises(KeyboardInterrupt):
        harness.run_iteration(1)
    [record] = harness.history.load()
    assert record.accepted
    assert record.commit == harness.run_state.expected_tip
    assert record.commit == git(project, "rev-parse", harness.paths.ref).stdout.strip()
    assert not list(harness.paths.worktrees.glob("iter-*"))
    harness.close()


@pytest.mark.parametrize("operation", ["commit", "rewind", "conflict", "merge", "delete-ref", "symbolic-ref"])
@pytest.mark.parametrize("accepted", [True, False])
def test_normalize_git_state_before_validation(project, monkeypatch, operation, accepted):
    configure(project, constraints=["true" if accepted else "false"], scope={"editable": ["code.py"]})
    harness = Harness(project, land_mode="manual")
    base = harness.run_state.expected_tip
    target = f"refs/heads/{harness.run_info['target_branch']}"
    user_tip = git(project, "commit-tree", f"{base}^{{tree}}", "-p", base, "-m", "user").stdout.strip()
    git(project, "update-ref", target, user_tip, base)

    def agent(cwd, *_):
        if operation == "rewind":
            git(cwd, "reset", "--hard", "HEAD~1")
        if operation in ("conflict", "merge"):
            git(cwd, "switch", "-c", f"side-{operation}")
            changed = "code.py" if operation == "conflict" else "side.txt"
            (cwd / changed).write_text("side\n")
            git(cwd, "add", ".")
            git(cwd, "commit", "-qm", "side")
            git(cwd, "checkout", "--detach", base)
        if operation == "commit":
            git(cwd, "checkout", f"autohelix/{harness.paths.run_id}")
        (cwd / "code.py").write_text("print('candidate')\n")
        if operation in ("commit", "conflict", "merge"):
            git(cwd, "add", "code.py")
            git(cwd, "commit", "-qm", "agent")
        if operation in ("conflict", "merge"):
            git(cwd, "merge", f"side-{operation}", "--no-edit", check=False)
            (cwd / "code.py").write_text("print('candidate')\n")
        if operation == "delete-ref":
            git(cwd, "update-ref", "-d", harness.paths.ref)
        if operation == "symbolic-ref":
            git(cwd, "symbolic-ref", harness.paths.ref, target)
        return AgentRunResult(success=True, usage={"cost_usd": 0.2})

    monkeypatch.setattr(harness, "run_agent", agent)
    record = harness.run_iteration(1)
    assert record.accepted == accepted
    assert git(project, "rev-parse", target).stdout.strip() == user_tip
    assert git(project, "symbolic-ref", "--quiet", harness.paths.ref, check=False).returncode != 0
    tip = git(project, "rev-parse", harness.paths.ref).stdout.strip()
    if accepted:
        assert git(project, "rev-list", "--parents", "-n", "1", tip).stdout.split() == [tip, base]
        assert "AutoHelix-Run:" in git(project, "show", "-s", "--format=%B", tip).stdout
    else:
        assert tip == base
    assert git(project, "reflog", "exists", harness.paths.ref, check=False).returncode == 0
    harness.close()


def test_normalize_after_failed_agent_and_review_edits_are_rejected(project, monkeypatch):
    harness = Harness(project, land_mode="manual")
    harness.config.reviewer = SimpleNamespace(
        model=None, timeout_seconds=None, auto_memory=False, prompt="Review",
    )

    def review(**kwargs):
        (kwargs["worktree_path"] / "code.py").write_text("judge edit")
        (kwargs["worktree_path"] / ".autohelix" / "review.md").write_text("Feedback")
        git(kwargs["worktree_path"], "add", "code.py")
        git(kwargs["worktree_path"], "commit", "-qm", "judge commit")
        return AgentRunResult(success=False, usage={"cost_usd": 0.3})

    monkeypatch.setattr("autohelix.harness.create_agent", lambda *_args, **_kwargs: SimpleNamespace(run=review))
    record = harness.run_iteration(1)
    assert not record.accepted and "Reviewer changed" in record.reason
    assert record.cost.cost_usd == pytest.approx(0.4)
    assert [call["role"] for call in record.calls] == ["agent", "reviewer"]
    assert git(project, "rev-parse", harness.paths.ref).stdout.strip() == harness.run_info["base_commit"]
    harness.close()


@pytest.mark.parametrize("slot", ["constraints", "observables", "stop"])
def test_nested_ask_mutation_cannot_be_swallowed(project, slot):
    data = yaml.safe_load((project / "autohelix.yaml").read_text())
    data["agent"]["settings"]["judge_change_file"] = "code.py"
    ask = 'autohelix ask "Done?" || true'
    if slot == "constraints":
        data["constraints"] = [ask]
    elif slot == "observables":
        data["observables"] = [{"command": '[ "$AUTOHELIX_ITERATION" = 0 ] || ' + ask}]
    else:
        data["stop"] = ['[ "$AUTOHELIX_ITERATION" = 0 ] && exit 1; ' + ask]
    (project / "autohelix.yaml").write_text(yaml.safe_dump(data))
    git(project, "add", ".")
    git(project, "commit", "-qm", "judge config")
    harness = Harness(project, land_mode="manual")
    harness.run(max_iterations=1, report=False)
    record = harness.history.load()[-1]
    assert record.accepted == (slot == "stop")
    assert record.stop is None
    assert any(call["usage"].get("candidate_changed") for call in record.calls)
    assert record.cost.cost_usd == pytest.approx(0.2)
    accepted = git(project, "show", f"{harness.paths.ref}:code.py").stdout
    assert "Unvalidated judge edit" not in accepted


def test_baseline_judge_mutation_fails_and_cost_survives(project):
    data = yaml.safe_load((project / "autohelix.yaml").read_text())
    data["agent"]["settings"]["judge_change_file"] = "code.py"
    data["observables"] = [{"command": 'autohelix ask "Done?" || true'}]
    (project / "autohelix.yaml").write_text(yaml.safe_dump(data))
    git(project, "add", ".")
    git(project, "commit", "-qm", "baseline judge")
    harness = Harness(project, land_mode="manual")
    with pytest.raises(AutoHelixRunError, match="Judge changed"):
        harness.run(max_iterations=1, report=False)
    [baseline] = harness.history.load()
    assert not baseline.accepted and baseline.iteration == 0
    assert baseline.cost.cost_usd == 0.1
    retry = Harness(project, land_mode="manual")
    with pytest.raises(RuntimeError, match="Uncommitted"):
        retry.run(max_iterations=1, report=False)
    assert retry.history.load()[0].cost.cost_usd == 0.1


@pytest.mark.parametrize("change", ["dirty", "new", "delete", "mode", "symlink"])
def test_guard_detects_candidate_content_and_modes(project, change):
    if change == "dirty":
        (project / "code.py").write_text("already dirty")
    if change == "symlink":
        (project / "link").symlink_to("code.py")
    with pytest.raises(CandidateChanged):
        with ContentGuard(project):
            if change == "new":
                (project / "new.py").write_text("new")
            elif change == "delete":
                (project / "code.py").unlink()
            elif change == "mode":
                (project / "code.py").chmod(0o755)
            elif change == "symlink":
                (project / "link").unlink()
                (project / "link").symlink_to("autohelix.yaml")
            else:
                (project / "code.py").write_text("changed contents with same status")


def test_guard_allows_feedback_and_exact_non_git_wrapper_outputs(tmp_path):
    log = tmp_path / "own.log"
    usage = tmp_path / "usage.jsonl"
    with ContentGuard(tmp_path, allowed=(log, usage)):
        log.write_text("log")
        usage.write_text("usage")
        feedback = tmp_path / ".autohelix" / "review.md"
        feedback.parent.mkdir()
        feedback.write_text("review")


def test_guard_detects_submodule_edit_and_commit(project, tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    git(child, "init", "-q")
    git(child, "config", "user.email", "test@example.com")
    git(child, "config", "user.name", "Test")
    (child / "code.py").write_text("original")
    git(child, "add", ".")
    git(child, "commit", "-qm", "init")
    git(project, "-c", "protocol.file.allow=always", "submodule", "add", str(child), "module")
    git(project, "commit", "-qm", "submodule")
    module = project / "module"
    with pytest.raises(CandidateChanged):
        with ContentGuard(project):
            (module / "code.py").write_text("judge change")
            git(module, "add", ".")
            git(module, "commit", "-qm", "judge")
    git(project, "submodule", "deinit", "-f", "module")
    with ContentGuard(project):
        pass


def test_constructor_failure_releases_owner_even_with_retained_traceback(project):
    harness = Harness(project)
    paths = harness.paths
    harness.close()
    paths.history.write_text("corrupt\n")
    with pytest.raises(RuntimeError) as retained:
        Harness(project)
    assert retained.value.__traceback__ is not None
    paths.history.unlink()
    retry = Harness(project)
    retry.close()


def test_prebaseline_retry_still_requires_clean_checkout(project):
    harness = Harness(project)
    run_id = harness.paths.run_id
    harness.close()
    (project / "code.py").write_text("dirty")
    retry = Harness(project)
    assert retry.paths.run_id == run_id
    with pytest.raises(RuntimeError, match="Uncommitted"):
        retry.run(max_iterations=1, report=False)
    assert not retry.history.load()


def test_resume_picks_up_user_commits_and_land_divergence_is_safe(project):
    harness = Harness(project, land_mode="manual")
    harness.run(max_iterations=1, report=False)
    paths = harness.paths
    with RunLock(paths.lock):
        land(paths, harness.run_info, harness.run_state.expected_tip)
    (project / "user.txt").write_text("user")
    git(project, "add", ".")
    git(project, "commit", "-qm", "user work")
    user = git(project, "rev-parse", "HEAD").stdout.strip()
    resumed = Harness(project, land_mode="manual")
    assert resumed.run_state.expected_tip == user
    resumed.run(max_iterations=2, report=False)
    (project / "code.py").write_text("divergence")
    git(project, "add", ".")
    git(project, "commit", "-qm", "diverged")
    target = git(project, "rev-parse", "HEAD").stdout.strip()
    with RunLock(paths.lock), pytest.raises(RuntimeError, match="diverged"):
        land(paths, resumed.run_info, resumed.run_state.expected_tip)
    assert git(project, "rev-parse", "HEAD").stdout.strip() == target


def test_auto_land_before_report_and_after_abort(project, monkeypatch):
    harness = Harness(project)
    harness.config.report = True
    seen = []

    def report(*args, **kwargs):
        seen.append(git(project, "rev-parse", "HEAD").stdout.strip())
        return True

    monkeypatch.setattr("autohelix.report.write_report", report)
    harness.run(max_iterations=1)
    assert seen == [harness.run_state.expected_tip]
    resumed = Harness(project)
    real = resumed.run_iteration

    def abort_after_accept(iteration):
        record = real(iteration)
        raise AutoHelixRunError("later infrastructure failure")

    monkeypatch.setattr(resumed, "run_iteration", abort_after_accept)
    with pytest.raises(AutoHelixRunError, match="later infrastructure"):
        resumed.run(max_iterations=2, report=False)
    assert git(project, "rev-parse", "HEAD").stdout.strip() == resumed.run_state.expected_tip


def test_legacy_layout_refuses_cli(project):
    (project / ".autohelix" / "history.jsonl").write_text("{}\n")
    result = CliRunner().invoke(main, ["run", "--path", str(project)])
    assert result.exit_code == 1
    assert "Legacy AutoHelix state" in result.output


def test_abandon_symbolic_ref_does_not_delete_user_branch(project):
    harness = Harness(project)
    paths = harness.paths
    target = f"refs/heads/{harness.run_info['target_branch']}"
    harness.close()
    old = git(project, "rev-parse", target).stdout.strip()
    git(project, "symbolic-ref", paths.ref, target)
    abandon(paths)
    assert git(project, "rev-parse", target).stdout.strip() == old
    assert git(project, "rev-parse", "--verify", paths.ref, check=False).returncode != 0


def test_interrupted_reviewer_keeps_unknown_attempt_cost_and_log(project, monkeypatch):
    configure(project, reviewer={"prompt": "Inspect without editing"})
    harness = Harness(project)
    # Capture baseline without a reviewer attempt, then interrupt iteration1.
    reviewer = harness.config.reviewer
    harness.config.reviewer = None
    harness._capture_baseline()
    harness.config.reviewer = reviewer
    monkeypatch.setattr(harness, "run_reviewer", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        harness.run_iteration(1)
    record = harness.history.load()[-1]
    assert not record.accepted
    assert [call["role"] for call in record.calls] == ["agent", "reviewer"]
    assert record.calls[-1]["usage"] == {}
    assert "call-002-reviewer" in record.calls[-1]["log"]
    assert record.cost.cost_usd == 0.1 and record.cost.incomplete
    harness.close()


def test_baseline_review_retry_has_distinct_transcripts(project, monkeypatch):
    configure(project, reviewer={"prompt": "Review baseline"})
    harness = Harness(project)
    reviewer = harness.config.reviewer
    harness.config.reviewer = None
    harness._capture_baseline()
    harness.config.reviewer = reviewer
    real = harness.run_reviewer
    monkeypatch.setattr(harness, "run_reviewer", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        harness._run_baseline_review()
    first = harness.history.load()[0].calls[-1]["log"]
    monkeypatch.setattr(harness, "run_reviewer", real)
    harness._run_baseline_review()
    calls = harness.history.load()[0].calls
    assert len(calls) == 2
    assert first != calls[-1]["log"]
    assert (harness.paths.run_dir / calls[-1]["log"]).is_file()
    harness.close()


def test_same_harness_failed_restart_does_not_land_old_tip(project):
    harness = Harness(project)
    harness.run(max_iterations=1, report=False)
    git(project, "reset", "--hard", harness.run_info["base_commit"])
    (project / ".gitignore").write_text("!.*\n")
    before = git(project, "rev-parse", "HEAD").stdout.strip()
    with pytest.raises(RuntimeError, match="gitignore"):
        harness.run(max_iterations=2, report=False)
    assert git(project, "rev-parse", "HEAD").stdout.strip() == before


def test_prompt_setup_failure_does_not_consume_attempt(project, monkeypatch):
    harness = Harness(project)
    harness._capture_baseline()
    monkeypatch.setattr(harness, "build_prompt", lambda *a: (_ for _ in ()).throw(ValueError("bad prompt")))
    with pytest.raises(AutoHelixRunError, match="bad prompt"):
        harness.run_iteration(1)
    assert [row.iteration for row in harness.history.load()] == [0]
    assert not (harness.paths.worktrees / "iter-1").exists()
    harness.close()


def test_report_owns_run_and_restores_ref_before_removal(project, monkeypatch):
    from rich.console import Console
    from autohelix.report import write_report

    harness = Harness(project, land_mode="manual")
    with pytest.raises(RuntimeError, match="live"):
        write_report(harness.config, project, Console(), run_id=harness.paths.run_id)
    harness.run(max_iterations=1, report=False)
    expected = harness.run_state.expected_tip

    def run(**kwargs):
        git(project, "update-ref", "--no-deref", harness.paths.ref, harness.run_info["base_commit"])
        return AgentRunResult(success=True)

    monkeypatch.setattr("autohelix.agents.create_agent", lambda *a, **k: SimpleNamespace(run=run))
    assert not write_report(harness.config, project, Console(), run_id=harness.paths.run_id)
    assert git(project, "rev-parse", harness.paths.ref).stdout.strip() == expected
    assert not (harness.paths.worktrees / "iter--1").exists()


def test_abandoned_noop_retains_user_commit_tip(project, monkeypatch):
    from autohelix.runs import run_tip

    harness = Harness(project)
    harness.run(max_iterations=1, report=False)
    (project / "code.py").write_text("user commit after land\n")
    git(project, "add", "code.py")
    git(project, "commit", "-qm", "user work")
    user_tip = git(project, "rev-parse", "HEAD").stdout.strip()
    resumed = Harness(project)
    monkeypatch.setattr(resumed, "run_agent", lambda *a, **k: AgentRunResult(success=True))
    resumed.run(max_iterations=2, report=False)
    assert resumed.history.load()[-1].commit is None
    abandon(resumed.paths)
    assert run_tip(resumed.paths) == user_tip
    # Readers must not chase subsequent movement of the user's target branch.
    git(project, "reset", "--hard", harness.run_info["base_commit"])
    assert run_tip(resumed.paths) == user_tip


def test_viewer_calls_containment_and_current_alias(project):
    from autohelix.viewer import RunData

    data = RunData(project, run_id="current")
    harness = Harness(project)
    harness.run(max_iterations=1, report=False)
    assert data.state()["run_id"] == harness.paths.run_id
    outside = project / "private.log"
    outside.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "private"}]}}) + "\n")
    linked = harness.paths.logs / "escape.log"
    linked.symlink_to(outside)
    assert data._events(outside) == []
    assert data._events(linked) == []
    second = Harness(project, new=True)
    second.run(max_iterations=1, report=False)
    assert data.state()["run_id"] == second.paths.run_id
    assert [call["role"] for call in data.iteration(1)["calls"]] == ["agent"]
    row = json.loads(second.paths.history.read_text().splitlines()[-1])
    assert row["metrics"] == {} and row["cost"]["cost_usd"] == 0.1


def test_selection_reloads_lifecycle_under_run_lock(project, monkeypatch):
    import autohelix.runs as runs

    harness = Harness(project)
    paths = harness.paths
    harness.close()
    original = runs.RunLock

    def acquire(path, **kwargs):
        lease = original(path, **kwargs)
        if path == paths.lock:
            identity = paths.read_identity()
            identity["lifecycle"] = "abandoned"
            runs.write_json(paths.identity, identity)
        return lease

    monkeypatch.setattr(runs, "RunLock", acquire)
    with pytest.raises(RuntimeError, match="abandoned"):
        Harness(project, run_id=paths.run_id)
    with original(paths.lock):
        pass  # failed selection explicitly closed the acquired lease


def test_real_worker_failure_includes_subprocess_stderr(project):
    from autohelix.parallel.core import Worker, _run_segment

    configure(project, agent={"type": "mock", "settings": {"should_fail": True}})
    worker = Worker(index=1, name="worker-1", config_file=project / "autohelix.yaml", worktree=project,
                    branch="unused", shared_dir=project / ".autohelix" / "peers", budget=1)
    with pytest.raises(RuntimeError, match="Worker exited 1.*Mock failure"):
        _run_segment(worker, 1, False)
