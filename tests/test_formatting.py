"""Tests for autohelix.formatting."""

from autohelix.formatting import format_cost, format_total_cost


def test_known_zero_is_distinct_from_unknown_cost():
    assert format_cost(0) == "$0.00"
    assert format_cost(None) == "cost unavailable"


def test_complete_partial_and_unavailable_totals():
    assert format_total_cost([{"cost_usd": 0.25}, {"cost_usd": 0}]) == "$0.25"
    assert format_total_cost([{"cost_usd": 0.25}, {}]) == "$0.25 (incomplete)"
    assert format_total_cost([{}, {"cost_usd": None}]) == "cost unavailable"


def test_format_duration_is_compact():
    from autohelix.formatting import format_duration
    assert [format_duration(s) for s in (0, 48, 62, 3599, 3700)] == ["0s", "48s", "1m 02s", "59m 59s", "1h 01m"]


def test_an_unavailable_total_says_why_when_its_calls_agree():
    reason = "set agent.model (Codex's output names no model)"
    assert format_total_cost([{"cost_unavailable_reason": reason}, {"cost_usd": None, "cost_unavailable_reason": reason}]) \
        == f"cost unavailable: {reason}"
    assert format_total_cost([{"cost_unavailable_reason": "a"}, {"cost_unavailable_reason": "b"}]) == "cost unavailable"
    # A known part: incomplete, as before (no reason).
    assert format_total_cost([{"cost_usd": 0.25}, {"cost_unavailable_reason": reason}]) == "$0.25 (incomplete)"
