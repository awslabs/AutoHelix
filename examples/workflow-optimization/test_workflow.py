"""Contract tests for the editable workflow and frozen benchmark."""

import json

import pytest

import benchmark
import workflow
from benchmark import CASES, POLICIES, FixtureModel, evaluate, execute_action


def test_prompt_contains_request_record_and_selected_policy():
    prompt = workflow.render_prompt(
        case_id="example",
        request="My package arrived damaged.",
        record={"order_id": "A-1"},
        policies=POLICIES,
        policy_names=["returns"],
        feedback="",
    )

    assert "My package arrived damaged." in prompt
    assert '"order_id": "A-1"' in prompt
    assert POLICIES["returns"] in prompt


def test_parse_decision_requires_exact_json_contract():
    decision = workflow.parse_decision(
        '{"action": "escalate", "reason_code": "identity_review"}'
    )
    assert decision == workflow.Decision("escalate", "identity_review")

    with pytest.raises((ValueError, json.JSONDecodeError)):
        workflow.parse_decision(
            '{"action": "escalate", "reason_code": "identity_review", "extra": true}'
        )


def test_solve_request_executes_a_valid_model_decision():
    case = CASES[0]
    result = workflow.solve_request(
        case_id=case.case_id,
        request=case.request,
        record=case.record,
        policies=POLICIES,
        model=FixtureModel(),
        execute=execute_action,
    )

    assert result.error is None
    assert result.decision == case.expected
    assert result.final_state == case.expected_state
    assert [event["stage"] for event in result.trace][-1] == "execute"


def test_invalid_model_output_is_reported_in_trace():
    class InvalidModel:
        def complete(self, prompt):
            return workflow.ModelResponse("not json")

    case = CASES[0]
    result = workflow.solve_request(
        case_id=case.case_id,
        request=case.request,
        record=case.record,
        policies=POLICIES,
        model=InvalidModel(),
        execute=execute_action,
    )

    assert result.error
    assert result.decision is None
    assert result.trace[-1]["stage"] == "error"


def test_benchmark_report_keeps_metrics_and_case_traces():
    report = evaluate(FixtureModel(), CASES)

    assert report["metrics"]["task_success_rate"] == 100
    assert "avg_tokens" in report["metrics"]
    assert "avg_model_cost_usd" in report["metrics"]
    assert len(report["cases"]) == len(CASES)
    assert all(row["trace"] for row in report["cases"])


def test_default_model_uses_tool_free_claude_completion(monkeypatch):
    captured = {}

    class Completed:
        returncode = 0
        stderr = ""
        stdout = json.dumps({"result": '{"action": "deny", "reason_code": "x"}'})

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)
    response = benchmark.ClaudeCliModel(model="haiku", timeout=7).complete("prompt")

    command = captured["command"]
    assert command[0] == "claude"
    assert command[command.index("--tools") + 1] == ""
    assert "--no-session-persistence" in command
    assert command[command.index("--model") + 1] == "haiku"
    assert captured["kwargs"]["input"] == "prompt"
    assert captured["kwargs"]["timeout"] == 7
    assert response.text == '{"action": "deny", "reason_code": "x"}'


def _claude_reply(**fields):
    class Completed:
        returncode = 0
        stderr = ""
        stdout = json.dumps({"result": '{"action": "deny", "reason_code": "x"}', **fields})

    return lambda command, **kwargs: Completed()


def _usage_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_each_model_call_records_its_cost_for_autohelix(monkeypatch, tmp_path):
    usage = tmp_path / "usage.jsonl"
    monkeypatch.setenv("AUTOHELIX_USAGE_FILE", str(usage))
    monkeypatch.setattr(benchmark.subprocess, "run", _claude_reply(
        total_cost_usd=0.0123, modelUsage={"claude-haiku": {}}))
    model = benchmark.ClaudeCliModel(model=None)
    model.complete("one")
    model.complete("two")

    lines = _usage_lines(usage)
    # Each call: a line when it starts (cost unknown), then one with its cost, same id.
    assert [line["cost_usd"] for line in lines] == [None, 0.0123, None, 0.0123]
    assert lines[0]["id"] == lines[1]["id"] != lines[2]["id"] == lines[3]["id"]
    assert {line["source"] for line in lines} == {"workflow-benchmark"}
    assert lines[1]["model"] == "claude-haiku"


def test_a_call_without_a_reported_cost_or_that_fails_is_of_unknown_cost(monkeypatch, tmp_path):
    usage = tmp_path / "usage.jsonl"
    monkeypatch.setenv("AUTOHELIX_USAGE_FILE", str(usage))
    monkeypatch.setattr(benchmark.subprocess, "run", _claude_reply())
    benchmark.ClaudeCliModel(model=None).complete("prompt")
    assert [line["cost_usd"] for line in _usage_lines(usage)] == [None, None]

    def timeout(command, **kwargs):
        raise benchmark.subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(benchmark.subprocess, "run", timeout)
    with pytest.raises(benchmark.ModelError):
        benchmark.ClaudeCliModel(model=None).complete("prompt")
    assert [line["cost_usd"] for line in _usage_lines(usage)] == [None, None, None]


def test_no_usage_file_outside_autohelix(monkeypatch, tmp_path):
    monkeypatch.delenv("AUTOHELIX_USAGE_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(benchmark.subprocess, "run", _claude_reply(total_cost_usd=0.5))
    benchmark.ClaudeCliModel(model=None).complete("prompt")
    assert list(tmp_path.iterdir()) == []
