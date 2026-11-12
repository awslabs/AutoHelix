"""Regression cases for candidate copies and publication."""

import os
import signal
import subprocess
import sys
import time
import json
from pathlib import Path

import pytest

from autohelix.agents import AgentRunResult
from autohelix.harness import AutoHelixRunError, Harness
from autohelix.runs import RunLock, delete, git, land, short_id
from autohelix.usage_log import append_usage
from autohelix.viewer import RunData
from tests.test_runs import project, configure


@pytest.mark.parametrize("operation", ["delete-marker", "orphan"])
def test_agent_git_tampering_preserves_user_checkout(project, monkeypatch, operation):
    import shutil

    harness = Harness(project, land_mode="manual")
    (project / "code.py").write_text("user dirty content")
    git(project, "add", "code.py")
    before_head = git(project, "symbolic-ref", "HEAD").stdout
    before_index = git(project, "diff", "--cached").stdout

    def agent(cwd, *_):
        (cwd / "code.py").write_text("validated candidate")
        if operation == "delete-marker":
            shutil.rmtree(cwd / ".git")
        else:
            git(cwd, "symbolic-ref", "HEAD", "refs/heads/unborn-agent")
        return AgentRunResult(success=True, usage={"cost_usd": 0.2})

    monkeypatch.setattr(harness, "run_agent", agent)
    try:
        record = harness.run_iteration(1)
        assert record.accepted
        assert git(project, "show", f"{harness.paths.ref}:code.py").stdout == "validated candidate"
        assert git(project, "symbolic-ref", "HEAD").stdout == before_head
        assert git(project, "diff", "--cached").stdout == before_index
        assert (project / "code.py").read_text() == "user dirty content"
        assert not harness.paths.copies.joinpath("iter-1").exists()
    finally:
        harness.close()


def test_validation_cannot_stage_protected_files_or_attach_head(project, monkeypatch):
    (project / "frozen.txt").write_text("protected")
    git(project, "add", ".")
    git(project, "commit", "-qm", "protected file")
    configure(project, scope={"editable": ["code.py"]})
    harness = Harness(project, land_mode="manual")
    target = git(project, "symbolic-ref", "HEAD").stdout.strip()
    base = git(project, "rev-parse", target).stdout.strip()

    def checks(*args, **kwargs):
        cwd = args[1]
        git(cwd, "symbolic-ref", "HEAD", target)
        (cwd / "frozen.txt").write_text("check changed protected file")
        git(cwd, "add", "frozen.txt")
        kwargs["after_command"]("fake check")
        return []

    monkeypatch.setattr("autohelix.harness.run_gate_commands", checks)
    try:
        record = harness.run_iteration(1)
        assert not record.accepted
        assert record.reason == "checks modified candidate files: fake check (frozen.txt)"
        assert git(project, "rev-parse", target).stdout.strip() == base
        assert git(project, "show", f"{harness.paths.ref}:frozen.txt").stdout == "protected"
    finally:
        harness.close()


def test_private_commit_does_not_run_hooks(project):
    marker = project / ".autohelix" / "hook-ran"
    hook = project / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    hook.chmod(0o755)
    harness = Harness(project, land_mode="manual")
    try:
        record = harness.run_iteration(1)
        assert record.accepted and not marker.exists()
        message = git(project, "show", "-s", "--format=%B", record.commit).stdout
        assert f"AutoHelix-Run: {harness.paths.run_id}" in message
    finally:
        harness.close()


def test_postaccept_stop_sees_accepted_head_and_clean_index(project):
    configure(project, stop=[
        '[ "$AUTOHELIX_ITERATION" != 0 ] && '
        '[ "$(git rev-parse HEAD)" = "$(git rev-parse refs/autohelix/$AUTOHELIX_RUN_ID)" ] && '
        'git diff --quiet HEAD && git diff --cached --quiet && echo "accepted HEAD is clean"'
    ])
    original_head = git(project, "rev-parse", "HEAD").stdout
    original_branch = git(project, "symbolic-ref", "HEAD").stdout
    harness = Harness(project, land_mode="manual")
    harness.run(max_iterations=3, report=False)
    records = harness.history.load()
    assert [record.iteration for record in records] == [0, 1]
    accepted = records[1]
    assert accepted.accepted and accepted.commit
    assert accepted.stop["reason"] == "accepted HEAD is clean"
    assert git(project, "rev-parse", harness.paths.ref).stdout.strip() == accepted.commit
    assert git(project, "rev-parse", "HEAD").stdout == original_head
    assert git(project, "symbolic-ref", "HEAD").stdout == original_branch


