"""Reviewer charges in iteration totals, baseline accounting, and budgets."""

import json
from types import SimpleNamespace

import pytest

from autohelix.agents import AgentRunResult, create_agent
from autohelix.config import ReviewerConfig
from autohelix.costs import summarize_costs
from autohelix.harness import Harness
from autohelix.history import History, IterationResult
from autohelix.viewer import RunData
from tests.test_integration import integration_project  # noqa: F401


def _harness(project, monkeypatch, *, reviewer_cost=0.04, success=True, write_review=True):
    monkeypatch.setattr("autohelix.harness.create_agent", create_agent)
    harness = Harness(project)
    harness.config.agent.settings["cost_usd"] = 0.10
    harness.config.reviewer = ReviewerConfig(model="review-model")
    harness.config.report = False
    calls = []

    def review(**kwargs):
        calls.append(kwargs["iteration"])
        if write_review:
            (kwargs["worktree_path"] / ".autohelix" / "review.md").write_text("Review")
        usage = {"input_tokens": 50, "cost_model": "review-model"}
        if reviewer_cost is not None:
            usage["cost_usd"] = reviewer_cost
        return AgentRunResult(success=success, usage=usage)

    monkeypatch.setattr("autohelix.harness.create_agent", lambda *a, **k: SimpleNamespace(run=review))
    return harness, calls


@pytest.mark.parametrize("success,write_review,reviewer_cost,expected,incomplete", [
    (True, True, 0.04, 0.14, False),
    (False, False, 0.04, 0.14, False),
    (True, False, 0.04, 0.14, False),
    (True, True, None, 0.10, True),
])
def test_iteration_includes_review_charge_even_on_failure(
    integration_project, monkeypatch, capsys, success, write_review, reviewer_cost, expected, incomplete,
):
    harness, calls = _harness(
        integration_project, monkeypatch, success=success,
        write_review=write_review, reviewer_cost=reviewer_cost,
    )
    result = harness.run_iteration(1)
    harness._print_iteration_summary(result)
    harness._print_summary()

    assert result.accepted
    assert calls == [1]
    saved = History(integration_project).load()[0]
    assert saved.usage["cost_usd"] == 0.10
    assert saved.reviewer_usage[0]["input_tokens"] == 50
    assert saved.reviewer_usage[0]["cost_model"] == "review-model"
    assert saved.cost.cost_usd == pytest.approx(expected)
    assert saved.cost.incomplete == incomplete
    output = capsys.readouterr().out
    assert f"${expected:.2f}" + (" (incomplete)" if incomplete else "") in output
    state = RunData(integration_project).state()
    assert state["iterations"][0]["cost"] == pytest.approx(expected)
    assert state["iterations"][0]["cost_incomplete"] == incomplete
    assert state["cost"]["cost_usd"] == pytest.approx(expected)
    assert state["cost"]["incomplete"] == incomplete


def test_review_skipped_after_failed_constraints_is_not_unknown(integration_project, monkeypatch):
    harness, calls = _harness(integration_project, monkeypatch)
    harness.config.constraints[0].command = "false"
    result = harness.run_iteration(1)
    assert not result.accepted
    assert calls == []
    assert result.reviewer_usage == []
    assert result.cost.cost_usd == 0.10
    assert not result.cost.incomplete


def test_baseline_and_iteration_reviews_count_toward_budget_on_resume(
    integration_project, monkeypatch, capsys,
):
    harness, calls = _harness(integration_project, monkeypatch)
    harness.config.max_cost_usd = 0.30
    harness.run(max_iterations=1)
    assert calls == [0, 1]
    history = History(integration_project)
    assert summarize_costs(history.cost_usages).cost_usd == pytest.approx(0.18)

    resumed, calls = _harness(integration_project, monkeypatch)
    resumed.config.max_cost_usd = 0.30
    resumed.run(max_iterations=10)
    assert calls == [2]
    assert [r.iteration for r in history.load()] == [0, 1, 2]
    assert summarize_costs(history.cost_usages).cost_usd == pytest.approx(0.32)
    assert "$0.32" in capsys.readouterr().out


