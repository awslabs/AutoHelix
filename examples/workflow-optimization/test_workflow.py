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
