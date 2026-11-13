"""Run ownership, publication, recovery, and candidate correctness."""

import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from autohelix.agents import AgentRunResult
from autohelix.judge import DirectoryGuard
from autohelix.cli import main
from autohelix.harness import AutoHelixRunError, Harness
from autohelix.history import History
from autohelix.runs import RunLock, RunPaths, archive, delete, git, land, repair_history, short_id
from click.testing import CliRunner


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
    assert re.fullmatch(r"\d{8}-\d{4}-second-[0-9a-z]{4}", named.paths.run_id)
    assert named.paths.run_id != run_id
    named.close()
    other = project / "other.yaml"
    other.write_text((project / "autohelix.yaml").read_text())
    git(project, "add", ".")
    git(project, "commit", "-qm", "other config")
    third = Harness(project, config_file=other)
    assert re.fullmatch(r"\d{8}-\d{4}-other-[0-9a-z]{4}", third.paths.run_id)
    third.close()
    explicit = Harness(project, run_id=run_id)
    assert explicit.paths.run_id == run_id
    assert explicit.config_file == project / "autohelix.yaml"
    explicit.close()


def test_suffixes_are_unique_across_run_folders_and_refs(project, monkeypatch):
    import autohelix.runs as runs

    suffixes = iter("aaaaaaaa" "bbbb" "cccc")
    monkeypatch.setattr(runs.secrets, "choice", lambda _: next(suffixes))
    retained = project / ".autohelix" / "runs" / "aaaa"
    retained.mkdir(parents=True)
    git(project, "update-ref", "refs/autohelix/bbbb", "HEAD")
    harness = Harness(project)
    assert short_id(harness.paths.run_id) == "cccc"  # aaaa and bbbb are taken
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


def test_lock_is_atomic_advisory_status_and_delete_keep_lock_inode(project):
    harness = Harness(project)
    paths = harness.paths
    with pytest.raises(RuntimeError, match="is running"):
        RunLock(paths.lock)
    runner = CliRunner()
    for args in (["land"], ["delete", short_id(paths.run_id)], ["archive", short_id(paths.run_id)]):
        result = runner.invoke(main, [*args, "--path", str(project)])
        assert result.exit_code == 1 and "is running" in result.output
    harness.close()
    inode = paths.lock.stat().st_ino
    paths.runtime.mkdir(parents=True, exist_ok=True)
    paths.status.write_text(json.dumps({"state": "running", "pid": os.getpid()}))
    resumed = Harness(project)
    assert resumed.paths.run_id == paths.run_id
    resumed.close()
    delete(paths, force=True)
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
        with pytest.raises(RuntimeError, match="is running"):
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
    assert not list(paths.copies.glob("iter-*"))
    tip = git(project, "rev-parse", paths.ref).stdout.strip()
    harness.close()
    resumed = Harness(project, land_mode="manual")
    recovered = resumed.history.load()[-1]
    assert recovered.accepted and recovered.iteration == 1
    assert recovered.reason == "recovered" and recovered.commit == tip
    assert recovered.cost.incomplete
    resumed.close()


def test_ref_cas_failure_never_records_acceptance(project, monkeypatch):
    from autohelix.workspace import Repo

    harness = Harness(project, land_mode="manual")
    base = harness.run_state.expected_tip
    real = Repo.git

    def fail_update(repo, *args, **kwargs):
        if args[:2] == ("update-ref", "--no-deref"):
            raise RuntimeError(f"{harness.paths.ref} moved")
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(Repo, "git", fail_update)
    with pytest.raises(AutoHelixRunError, match="refs/autohelix"):
        harness.run_iteration(1)
    [record] = harness.history.load()
    assert not record.accepted and record.commit is None
    assert record.cost.cost_usd == 0.1
    assert git(project, "rev-parse", harness.paths.ref).stdout.strip() == base
    assert not list(harness.paths.copies.glob("iter-*"))
    harness.close()