def test_baseline_review_alone_can_exhaust_budget(integration_project, monkeypatch, capsys):
    harness, calls = _harness(integration_project, monkeypatch, reviewer_cost=0.25)
    harness.config.max_cost_usd = 0.20
    assert not harness.run(max_iterations=10)
    assert calls == [0]
    assert [r.iteration for r in harness.history.load()] == [0]
    assert "$0.25" in capsys.readouterr().out
    state = RunData(integration_project).state()
    assert state["cost"] == {"cost_usd": 0.25, "incomplete": False}

    resumed, calls = _harness(integration_project, monkeypatch, reviewer_cost=0.25)
    resumed.config.max_cost_usd = 0.20
    assert not resumed.run(max_iterations=10)
    assert calls == []


def test_failed_baseline_retry_preserves_every_charge(integration_project, monkeypatch):
    harness, calls = _harness(integration_project, monkeypatch, success=False, write_review=False)
    harness._capture_baseline()
    assert calls == [0]
    resumed, calls = _harness(integration_project, monkeypatch, success=False, write_review=False)
    resumed.config.max_cost_usd = 0.08
    assert not resumed.run(max_iterations=10)
    baseline = History(integration_project).load()[0]
    assert calls == [0]
    assert len(baseline.reviewer_usage) == 2
    assert baseline.cost.cost_usd == 0.08
    assert not baseline.cost.incomplete


def test_interrupted_baseline_review_preserves_unknown_charge(integration_project, monkeypatch):
    harness, _ = _harness(integration_project, monkeypatch)

    def interrupt_review(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(harness, "run_reviewer", interrupt_review)
    with pytest.raises(KeyboardInterrupt):
        harness._capture_baseline()
    history = History(integration_project)
    assert history.load()[0].reviewer_usage == [{}]
    assert summarize_costs(history.cost_usages).incomplete

    resumed, calls = _harness(integration_project, monkeypatch)
    resumed.run(max_iterations=1)
    assert calls == [0, 1]
    baseline = history.load()[0]
    assert len(baseline.reviewer_usage) == 2
    total = summarize_costs(history.cost_usages)
    assert total.cost_usd == pytest.approx(0.18)
    assert total.incomplete


def test_unknown_review_cost_warns_but_known_spend_still_enforces_budget(
    integration_project, monkeypatch, capsys,
):
    harness, calls = _harness(integration_project, monkeypatch, reviewer_cost=None)
    harness.config.max_cost_usd = 0.20
    harness.run(max_iterations=10)
    assert calls == [0, 1, 2]
    assert len(harness.history.load()) == 3
    output = capsys.readouterr().out
    assert "budget.cost can only track known costs" in output
    assert "$0.20 (incomplete)" in output


def test_report_generation_cost_remains_separate(tmp_path):
    history = History(tmp_path)
    history.append(IterationResult(
        iteration=0, accepted=True, metrics={}, reviewer_usage=[{"cost_usd": 0.04}],
    ))
    history.append(IterationResult(
        iteration=1, accepted=True, metrics={}, usage={"cost_usd": 0.10},
        reviewer_usage=[{"cost_usd": 0.04}],
    ))
    output = tmp_path / ".autohelix" / "output"
    output.mkdir()
    (output / "report.md").write_text("Summary")
    (output / "report.json").write_text(json.dumps({"cost_usd": 0.99}))
    state = RunData(tmp_path).state()
    assert state["cost"]["cost_usd"] == pytest.approx(0.18)
    assert state["report"]["cost_usd"] == 0.99


def test_terminal_shows_baseline_review_and_marks_partial_iteration_cost(
    integration_project, monkeypatch, capsys,  # noqa: F811
):
    harness, _ = _harness(integration_project, monkeypatch, reviewer_cost=None)
    harness.run(max_iterations=1)
    out = capsys.readouterr().out
    assert "baseline review │ cost unavailable" in out
    # Agent cost known, review cost not: the line shows the known part with "+",
    # in the same column width as a plain amount.
    line = next(l for l in out.splitlines() if l.lstrip().startswith("iter 1"))
    assert " $0.10+ │" in line


def test_reviewer_usage_needs_an_existing_record(tmp_path):
    with pytest.raises(ValueError, match="No history record for iteration 0"):
        History(tmp_path).record_reviewer_usage(0, {})