def test_failed_publication_with_pending_interrupt_keeps_original_error(project, monkeypatch):
    from autohelix.workspace import Repo

    harness = Harness(project, land_mode="manual")
    original = Repo.git

    def fail(repo, *args, **kwargs):
        if args[:2] == ("update-ref", "--no-deref"):
            os.kill(os.getpid(), signal.SIGINT)
            raise RuntimeError("publication compare failed")
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(Repo, "git", fail)
    try:
        with pytest.raises(AutoHelixRunError, match="publication compare failed"):
            harness.run_iteration(1)
        record = harness.history.load()[-1]
        assert not record.accepted
        assert "publication compare failed" in record.reason
        assert git(project, "rev-parse", harness.paths.ref).stdout.strip() == harness.run_state.expected_tip
    finally:
        harness.close()


def test_dirty_first_start_allocates_no_run(project):
    (project / "code.py").write_text("uncommitted user work")
    with pytest.raises(RuntimeError, match="Uncommitted"):
        harness = Harness(project)
        try:
            harness.run(max_iterations=1, report=False)
        finally:
            harness.close()
    assert not (project / ".autohelix" / "current").exists()
    assert not list((project / ".autohelix" / "runs").glob("*/run.json"))
    assert not git(project, "for-each-ref", "--format=%(refname)", "refs/autohelix/").stdout.strip()


def test_landing_refuses_target_mid_rebase_without_mutating_it(project):
    harness = Harness(project, land_mode="manual")
    base = harness.run_state.expected_tip
    target = harness.run_info["target_branch"]
    git(project, "branch", "other", base)
    (project / "code.py").write_text("target conflicting change")
    git(project, "commit", "-qam", "target")
    old = git(project, "rev-parse", "HEAD").stdout.strip()
    tree = git(project, "rev-parse", "HEAD^{tree}").stdout.strip()
    accepted = git(project, "commit-tree", tree, "-p", old, "-m", "accepted").stdout.strip()
    git(project, "checkout", "-q", "other")
    (project / "code.py").write_text("other conflicting change")
    git(project, "commit", "-qam", "other")
    git(project, "checkout", "-q", target)
    assert git(project, "rebase", "other", check=False).returncode
    head = git(project, "rev-parse", "HEAD").stdout
    index = git(project, "ls-files", "--stage").stdout
    try:
        with pytest.raises(RuntimeError, match="busy or changed"):
            land(harness.paths, harness.run_info, accepted)
        assert git(project, "rev-parse", f"refs/heads/{target}").stdout.strip() == old
        assert git(project, "rev-parse", "HEAD").stdout == head
        assert git(project, "ls-files", "--stage").stdout == index
    finally:
        git(project, "rebase", "--abort")
        harness.close()


def test_auto_landing_never_rewinds_target_changed_during_segment(project, monkeypatch):
    harness = Harness(project)
    run_agent = harness.run_agent
    observed = []

    def agent(*args):
        (project / "user.txt").write_text("legitimate concurrent user commit")
        git(project, "add", "user.txt")
        git(project, "commit", "-qm", "user")
        observed.append(git(project, "rev-parse", "HEAD").stdout.strip())
        return run_agent(*args)

    monkeypatch.setattr(harness, "run_agent", agent)
    harness.run(max_iterations=1, report=False)
    assert harness.history.load()[-1].accepted
    assert git(project, "rev-parse", "HEAD").stdout.strip() == observed[0]
    assert (project / "user.txt").read_text() == "legitimate concurrent user commit"


