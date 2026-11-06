"""Codex pricing, model resolution, and recorded-usage regression tests."""

import json
from datetime import datetime, timezone
from uuid import UUID

import pytest

from autohelix.agents import AgentConfig
from autohelix.agents.codex import CodexAgent
from autohelix.agents.codex_cost import estimate_cost, session_metadata
from autohelix.agents.runner import ProcessRunResult
from autohelix.history import History, IterationResult


USAGE = {
    "input_tokens": 1_000_000,
    "cached_input_tokens": 700_000,
    "cache_write_input_tokens": 200_000,
    "output_tokens": 100_000,
    "reasoning_output_tokens": 50_000,
}
CREATED = datetime(2026, 10, 27, 22, 39, 46, tzinfo=timezone.utc)
THREAD_ID = str(UUID(int=(int(CREATED.timestamp() * 1000) << 80) | (7 << 76) | (2 << 62) | 1))


def rollout(tmp_path, monkeypatch, *, model="openai.gpt-6.1-sol", reasoning_included=None):
    """A model supplied by the actual session, independent of caller config."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    directory = tmp_path / "sessions" / CREATED.strftime("%Y/%m/%d")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-example-{THREAD_ID}.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": THREAD_ID, "model_provider": "amazon-bedrock"}},
        {"type": "turn_context", "payload": {"model": model}},
    ]
    if reasoning_included is not None:
        records.append({"type": "event_msg", "payload": {
            "type": "token_count", "info": {"last_usage_includes_reasoning": reasoning_included},
        }})
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def run_agent(tmp_path, monkeypatch, config=None, *, usage=USAGE, thread_id=None, timed_out=False):
    def fake_run_process(**kwargs):
        if thread_id:
            kwargs["line_callback"](json.dumps({"type": "thread.started", "thread_id": thread_id}))
        if usage is not None:
            kwargs["line_callback"](json.dumps({"type": "turn.completed", "usage": usage}))
        return ProcessRunResult(exit_code=124 if timed_out else 0, timed_out=timed_out)

    monkeypatch.setattr("autohelix.agents.codex.run_process", fake_run_process)
    events = []
    agent = CodexAgent(config or AgentConfig(type="codex", model="gpt-6.1-sol"))
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


def test_session_uses_thread_creation_date_and_actual_model(tmp_path, monkeypatch):
    rollout(tmp_path, monkeypatch, reasoning_included=False)
    # The current date is different from the recorded thread's creation date.
    result = session_metadata(THREAD_ID, datetime(2026, 11, 6, tzinfo=timezone.utc))
    assert result == {
        "model": "openai.gpt-6.1-sol", "provider": "amazon-bedrock",
        "output_includes_reasoning": False,
    }


def test_session_lookup_accepts_local_date_before_utc_date(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    # 03:41 UTC on November 5 is still November 4 in New York.
    created = datetime(2026, 11, 5, 3, 41, tzinfo=timezone.utc)
    uid = str(UUID(int=(int(created.timestamp() * 1000) << 80) | (7 << 76) | (2 << 62) | 1))
    directory = tmp_path / "sessions" / "2026/11/04"
    directory.mkdir(parents=True)
    (directory / f"rollout-local-{uid}.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": uid, "model_provider": "amazon-bedrock"}}) + "\n"
        + json.dumps({"type": "turn_context", "payload": {"model": "openai.gpt-6.1-sol"}}) + "\n"
    )
    assert session_metadata(uid, created)["model"] == "openai.gpt-6.1-sol"


def test_duplicate_thread_files_are_ambiguous(tmp_path, monkeypatch):
    path = rollout(tmp_path, monkeypatch)
    second_directory = tmp_path / "sessions" / "2026/10/26"
    second_directory.mkdir(parents=True)
    (second_directory / path.name).write_bytes(path.read_bytes())
    assert session_metadata(THREAD_ID, CREATED) == {}


def test_session_metadata_requires_matching_session_id(tmp_path, monkeypatch):
    path = rollout(tmp_path, monkeypatch)
    path.write_text(path.read_text().replace(THREAD_ID, "another-thread"))
    assert session_metadata(THREAD_ID, CREATED) == {}


@pytest.mark.parametrize("thread_id", [None, "../../secret", "*", "", "not-a-uuid", 12])
def test_invalid_thread_ids_do_not_read_rollouts(tmp_path, monkeypatch, thread_id):
    rollout(tmp_path, monkeypatch)
    assert session_metadata(thread_id, CREATED) == {}


def test_missing_or_unreadable_rollout_is_harmless(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert session_metadata(THREAD_ID, CREATED) == {}
    path = rollout(tmp_path, monkeypatch)
    path.unlink()
    path.mkdir()
    assert session_metadata(THREAD_ID, CREATED) == {}


def test_reasoning_marker_in_tail_of_large_rollout(tmp_path, monkeypatch):
    path = rollout(tmp_path, monkeypatch)
    with path.open("a") as log:
        log.write(json.dumps({"type": "response_item", "payload": "x" * 1_100_000}) + "\n")
        log.write(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "info": {"last_usage_includes_reasoning": False},
        }}) + "\n")
    assert session_metadata(THREAD_ID, CREATED)["output_includes_reasoning"] is False


def test_model_change_in_middle_of_large_rollout_is_detected(tmp_path, monkeypatch):
    path = rollout(tmp_path, monkeypatch)
    with path.open("a") as log:
        log.write(json.dumps({"type": "response_item", "payload": "x" * 600_000}) + "\n")
        log.write(json.dumps({"type": "turn_context", "payload": {"model": "gpt-6-astra"}}) + "\n")
        log.write(json.dumps({"type": "response_item", "payload": "x" * 600_000}) + "\n")
        log.write(json.dumps({"type": "turn_context", "payload": {"model": "openai.gpt-6.1-sol"}}) + "\n")
    result, _ = run_agent(tmp_path, monkeypatch, thread_id=THREAD_ID)
    assert "cost_usd" not in result.usage
    assert "multiple models" in result.usage["cost_unavailable_reason"]


@pytest.mark.parametrize("limit", ["record", "total"])
def test_session_read_limits_make_cost_unavailable(tmp_path, monkeypatch, limit):
    import autohelix.agents.codex_cost as pricing

    path = rollout(tmp_path, monkeypatch)
    if limit == "record":
        monkeypatch.setattr(pricing, "_MAX_RECORD_BYTES", 1024)
    else:
        monkeypatch.setattr(pricing, "_MAX_SESSION_BYTES", 1024)
    with path.open("a") as log:
        log.write(json.dumps({"type": "response_item", "payload": "x" * 2048}) + "\n")
    result, _ = run_agent(tmp_path, monkeypatch, thread_id=THREAD_ID)
    assert "cost_usd" not in result.usage
    assert "bounded read" in result.usage["cost_unavailable_reason"]


def test_multiple_models_in_rollout_are_not_priced_as_one(tmp_path, monkeypatch):
    path = rollout(tmp_path, monkeypatch)
    with path.open("a") as log:
        log.write(json.dumps({"type": "turn_context", "payload": {"model": "gpt-6-astra"}}) + "\n")
    result, _ = run_agent(tmp_path, monkeypatch, thread_id=THREAD_ID)
    assert "cost_usd" not in result.usage
    assert "multiple models" in result.usage["cost_unavailable_reason"]


def test_invalid_model_context_does_not_reuse_an_earlier_model(tmp_path, monkeypatch):
    path = rollout(tmp_path, monkeypatch)
    with path.open("a") as log:
        log.write(json.dumps({"type": "turn_context", "payload": {}}) + "\n")
    result, _ = run_agent(tmp_path, monkeypatch, thread_id=THREAD_ID)
    assert "cost_usd" not in result.usage
    assert "invalid model context" in result.usage["cost_unavailable_reason"]


def test_default_model_resolved_from_rollout_overrides_config(tmp_path, monkeypatch):
    rollout(tmp_path, monkeypatch, reasoning_included=False)
    result, _ = run_agent(
        tmp_path, monkeypatch, AgentConfig(type="codex", model="gpt-6-luna"),
        thread_id=THREAD_ID,
    )
    assert result.success
    assert result.usage["cost_usd"] == pytest.approx(2.27)
    assert result.usage["cost_model"] == "openai.gpt-6.1-sol"
    assert result.usage["cost_provider"] == "amazon-bedrock"
    assert result.usage["usage_complete"] is True
    assert result.usage["cache_write_input_tokens"] == 200_000


def test_omitted_model_resolved_from_rollout(tmp_path, monkeypatch):
    rollout(tmp_path, monkeypatch)
    result, _ = run_agent(
        tmp_path, monkeypatch, AgentConfig(type="codex"), thread_id=THREAD_ID,
    )
    assert result.usage["cost_usd"] == pytest.approx(1.77)


@pytest.mark.parametrize("config", [
    AgentConfig(type="codex", model="gpt-6.1-sol", extra_args=["--ephemeral"]),
    AgentConfig(type="codex", settings={"model": "openai.gpt-6.1-sol"}),
])
def test_explicit_model_can_price_ephemeral_usage(tmp_path, monkeypatch, config):
    result, _ = run_agent(tmp_path, monkeypatch, config)
    assert result.usage["cost_usd"] == pytest.approx(1.77)


@pytest.mark.parametrize("extra_args", [
    ["--model=gpt-6-luna"], ["-m", "gpt-6-luna"],
    ["-mgpt-6-luna"], ['-cmodel="gpt-6-luna"'], ["-pother"],
    ["-c", 'model="gpt-6-luna"'], ["--profile", "other"], ["resume", THREAD_ID],
])
def test_ambiguous_or_resumed_usage_is_not_guessed(tmp_path, monkeypatch, extra_args):
    result, events = run_agent(
        tmp_path, monkeypatch,
        AgentConfig(type="codex", model="gpt-6.1-sol", extra_args=extra_args),
    )
    assert "cost_usd" not in result.usage
    assert any("Cost unavailable" in event.text for event in events)


def test_missing_model_does_not_invent_a_default(tmp_path, monkeypatch):
    result, _ = run_agent(tmp_path, monkeypatch, AgentConfig(type="codex"))
    assert "cost_usd" not in result.usage


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
    agent = CodexAgent(AgentConfig(type="codex", model="gpt-6.1-sol"))
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
    history.append(IterationResult(iteration=1, accepted=True, metrics={}, usage=result.usage))
    usage = history.load()[0].usage
    assert usage["cost_usd"] == pytest.approx(1.77)
    assert usage["cost_source"] == "calculated"
    assert usage["cost_model"] == "gpt-6.1-sol"
    assert usage["cost_rates"]["cache_write"] == 2.5