def test_sigint_during_publication_defers_until_ref_and_history_agree(project, monkeypatch):
    from autohelix.workspace import Repo

    harness = Harness(project, land_mode="manual")
    real = Repo.git
    sent = False

    def interrupt(repo, *args, **kwargs):
        nonlocal sent
        if args[0] == "update-ref" and args[1] == "--no-deref" and not sent:
            sent = True
            os.kill(os.getpid(), signal.SIGINT)
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(Repo, "git", interrupt)
    with pytest.raises(KeyboardInterrupt):
        harness.run_iteration(1)
    [record] = harness.history.load()
    assert record.accepted
    assert record.commit == harness.run_state.expected_tip
    assert record.commit == git(project, "rev-parse", harness.paths.ref).stdout.strip()
    assert not list(harness.paths.copies.glob("iter-*"))
    harness.close()


@pytest.mark.parametrize("operation", ["commit", "rewind", "conflict", "merge", "delete-ref", "symbolic-ref"])
@pytest.mark.parametrize("accepted", [True, False])
def test_normalize_git_state_before_validation(project, monkeypatch, operation, accepted):
    configure(project, gates=["true" if accepted else "false"], scope={"editable": ["code.py"]})
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


@pytest.mark.parametrize("slot", ["gates", "observe", "stop"])
def test_nested_ask_mutation_cannot_be_swallowed(project, slot):
    data = yaml.safe_load((project / "autohelix.yaml").read_text())
    data["agent"]["settings"]["judge_change_file"] = "code.py"
    ask = 'autohelix ask "Done?" || true'
    if slot == "gates":
        data["gates"] = ['[ "$AUTOHELIX_ITERATION" = 0 ] || ' + ask]
    elif slot == "observe":
        data["observe"] = [{"name": "judge", "command": '[ "$AUTOHELIX_ITERATION" = 0 ] || ' + ask}]
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
    assert any(call["usage"].get("integrity_violation") for call in record.calls)
    assert record.cost.cost_usd == pytest.approx(0.2)
    accepted = git(project, "show", f"{harness.paths.ref}:code.py").stdout
    assert "Unvalidated judge edit" not in accepted


def test_baseline_judge_mutation_fails_and_cost_survives(project):
    data = yaml.safe_load((project / "autohelix.yaml").read_text())
    data["agent"]["settings"]["judge_change_file"] = "code.py"
    data["observe"] = [{"name": "judge", "command": 'autohelix ask "Done?" || true'}]
    (project / "autohelix.yaml").write_text(yaml.safe_dump(data))
    git(project, "add", ".")
    git(project, "commit", "-qm", "baseline judge")
    harness = Harness(project, land_mode="manual")
    with pytest.raises(AutoHelixRunError, match="Baseline command modified candidate files"):
        harness.run(max_iterations=1, report=False)
    [baseline] = harness.history.load()
    assert not baseline.accepted and baseline.iteration == 0
    assert baseline.cost.cost_usd == 0.1
    assert (project / "code.py").read_text() == "print('baseline')\n"
    retry = Harness(project, land_mode="manual")
    with pytest.raises(AutoHelixRunError, match="Baseline command modified candidate files"):
        retry.run(max_iterations=1, report=False)
    assert retry.history.load()[0].cost.cost_usd == pytest.approx(0.2)


@pytest.mark.parametrize("change", ["dirty", "new", "delete", "mode", "symlink"])
def test_guard_detects_candidate_content_and_modes(project, change):
    if change == "dirty":
        (project / "code.py").write_text("already dirty")
    if change == "symlink":
        (project / "link").symlink_to("code.py")
    guard = DirectoryGuard(project)
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
    assert guard.violations()


def test_guard_allows_feedback_and_exact_non_git_wrapper_outputs(tmp_path):
    log = tmp_path / "own.log"
    usage = tmp_path / "usage.jsonl"
    guard = DirectoryGuard(tmp_path, allowed=(log, usage))
    log.write_text("log")
    usage.write_text("usage")
    feedback = tmp_path / ".autohelix-iteration" / "notes" / "iter-1.md"  # the copy's harness material
    feedback.parent.mkdir(parents=True)
    feedback.write_text("notes")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_text("cache")
    assert guard.violations() == []


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
    git(module, "config", "user.email", "test@example.com")  # a fresh clone has no identity of its own
    git(module, "config", "user.name", "Test")
    guard = DirectoryGuard(project)
    (module / "code.py").write_text("judge change")
    git(module, "add", ".")
    git(module, "commit", "-qm", "judge")
    assert guard.violations() == ["module/code.py"]
    git(project, "submodule", "deinit", "-f", "module")
    assert DirectoryGuard(project).violations() == []