def test_selection_waits_briefly_and_real_new_runs_allocate_distinct_ids(project):
    script = (
        "from pathlib import Path; from autohelix.harness import Harness; "
        "h=Harness(Path('.'),new=True,land_mode='manual'); "
        "h.run(max_iterations=1,report=False); print('RESULT='+h.paths.run_id)"
    )
    lease = RunLock(project / ".autohelix" / "runtime" / "locks" / "selection.lock")
    processes = [subprocess.Popen([sys.executable, "-c", script], cwd=project,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(2)]
    time.sleep(0.15)
    assert all(process.poll() is None for process in processes)
    lease.close()
    ids = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        ids.append(next(line.removeprefix("RESULT=") for line in stdout.splitlines() if line.startswith("RESULT=")))
    assert ids[0] != ids[1]


def test_reusing_deleted_id_does_not_recover_older_run_commits(project, monkeypatch):
    import autohelix.runs as runs

    monkeypatch.setattr(runs.secrets, "choice", lambda _: "a")
    first = Harness(project)
    first.run(max_iterations=1, report=False)
    assert short_id(first.paths.run_id) == "aaaa"
    delete(first.paths, force=True)
    second = Harness(project, new=True)
    try:
        assert short_id(second.paths.run_id) == "aaaa"
        assert second.history.load() == []
        assert second.run_state.expected_tip == git(project, "rev-parse", "HEAD").stdout.strip()
    finally:
        second.close()


@pytest.mark.parametrize("metadata", ["outside", "malformed", "symlink"])
def test_calls_only_retains_usage_with_unsafe_live_log_metadata(project, tmp_path, monkeypatch, metadata):
    harness = Harness(project, land_mode="manual")
    outside = tmp_path / "outside"
    outside.mkdir()
    (harness.paths.logs / "escaped").parent.mkdir(parents=True, exist_ok=True)
    (harness.paths.logs / "escaped").symlink_to(outside)
    value = {"outside": str(outside / "call.log"), "malformed": {"path": "bad"},
             "symlink": "logs/escaped/call.log"}[metadata]

    def checks(*args, **kwargs):
        ledger = kwargs["extra_env"]["AUTOHELIX_USAGE_FILE"]
        append_usage({"id": "bad-log", "source": "custom", "cost_usd": 0.2, "log": value}, path=Path(ledger))
        append_usage({"id": "unfinished", "source": "custom", "status": "started", "cost_usd": None}, path=Path(ledger))
        live = RunData(project).iteration(1, diff=False)
        bad = next(call for call in live["calls"] if call["usage"].get("id") == "bad-log")
        assert bad["log"] is None and bad["usage"]["cost_usd"] == 0.2
        return []

    monkeypatch.setattr("autohelix.harness.run_gate_commands", checks)
    try:
        record = harness.run_iteration(1)
        assert record.accepted and record.cost.cost_usd == pytest.approx(0.3)
        assert record.cost.incomplete
        assert all(call.get("log") is None or not Path(call["log"]).is_absolute() for call in record.calls)
        persisted = json.loads(harness.paths.history.read_text().splitlines()[-1])
        assert not {"usage", "command_usage"} & persisted.keys()
        assert len(persisted["calls"]) == 3
    finally:
        harness.close()


def test_no_current_observers_ignore_old_top_level_files(tmp_path):
    root = tmp_path / ".autohelix"
    root.mkdir()
    (root / "history.jsonl").write_text("corrupt legacy history")
    (root / "runtime").mkdir()
    (root / "runtime" / "status.json").write_text('{"state":"running"}')
    (root / "notes").mkdir()
    (root / "notes" / "iter-1.md").write_text("old notes")
    data = RunData(tmp_path)
    assert data.state()["iterations"] == []
    assert data.state()["status"] is None and data.state()["notes_files"] == []
    assert data.iteration(1)["calls"] == []


@pytest.mark.parametrize("preexisting", [False, True])
def test_checks_cannot_modify_candidate_but_capture_new_files(project, preexisting):
    if preexisting:
        (project / "metric.txt").write_text("protected candidate content")
        git(project, "add", "metric.txt")
        git(project, "commit", "-qm", "protected capture")
    configure(project, scope={"editable": ["code.py"]}, observe=[{
        "name": "bench", "command": "echo 'new metric output' > metric.txt; echo 'score: 1'",
        "metrics": {"score": "higher"}, "capture": ["metric.txt"],
    }])
    harness = Harness(project, land_mode="manual")
    try:
        if preexisting:
            record = harness.run_iteration(1)
            assert not record.accepted and record.reason.startswith("checks modified candidate files: echo")
            assert git(project, "show", f"{harness.paths.ref}:metric.txt").stdout == "protected candidate content"
        else:
            result = harness.run_iteration(1)
            assert result.accepted
            assert (harness.paths.observations / "iter-1" / "bench" / "metric.txt").read_text() == "new metric output\n"
            assert git(project, "cat-file", "-e", f"{harness.paths.ref}:metric.txt", check=False).returncode
    finally:
        harness.close()
