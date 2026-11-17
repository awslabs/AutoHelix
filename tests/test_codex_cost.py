"""Codex pricing, model resolution, and recorded-usage regression tests."""

import json

import pytest

from autohelix.agents import AgentConfig
from autohelix.agents.codex import CodexAgent
from autohelix.agents.codex_cost import estimate_cost
from autohelix.agents.runner import ProcessRunResult
from autohelix.history import History, IterationResult


USAGE = {
    "input_tokens": 1_000_000,
    "cached_input_tokens": 700_000,
    "cache_write_input_tokens": 200_000,
    "output_tokens": 100_000,
    "reasoning_output_tokens": 50_000,
}
def run_agent(tmp_path, monkeypatch, config=None, *, usage=USAGE, model=None, timed_out=False):
    """A fake Codex call; `model`: the model its output names (on its `thread.started`)."""
    def fake_run_process(**kwargs):
        kwargs["line_callback"](json.dumps({"type": "thread.started", "thread_id": "t",
                                            **({"model": model} if model else {})}))
        if usage is not None:
            kwargs["line_callback"](json.dumps({"type": "turn.completed", "usage": usage}))
        return ProcessRunResult(exit_code=124 if timed_out else 0, timed_out=timed_out)

    monkeypatch.setattr("autohelix.agents.codex.run_process", fake_run_process)
    events = []
    agent = CodexAgent(config or AgentConfig(backend="codex", model="gpt-6.1-sol"))
    result = agent.run(tmp_path, "test", 1, tmp_path / "agent.log", events.append)
    return result, events


def test_prices_cached_reads_and_writes_without_double_counting():
    # $0.20 fresh + $0.07 cached + $0.50 writes + $1.00 output.
    result = estimate_cost(USAGE, "openai.gpt-6.1-sol")
    assert result["cost_usd"] == pytest.approx(1.77)
    assert result["cost_source"] == "calculated"
    assert result["cost_model"] == "openai.gpt-6.1-sol"
    assert result["cost_basis"] == "openai_standard_api"
    assert result["cost_rates"] == {
        "input": 2.0, "cached_input": 0.1, "output": 10.0, "cache_write": 2.5,
        "unit": "usd_per_million_tokens", "cache_write_ttl": "30m", "verified_at": "2026-11-06",
    }


def test_reasoning_added_only_with_explicit_exclusion():
    assert estimate_cost(USAGE, "gpt-6.1-sol")["cost_usd"] == pytest.approx(1.77)
    assert estimate_cost(
        USAGE, "gpt-6.1-sol", output_includes_reasoning=False,
    )["cost_usd"] == pytest.approx(2.27)


def test_separate_reasoning_requires_a_reported_count():
    usage = {key: value for key, value in USAGE.items() if key != "reasoning_output_tokens"}
    assert "cost_usd" not in estimate_cost(usage, "gpt-6.1-sol", output_includes_reasoning=False)


def test_inclusive_reasoning_cannot_exceed_output():
    assert "cost_usd" not in estimate_cost(
        {**USAGE, "reasoning_output_tokens": 200_000}, "gpt-6.1-sol",
    )


def test_cumulative_input_does_not_trigger_per_request_long_context_surcharge():
    usage = {"input_tokens": 1_000_000, "cached_input_tokens": 0, "output_tokens": 0}
    assert estimate_cost(usage, "gpt-6.1-sol")["cost_usd"] == 2.0


def test_older_codex_model_prices_without_separate_writes():
    usage = {"input_tokens": 1000, "cached_input_tokens": 900, "output_tokens": 100}
    assert estimate_cost(usage, "gpt-5.3-codex")["cost_usd"] == pytest.approx(0.0017325)


@pytest.mark.parametrize("model", [None, "new-model", "gpt-6.1-sol-custom", "gpt-5.6-sol"])
def test_unknown_or_expiring_prices_are_unavailable(model):
    result = estimate_cost(USAGE, model)
    assert "cost_usd" not in result
    assert "No API pricing" in result["cost_unavailable_reason"]


@pytest.mark.parametrize("field,value", [
    ("input_tokens", -1), ("output_tokens", None), ("cached_input_tokens", True),
    ("cache_write_input_tokens", 200_000.5), ("reasoning_output_tokens", "50"),
    ("cached_input_tokens", 900_000),
])
def test_invalid_counts_never_become_a_dollar_amount(field, value):
    assert "cost_usd" not in estimate_cost({**USAGE, field: value}, "gpt-6.1-sol")


@pytest.mark.parametrize("field", ["input_tokens", "cached_input_tokens", "output_tokens"])
def test_missing_required_counts_are_unavailable(field):
    usage = USAGE.copy()
    del usage[field]
    assert "cost_usd" not in estimate_cost(usage, "gpt-6.1-sol")


def test_unpriced_cache_writes_are_unavailable():
    assert "cost_usd" not in estimate_cost(USAGE, "gpt-5.3-codex")


