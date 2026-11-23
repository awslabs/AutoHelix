"""Tests for autohelix.formatting."""

import pytest

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
    assert format_total_cost([{"cost_unavailable_reason": "set agent.model"},
                              {"cost_usd": None, "cost_unavailable_reason": "set agent.model"}]) \
        == "cost unavailable: set agent.model"
    assert format_total_cost([{"cost_unavailable_reason": "No API pricing for model x."}]) \
        == "cost unavailable: No API pricing for model x"
    # Different reasons, or a call without one: no reason.
    assert format_total_cost([{"cost_unavailable_reason": "a"}, {"cost_unavailable_reason": "b"}]) == "cost unavailable"
    assert format_total_cost([{"cost_unavailable_reason": "a"}, {}]) == "cost unavailable"
    # A known part: incomplete, as before (no reason).
    assert format_total_cost([{"cost_usd": 0.25}, {"cost_unavailable_reason": "a"}]) == "$0.25 (incomplete)"


@pytest.mark.parametrize("reason", [["a"], {"a": 1}, 3])
def test_a_reason_that_isnt_a_string_is_ignored(reason):
    # Usage records can come from a file candidate code writes ($AUTOHELIX_USAGE_FILE).
    assert format_total_cost([{"cost_unavailable_reason": reason}]) == "cost unavailable"


def test_a_reason_is_cleaned_of_terminal_controls():
    text = format_total_cost([{"cost_unavailable_reason": "\x1b[2Jset agent.model\x1b]0;x\x07"}])
    assert "\x1b" not in text and "\x07" not in text and "set agent.model" in text
