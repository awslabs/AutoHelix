"""Stop conditions: `stop:` commands that end a run early when one exits 0."""

import json
import subprocess

import pytest

from autohelix.checks import run_stop_checks
from autohelix.config import Config
from autohelix.harness import AutoHelixRunError, Harness
from autohelix.parallel import core as parallel
from autohelix.history import History
from autohelix.prompt_template import build_prompt_variables
from autohelix.run_status import read_run_status
from autohelix.watch import status_line


def _git(project, *args):
    subprocess.run(["git", *args], cwd=project, capture_output=True, check=True)


@pytest.fixture
def project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init")
    _git(project, "config", "user.email", "test@test.com")
    _git(project, "config", "user.name", "Test")
    (project / "hello.py").write_text('print("hello")\n')
    (project / ".gitignore").write_text(".autohelix/\n")
    (project / ".autohelix").mkdir()
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "init")
    return project


def _configure(project, extra: str) -> None:
    (project / "autohelix.yaml").write_text(f"""
goal: Test stop conditions

agent:
  type: mock
  settings:
    change_file: hello.py
    change_content: "# iteration {{iteration}}"

{extra}
""")
    _git(project, "add", "autohelix.yaml")
    _git(project, "commit", "-m", "config")


def _run(project, n: int) -> list:
    Harness(project, verbose=False).run(max_iterations=n, report=False)
    return History(project).load()


# --- config ---------------------------------------------------------------


def test_parses_strings_mappings_and_single_entries():
    config = Config.from_dict({"goal": "g", "stop": ["a", {"command": "b", "timeout": 7}]})
    assert [(c.command, c.timeout) for c in config.stop][1] == ("b", 7)
    assert config.stop[0].command == "a"
    assert [c.command for c in Config.from_dict({"goal": "g", "stop": "only"}).stop] == ["only"]
    assert [c.command for c in Config.from_dict({"goal": "g", "stop": {"command": "x"}}).stop] == ["x"]
    assert Config.from_dict({"goal": "g"}).stop == []


def test_stop_is_a_known_key_and_empty_commands_are_errors():
    raw = {"goal": "real goal", "constraints": ["true"], "stop": [{"command": " "}]}
    issues = Config.from_dict(raw).validate(raw)
    messages = [i.message for i in issues]
    assert "Stop command is empty" in messages
    assert not any("Unknown config key" in m for m in messages)


# --- runner ---------------------------------------------------------------


def test_exit_codes_reason_and_order(tmp_path):
    config = Config.from_dict({"goal": "g", "stop": [
        "echo not yet; exit 1",
        "echo broken >&2; exit 3",
        "echo checking; echo 'score 9 >= 8'; echo noise >&2",
        "echo never run",
    ]})
    results = run_stop_checks(config, tmp_path)
    assert [r.return_code for r in results] == [1, 3, 0]
    assert [r.stop for r in results] == [False, False, True]
    assert [r.error for r in results] == [False, True, False]
    assert results[2].reason == "score 9 >= 8"  # last stdout line, not stderr


def test_reason_defaults_to_command_and_env_is_passed(tmp_path):
    config = Config.from_dict({"goal": "g", "stop": ['[ "$AUTOHELIX_ITERATION" = 4 ]']})
    [r] = run_stop_checks(config, tmp_path, extra_env={"AUTOHELIX_ITERATION": "4"})
    assert r.stop and r.reason == config.stop[0].command


def test_timeout_is_an_error(tmp_path):
    config = Config.from_dict({"goal": "g", "stop": [{"command": "sleep 5", "timeout": 0}]})
    [r] = run_stop_checks(config, tmp_path)
    assert r.error and not r.stop


# --- harness --------------------------------------------------------------


def test_condition_stops_the_run_and_is_recorded(project):
    _configure(project, """
stop:
  - '[ "$AUTOHELIX_ITERATION" -ge 2 ] && echo "reached iteration $AUTOHELIX_ITERATION"'
""")
    records = _run(project, 5)
    assert [r.iteration for r in records] == [0, 1, 2]
    assert records[1].stop is None
    assert records[2].stop["reason"] == "reached iteration 2"

    status = read_run_status(project)
    assert status["state"] == "finished"
    assert status["stop_reason"]["kind"] == "condition"
    assert status["stop_reason"]["iteration"] == 2
    assert "reached iteration 2" in status["stop_reason"]["detail"]
    assert "reached iteration 2" in status_line(status)