def test_zero_usage_is_a_known_zero_cost():
    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    assert estimate_cost(usage, "gpt-6.1-sol")["cost_usd"] == 0


def test_the_model_codex_output_names_is_priced_over_agent_model(tmp_path, monkeypatch):
    result, _ = run_agent(tmp_path, monkeypatch, AgentConfig(backend="codex", model="gpt-6-luna"),
                          usage={**USAGE, "output_includes_reasoning": False}, model="openai.gpt-6.1-sol")
    assert result.success
    assert result.usage["cost_usd"] == pytest.approx(2.27)
    assert result.usage["cost_model"] == "openai.gpt-6.1-sol"
    assert result.usage["usage_complete"] is True
    assert result.usage["cache_write_input_tokens"] == 200_000


def test_without_a_reported_model_agent_model_prices_it(tmp_path, monkeypatch):
    result, _ = run_agent(tmp_path, monkeypatch, AgentConfig(backend="codex", model="gpt-6.1-sol"))
    assert result.usage["cost_usd"] == pytest.approx(1.77) and result.usage["cost_model"] == "gpt-6.1-sol"


@pytest.mark.parametrize("extra_args", [["-m", "gpt-6-luna"], ["resume", "t"], ["--profile", "other"]])
def test_extra_args_dont_change_the_pricing(tmp_path, monkeypatch, extra_args):
    """No guessing from arguments: the reported model, else agent.model."""
    result, _ = run_agent(tmp_path, monkeypatch,
                          AgentConfig(backend="codex", model="gpt-6.1-sol", extra_args=extra_args))
    assert result.usage["cost_usd"] == pytest.approx(1.77)


@pytest.mark.parametrize("config", [AgentConfig(backend="codex"),
                                    AgentConfig(backend="codex", settings={"model": "gpt-6.1-sol"})])
def test_no_model_anywhere_says_set_agent_model(tmp_path, monkeypatch, config):
    result, events = run_agent(tmp_path, monkeypatch, config)
    assert "cost_usd" not in result.usage
    assert result.usage["cost_unavailable_reason"].startswith("set agent.model")
    assert any(event.text.startswith("Cost unavailable: set agent.model") for event in events)


def test_codex_session_files_are_never_read(tmp_path, monkeypatch):
    """A session folder naming another model changes nothing."""
    sessions = tmp_path / "codex" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr("pathlib.Path.glob", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read")))
    result, _ = run_agent(tmp_path, monkeypatch, AgentConfig(backend="codex", model="gpt-6.1-sol"))
    assert result.usage["cost_usd"] == pytest.approx(1.77)


@pytest.mark.parametrize("timed_out,usage", [(True, None), (True, USAGE), (False, None)])
def test_incomplete_process_is_not_priced_as_free(tmp_path, monkeypatch, timed_out, usage):
    result, _ = run_agent(tmp_path, monkeypatch, usage=usage, timed_out=timed_out)
    assert "cost_usd" not in result.usage
    assert result.usage["usage_complete"] is False
    assert result.usage["duration_ms"] >= 0


def test_cumulative_completion_snapshot_replaces_previous_counts(tmp_path, monkeypatch):
    def fake_run_process(**kwargs):
        for input_tokens in [1000, 2000]:
            kwargs["line_callback"](json.dumps({
                "type": "turn.completed", "usage": {
                    "input_tokens": input_tokens, "cached_input_tokens": 0, "output_tokens": 0,
                },
            }))
        return ProcessRunResult(exit_code=0)

    monkeypatch.setattr("autohelix.agents.codex.run_process", fake_run_process)
    agent = CodexAgent(AgentConfig(backend="codex", model="gpt-6.1-sol"))
    result = agent.run(tmp_path, "test", 1, tmp_path / "agent.log", lambda event: None)
    assert result.usage["cost_usd"] == pytest.approx(0.004)


def test_duration_is_measured_even_without_backend_duration(tmp_path, monkeypatch):
    timestamps = iter([100.0, 102.5])
    monkeypatch.setattr("autohelix.agents.codex.time.monotonic", lambda: next(timestamps))
    result, _ = run_agent(tmp_path, monkeypatch)
    assert result.usage["duration_ms"] == 2500


def test_cost_and_provenance_survive_history_round_trip(tmp_path, monkeypatch):
    result, _ = run_agent(tmp_path, monkeypatch)
    history = History(tmp_path)
    history.append(IterationResult(iteration=1, accepted=True, metrics={}, calls=[{'role': 'agent', 'usage': result.usage}]))
    usage = history.load()[0].usage
    assert usage["cost_usd"] == pytest.approx(1.77)
    assert usage["cost_source"] == "calculated"
    assert usage["cost_model"] == "gpt-6.1-sol"
    assert usage["cost_rates"]["cache_write"] == 2.5