def test_judge_violation_stays_recorded_after_wrapper_restores_files(project):
    data = yaml.safe_load((project / "autohelix.yaml").read_text())
    data["agent"]["settings"]["judge_change_file"] = "code.py"
    data["gates"] = ['[ "$AUTOHELIX_ITERATION" = 0 ] && exit 0; cp code.py /tmp/ah-saved.$$; '
                           'autohelix ask "Done?"; cp /tmp/ah-saved.$$ code.py; exit 0']
    (project / "autohelix.yaml").write_text(yaml.safe_dump(data))
    git(project, "add", ".")
    git(project, "commit", "-qm", "restoring wrapper")
    harness = Harness(project, land_mode="manual")
    harness.run(max_iterations=1, report=False)
    record = harness.history.load()[-1]
    assert not record.accepted
    assert "judge call changed the files it judged" in record.reason
    assert any(call["usage"].get("integrity_violation") for call in record.calls)
    harness.close()


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


def test_delete_symbolic_ref_does_not_delete_user_branch(project):
    harness = Harness(project)
    paths = harness.paths
    target = f"refs/heads/{harness.run_info['target_branch']}"
    harness.close()
    old = git(project, "rev-parse", target).stdout.strip()
    git(project, "symbolic-ref", paths.ref, target)
    delete(paths)
    assert git(project, "rev-parse", target).stdout.strip() == old
    assert git(project, "rev-parse", "--verify", paths.ref, check=False).returncode != 0


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
    monkeypatch.setattr(harness, "build_prompt", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad prompt")))
    with pytest.raises(AutoHelixRunError, match="bad prompt"):
        harness.run_iteration(1)
    assert [row.iteration for row in harness.history.load()] == [0]
    assert not (harness.paths.copies / "iter-1").exists()
    harness.close()


def test_report_owns_run_and_restores_ref_before_removal(project, monkeypatch):
    from rich.console import Console
    from autohelix.report import write_report

    harness = Harness(project, land_mode="manual")
    with pytest.raises(RuntimeError, match="is running"):
        write_report(harness.config, project, Console(), harness.config_file, run_id=harness.paths.run_id)
    harness.run(max_iterations=1, report=False)
    expected = harness.run_state.expected_tip

    def run(**kwargs):
        git(project, "update-ref", "--no-deref", harness.paths.ref, harness.run_info["base_commit"])
        return AgentRunResult(success=True)

    monkeypatch.setattr("autohelix.agents.create_agent", lambda *a, **k: SimpleNamespace(run=run))
    assert not write_report(harness.config, project, Console(), harness.config_file, run_id=harness.paths.run_id)
    assert git(project, "rev-parse", harness.paths.ref).stdout.strip() == expected
    assert not harness.paths.copies.exists()


def test_archived_noop_retains_user_commit_tip(project, monkeypatch):
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
    archive(resumed.paths)
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


def test_selection_rereads_the_run_under_its_lock(project, monkeypatch):
    import autohelix.runs as runs

    harness = Harness(project)
    paths = harness.paths
    harness.close()
    original = runs.RunLock

    def acquire(path, **kwargs):
        lease = original(path, **kwargs)
        if path == paths.lock:  # the ref went away while we waited
            git(project, "update-ref", "-d", paths.ref)
        return lease

    monkeypatch.setattr(runs, "RunLock", acquire)
    with pytest.raises(RuntimeError, match="no ref left"):
        Harness(project, run_id=paths.run_id)
    with original(paths.lock):
        pass  # failed selection explicitly closed the acquired lease


def test_mutating_baseline_check_is_a_setup_error_before_agent_spend(project, monkeypatch):
    configure(project, gates=["true", "echo formatted >> code.py"])
    harness = Harness(project, land_mode="manual")
    monkeypatch.setattr(harness, "run_agent", lambda *_: pytest.fail("agent must not run"))
    try:
        with pytest.raises(AutoHelixRunError, match=r"Baseline command modified candidate files: "
                                                    r"echo formatted >> code.py \(code.py\)"):
            harness.run(max_iterations=1, report=False)
        [baseline] = harness.history.load()
        assert not baseline.accepted
        assert (project / "code.py").read_text() == "print('baseline')\n"
    finally:
        harness.close()


def test_failing_baseline_constraint_does_not_block_the_run(project):
    configure(project, gates=['[ "$AUTOHELIX_ITERATION" != 0 ]'])
    harness = Harness(project, land_mode="manual")
    harness.run(max_iterations=1, report=False)
    assert [(r.iteration, r.accepted) for r in harness.history.load()] == [(0, True), (1, True)]
    harness.close()


@pytest.mark.parametrize("scope", [{}, {"frozen": ["frozen.txt"]}])
def test_project_subdirectory_never_accepts_staged_sibling(project, monkeypatch, scope):
    sub = project / "sub"
    sub.mkdir()
    config = yaml.safe_load((project / "autohelix.yaml").read_text())
    config["scope"] = scope
    (sub / "autohelix.yaml").write_text(yaml.safe_dump(config))
    (sub / "code.py").write_text("baseline sub")
    (project / "sibling.py").write_text("baseline sibling")
    git(project, "add", ".")
    git(project, "commit", "-qm", "subdirectory")
    harness = Harness(sub, land_mode="manual")

    def agent(cwd, *_):
        (cwd / "code.py").write_text("candidate sub")
        (cwd.parent / "sibling.py").write_text("protected sibling")
        git(cwd, "add", "../sibling.py")
        return AgentRunResult(success=True, usage={"cost_usd": 0.2})

    monkeypatch.setattr(harness, "run_agent", agent)
    try:
        result = harness.run_iteration(1)
        assert result.accepted
        assert git(project, "show", f"{harness.paths.ref}:sibling.py").stdout == "baseline sibling"
        assert git(project, "show", f"{harness.paths.ref}:sub/code.py").stdout == "candidate sub"
    finally:
        harness.close()


def test_standalone_unborn_git_retains_cached_inventory(project, tmp_path):
    empty = tmp_path / "unborn"
    empty.mkdir()
    git(empty, "init", "-q")
    (empty / ".gitignore").write_text("ignored.txt\n")
    (empty / "ignored.txt").write_text("staged ignored content")
    git(empty, "add", "-f", "ignored.txt")
    guard = DirectoryGuard(empty)
    git(empty, "rm", "--cached", "ignored.txt")
    (empty / "ignored.txt").write_text("changed behind removed index")
    assert guard.violations() == ["ignored.txt"]


def test_nongit_and_dangling_symlink_mutations_are_detected(tmp_path):
    (tmp_path / "draft").write_text("candidate")
    (tmp_path / "link").symlink_to("missing")
    guard = DirectoryGuard(tmp_path)
    (tmp_path / "draft").write_text("judge edit")
    (tmp_path / "link").unlink()
    (tmp_path / "link").symlink_to("another-missing")
    assert guard.violations() == ["draft", "link"]


def add_submodule(project, tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    git(child, "init", "-q")
    git(child, "config", "user.email", "test@example.com")
    git(child, "config", "user.name", "Test")
    (child / "code.py").write_text("child baseline")
    git(child, "add", ".")
    git(child, "commit", "-qm", "child")
    git(project, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(child), "sub")
    git(project, "commit", "-qam", "submodule")


def test_initializing_empty_submodule_invalidates_guard(project, tmp_path):
    add_submodule(project, tmp_path)
    git(project, "submodule", "deinit", "-f", "sub")
    guard = DirectoryGuard(project)
    git(project, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "sub")
    (project / "sub" / "code.py").write_text("judge changed initialized child")
    assert "sub/code.py" in guard.violations()


def test_fresh_run_has_no_target_warning(project):
    from autohelix.runs import reconcile

    harness = Harness(project)
    try:
        assert reconcile(harness.paths, harness.run_info, harness.history, "auto")[1] is None
        assert reconcile(harness.paths, harness.run_info, harness.history, "manual")[1] is None
        git(project, "commit", "-q", "--allow-empty", "-m", "target ahead before baseline")
        assert reconcile(harness.paths, harness.run_info, harness.history, "auto")[1] is None
    finally:
        harness.close()


def test_target_behind_after_baseline_warns_only_for_auto_landing(project):
    from autohelix.runs import reconcile

    harness = Harness(project, land_mode="manual")
    harness.run(max_iterations=1, report=False)
    base = harness.run_info["base_commit"]
    warning = reconcile(harness.paths, harness.run_info, harness.history, "auto")[1]
    assert "is behind" in warning and "auto landing" in warning
    # In manual mode, behind just means not landed yet.
    assert reconcile(harness.paths, harness.run_info, harness.history, "manual")[1] is None
    assert git(project, "rev-parse", "HEAD").stdout.strip() == base


@pytest.mark.parametrize("problem", ["editable", "preflight"])
def test_refused_first_start_creates_no_identity(project, problem):
    if problem == "editable":
        configure(project, scope={"editable": ["missing_dir/*.py"]})
    else:
        configure(project, agent={"backend": "claude", "command": "/nonexistent/agent"})
    with pytest.raises(RuntimeError, match="No files found|Preflight checks failed"):
        Harness(project)
    root = RunPaths(project).root
    assert not (root / "current").exists()
    assert not list((root / "runs").glob("*/run.json"))
    assert not git(project, "for-each-ref", "--format=%(refname)", "refs/autohelix/").stdout.strip()


def test_manual_mode_warns_when_target_moves_during_segment(project, monkeypatch):
    harness = Harness(project, land_mode="manual")
    target = harness.run_info["target_branch"]
    printed = []
    original = harness.console.print
    monkeypatch.setattr(harness.console, "print", lambda *a, **k: (printed.append(" ".join(map(str, a))),
                                                                    original(*a, **k)))
    real = harness.run_agent

    def agent(cwd, prompt, iteration):
        git(project, "commit", "-q", "--allow-empty", "-m", "agent moved the target by absolute path")
        return real(cwd, prompt, iteration)

    monkeypatch.setattr(harness, "run_agent", agent)
    harness.run(max_iterations=1, report=False)
    assert any(f"Target {target} changed during this segment" in line for line in printed)


def test_ask_from_an_agent_without_log_dir_records_absolute_log(tmp_path, monkeypatch):
    from autohelix.agents import AgentConfig
    from autohelix.judge import ask
    from autohelix.usage_log import read_usage

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    usage = tmp_path / "usage.jsonl"
    monkeypatch.setenv("AUTOHELIX_RUN_DIR", str(run_dir))
    monkeypatch.setenv("AUTOHELIX_USAGE_FILE", str(usage))
    monkeypatch.delenv("AUTOHELIX_LOG_DIR", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    answer = ask("Done?", work, AgentConfig(backend="mock", read_only=True))
    assert answer.yes
    [call] = read_usage(usage)
    assert Path(call["log"]).is_absolute() and Path(call["log"]).exists()



def test_old_reviewer_call_costs_still_count_toward_the_budget_on_resume(project):
    configure(project, budget={"cost": 0.55, "iterations": 5})
    first = Harness(project)
    first.run(max_iterations=1, report=False)
    run_id = first.paths.run_id
    # A record written when a built-in reviewer still ran: its call stays in the history.
    record = first.history.load()[-1]
    record.calls.append({"role": "reviewer", "usage": {"cost_usd": 0.5}, "log": None})
    first.history.update(record)
    assert first.history.load()[-1].cost.cost_usd == pytest.approx(0.6)
    resumed = Harness(project, run_id=run_id)
    resumed.run(report=False)
    assert [r.iteration for r in resumed.history.load()] == [0, 1]  # 0.6 >= 0.55: nothing more runs
    assert json.loads(resumed.paths.status.read_text())["stop_reason"]["budget"] == "cost"