def test_resuming_after_a_stop_continues(project):
    _configure(project, """
stop:
  - '[ "$AUTOHELIX_ITERATION" -ge 2 ]'
""")
    _run(project, 5)
    records = _run(project, 5)
    assert [r.iteration for r in records] == [0, 1, 2, 3]
    assert records[3].stop is not None


def test_metrics_reach_the_command_and_a_met_baseline_runs_nothing(project):
    _configure(project, """
metrics:
  - command: "echo '##autohelix[score=7]'"
    values:
      score: higher

stop:
  - python3 -c "import json, os, sys; m = json.load(open(os.environ['AUTOHELIX_METRICS'])); print('score', m['score']); sys.exit(m['score'] < 7)"
""")
    records = _run(project, 3)
    assert [r.iteration for r in records] == [0]
    assert records[0].stop["reason"] == "score 7.0"
    reason = read_run_status(project)["stop_reason"]
    assert reason["kind"] == "condition" and reason["iteration"] == 0


def test_broken_command_fails_on_the_baseline_before_recording(project):
    _configure(project, "stop:\n  - exit 3\n")
    with pytest.raises(AutoHelixRunError, match="Stop command failed on the baseline"):
        Harness(project, verbose=False).run(max_iterations=2, report=False)
    records = History(project).load()
    assert len(records) == 1 and records[0].iteration == 0 and not records[0].accepted


def test_command_errors_after_the_baseline_only_warn(project):
    _configure(project, """
stop:
  - '[ "$AUTOHELIX_ITERATION" = 0 ] && exit 1; exit 3'
""")
    records = _run(project, 2)
    assert [r.iteration for r in records] == [0, 1, 2]
    assert all(r.stop is None for r in records)
    assert read_run_status(project)["stop_reason"]["budget"] == "iterations"


def test_rejected_iterations_are_not_checked(project):
    # Would stop on any iteration after the baseline, but every iteration is rejected.
    _configure(project, """
constraints:
  - '! grep -q iteration hello.py'

stop:
  - '[ "$AUTOHELIX_ITERATION" != 0 ]'
""")
    records = _run(project, 2)
    assert [(r.iteration, r.accepted) for r in records] == [(0, True), (1, False), (2, False)]
    assert all(r.stop is None for r in records)


def test_commands_see_the_accepted_candidate(project):
    _configure(project, """
stop:
  - 'grep -q "iteration 1" hello.py && echo found'
""")
    records = _run(project, 3)
    assert [r.iteration for r in records] == [0, 1]
    assert records[1].stop["reason"] == "found"


def test_interrupt_during_stop_check_still_records_the_merge(project, monkeypatch):
    _configure(project, "stop:\n  - exit 1\n")
    harness = Harness(project, verbose=False)
    original = harness._check_stop

    def interrupt_after_baseline(workspace, iteration, metrics):
        if iteration == 0:
            return original(workspace, iteration, metrics)
        raise KeyboardInterrupt

    monkeypatch.setattr(harness, "_check_stop", interrupt_after_baseline)
    harness.run(max_iterations=3, report=False)  # the loop handles the interrupt
    records = History(project).load()
    assert [(r.iteration, r.accepted) for r in records] == [(0, True), (1, True)]
    assert records[1].commit
    assert read_run_status(project)["state"] == "interrupted"


def test_budget_reasons_without_stop_conditions(project):
    _configure(project, "")
    _run(project, 2)
    reason = read_run_status(project)["stop_reason"]
    assert reason == {"kind": "budget", "budget": "iterations",
                      "detail": "iteration budget reached (2)"}


def test_prompt_lists_stop_commands(project):
    config = Config.from_dict({"goal": "g", "stop": ["./done.sh"]})
    variables = build_prompt_variables(config, History(project), 1, project)
    assert variables["stop"] == ["./done.sh"]


