"""End-to-end behavior of the copy-based iteration (run-model-core-design-alt step 7)."""

import subprocess
from pathlib import Path

import pytest
import yaml

from autohelix.harness import Harness
from autohelix.runs import RunPaths, git, land
from tests.test_runs import configure, project  # noqa: F401 - fixture


def _judge_config(project, **settings):
    data = yaml.safe_load((project / "autohelix.yaml").read_text())
    data["agent"]["settings"].update(settings)
    return data


def _write(project, data):
    (project / "autohelix.yaml").write_text(yaml.safe_dump(data))
    git(project, "add", ".")
    git(project, "commit", "-qm", "config")


def test_mutate_validate_restore_across_commands_is_caught(project):  # noqa: F811
    # One command changes a value, a second validates it and restores the file;
    # the final files equal T, but the second command saw different content.
    configure(project, gates=[
        '[ "$AUTOHELIX_ITERATION" = 0 ] || { cp code.py /tmp/ah-e2e.$PPID; echo "x = 1" > code.py; }',
        '[ "$AUTOHELIX_ITERATION" = 0 ] || { grep -q "x = 1" code.py && cp /tmp/ah-e2e.$PPID code.py; }',
    ])
    harness = Harness(project, land_mode="manual")
    harness.run(max_iterations=1, report=False)
    record = harness.history.load()[-1]
    assert not record.accepted
    assert record.reason.startswith("checks modified candidate files: [ \"$AUTOHELIX_ITERATION\" = 0 ] || { cp")
    assert git(project, "rev-parse", harness.paths.ref).stdout.strip() == harness.run_info["base_commit"]
    harness.close()


def test_swallowed_failed_ask_passes_and_keeps_its_usage(project):  # noqa: F811
    data = _judge_config(project, judge_fail=True)
    data["gates"] = ['autohelix ask "Done?" || true']
    _write(project, data)
    harness = Harness(project, land_mode="manual")
    harness.run(max_iterations=1, report=False)
    record = harness.history.load()[-1]
    assert record.accepted
    judges = [call for call in record.calls if call["role"] == "command"]
    assert judges and all(call["usage"]["status"] == "error" for call in judges)
    assert all(call["log"] and (harness.paths.run_dir / call["log"]).exists() for call in judges)
    assert not any(call["usage"].get("integrity_violation") for call in judges)
    harness.close()


def test_interrupted_ask_records_unknown_cost(project, monkeypatch):  # noqa: F811
    from autohelix.usage_log import append_usage, new_call_id

    configure(project, gates=["true"])
    harness = Harness(project, land_mode="manual")

    def interrupted(*_args, **kwargs):
        append_usage({"id": new_call_id(), "source": "ask", "status": "started", "cost_usd": None},
                     Path(kwargs["extra_env"]["AUTOHELIX_USAGE_FILE"]))
        raise KeyboardInterrupt

    monkeypatch.setattr("autohelix.harness.run_gate_commands", interrupted)
    with pytest.raises(KeyboardInterrupt):
        harness.run_iteration(1)
    record = harness.history.load()[-1]
    assert not record.accepted and record.reason == "interrupted"
    assert record.cost.incomplete
    harness.close()


def test_noop_stop_sees_the_unchanged_tip(project, monkeypatch):  # noqa: F811
    from autohelix.agents import AgentRunResult

    configure(project, stop=['[ "$AUTOHELIX_ITERATION" != 0 ] && '
                             '[ "$(git rev-parse HEAD)" = "$(git rev-parse refs/autohelix/$AUTOHELIX_RUN_ID)" ] && '
                             'git diff --quiet HEAD && echo "same tip"'])
    harness = Harness(project, land_mode="manual")
    base = harness.run_info["base_commit"]
    monkeypatch.setattr(harness, "run_agent", lambda *_: AgentRunResult(success=True, usage={}))
    harness.run(max_iterations=2, report=False)
    records = harness.history.load()
    assert [(r.iteration, r.accepted, r.commit) for r in records] == [(0, True, None), (1, True, None)]
    assert records[1].stop["reason"] == "same tip"
    assert git(project, "rev-parse", harness.paths.ref).stdout.strip() == base
    harness.close()


def test_stop_modifying_files_is_an_error_but_acceptance_stays(project):  # noqa: F811
    configure(project, stop=['[ "$AUTOHELIX_ITERATION" = 0 ] && exit 1; echo stop-edit >> code.py; exit 0'])
    harness = Harness(project, land_mode="manual")
    harness.run(max_iterations=1, report=False)
    record = harness.history.load()[-1]
    assert record.accepted and record.commit and record.stop is None
    assert "stop-edit" not in git(project, "show", f"{harness.paths.ref}:code.py").stdout
    harness.close()


def test_land_refuses_target_checked_out_in_another_worktree(project, tmp_path):  # noqa: F811
    harness = Harness(project, land_mode="manual")
    harness.run(max_iterations=1, report=False)
    target = harness.run_info["target_branch"]
    git(project, "checkout", "-q", "--detach")
    other = tmp_path / "other"
    git(project, "worktree", "add", "-q", str(other), target)
    before = git(project, "rev-parse", target).stdout
    with pytest.raises(RuntimeError, match="checked out in another worktree"):
        land(harness.paths, harness.run_info, harness.run_state.expected_tip)
    assert git(project, "rev-parse", target).stdout == before
    harness.close()


def test_copies_and_the_pin_are_gone_after_a_segment(project):  # noqa: F811
    harness = Harness(project, land_mode="manual")
    harness.run(max_iterations=2, report=False)
    assert not harness.paths.copies.exists()
    assert git(project, "rev-parse", "--verify", harness.paths.pin, check=False).returncode != 0


def test_stale_copies_are_removed_at_segment_start(project):  # noqa: F811
    harness = Harness(project, land_mode="manual")
    stale = harness.paths.copies / "iter-7-agent"
    (stale / "pkg").mkdir(parents=True)
    (stale / "pkg" / "x").write_text("x")
    (stale / "pkg").chmod(0o555)
    harness.paths.copies.joinpath("iter-7-agent.index").write_text("")
    harness.run(max_iterations=1, report=False)
    assert not stale.exists() and not harness.paths.copies.joinpath("iter-7-agent.index").exists()
    harness.close()


def test_posttrain_in_repo_artifacts_fallback_is_an_error(tmp_path):
    example = Path(__file__).resolve().parents[1] / "examples" / "posttrain"
    copy = tmp_path / "posttrain"
    subprocess.run(["cp", "-R", str(example), str(copy)], check=True)
    env = {k: v for k, v in __import__("os").environ.items() if k != "ARTIFACTS"}
    result = subprocess.run(["bash", str(copy / "check_final_model.sh")], capture_output=True,
                            text=True, env=env)
    assert result.returncode == 1
    assert "inside the project" in result.stdout


def test_commit_summary_first_line_from_the_agent_copy(project, monkeypatch):  # noqa: F811
    harness = Harness(project, land_mode="manual")
    real = harness.run_agent

    def agent(cwd, prompt, iteration):
        (cwd / ".autohelix-iteration" / "commit_summary.txt").write_text("First line\nSecond line\n")
        return real(cwd, prompt, iteration)

    monkeypatch.setattr(harness, "run_agent", agent)
    record = harness.run_iteration(1)
    message = git(project, "show", "-s", "--format=%B", record.commit).stdout
    assert message.startswith("First line\n\nAutoHelix iteration 1") and "Second line" not in message
    harness.close()