def test_history_without_stop_field_loads(project):
    path = project / ".autohelix" / "history.jsonl"
    path.write_text(json.dumps({"iteration": 0, "accepted": True, "metrics": {}}) + "\n")
    assert History(project).load()[0].stop is None


def test_bad_command_shapes_are_config_errors():
    for raw in ({"goal": "g", "stop": 5}, {"goal": "g", "stop": [["x"]]}, {"goal": "g", "constraints": 5}):
        messages = [i.message for i in Config.from_dict(raw).validate()]
        assert any("expected a command" in m or "each entry must be" in m for m in messages), raw


@pytest.mark.parametrize("key", ["constraints", "stop"])
@pytest.mark.parametrize("entry,message", [
    ({"command": 1}, "command must be a string"),
    ({"command": None}, "command must be a string"),
    ({"command": "true", "timeout": None}, "timeout must be an integer"),
    ({"command": "true", "timeout": "invalid"}, "timeout must be an integer"),
])
def test_bad_command_fields_are_config_errors(key, entry, message):
    config = Config.from_dict({"goal": "g", key: [entry]})
    assert any(issue.level == "error" and message in issue.message for issue in config.validate())


def test_constraint_mapping_parses_as_one_command():
    config = Config.from_dict({"goal": "g", "constraint": {"command": "pytest", "timeout": 5}})
    assert [(c.command, c.timeout) for c in config.constraints] == [("pytest", 5)]


def test_stop_check_failure_after_merge_keeps_the_iteration(project, monkeypatch):
    _configure(project, "stop:\n  - exit 1\n")
    harness = Harness(project, verbose=False)
    original = harness._check_stop

    def fail_after_baseline(workspace, iteration, metrics):
        if iteration == 0:
            return original(workspace, iteration, metrics)
        raise OSError("disk full")

    monkeypatch.setattr(harness, "_check_stop", fail_after_baseline)
    harness.run(max_iterations=2, report=False)
    records = History(project).load()
    assert [(r.iteration, r.accepted, r.stop) for r in records] == [(0, True, None), (1, True, None), (2, True, None)]


def test_cost_budget_reason(project, monkeypatch):
    _configure(project, "budget:\n  cost: 0.10\n")
    harness = Harness(project, verbose=False)
    original = harness.run_iteration

    def costly(iteration):
        result = original(iteration)
        result.usage["cost_usd"] = 0.06
        return result

    monkeypatch.setattr(harness, "run_iteration", costly)
    harness.run(max_iterations=10, report=False)
    assert read_run_status(project)["stop_reason"]["budget"] == "cost"


def test_report_prompt_says_why_the_run_ended(project):
    from autohelix.report import build_report_prompt

    _configure(project, "stop:\n  - '[ \"$AUTOHELIX_ITERATION\" -ge 1 ] && echo done enough'\n")
    _run(project, 3)
    config = Config.from_dict({"goal": "g"})
    prompt = build_report_prompt(config, project, None, project / "draft.md")
    assert "The run ended because: stop condition met at iteration 1: done enough." in prompt


@pytest.mark.parametrize("reason", [
    {"kind": "condition"},
    {"kind": "budget", "budget": "cost"},
    {"kind": "budget", "budget": "time"},
])
@pytest.mark.parametrize("no_report", [False, True])
def test_parallel_worker_stops_early_and_respects_report_option(tmp_path, monkeypatch, reason, no_report):
    segments, reports = [], []
    worker = parallel.Worker(index=1, name="w", config_file=tmp_path / "c.yaml", worktree=tmp_path,
                             branch="b", shared_dir=tmp_path, budget=5)
    monkeypatch.setattr(parallel, "_run_segment", lambda w, upto, verbose, args: segments.append(upto))
    monkeypatch.setattr(parallel, "sync_own_notes", lambda w: None)
    monkeypatch.setattr(parallel, "_write_worker_report", lambda w: reports.append(w.name))
    monkeypatch.setattr(parallel, "read_run_status",
                        lambda *a: {"stop_reason": reason} if len(segments) >= 2 else {})
    parallel.run_worker(worker, sync_every=1, verbose=False,
                        run_args=["--no-report"] if no_report else None)
    assert worker.error is None
    assert segments == [1, 2]
    assert reports == ([] if no_report else ["w"])
