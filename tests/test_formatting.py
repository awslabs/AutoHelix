"""Tests for autohelix.formatting."""

from autohelix.formatting import format_cost, format_delta, format_total_cost


def test_known_zero_is_distinct_from_unknown_cost():
    assert format_cost(0) == "$0.00"
    assert format_cost(None) == "cost unavailable"


def test_complete_partial_and_unavailable_totals():
    assert format_total_cost([{"cost_usd": 0.25}, {"cost_usd": 0}]) == "$0.25"
    assert format_total_cost([{"cost_usd": 0.25}, {}]) == "$0.25 (incomplete)"
    assert format_total_cost([{}, {"cost_usd": None}]) == "cost unavailable"


class TestFormatDelta:
    def test_no_change_returns_empty(self):
        assert format_delta(100, 100) == ""

    def test_zero_baseline_returns_empty(self):
        assert format_delta(100, 0) == ""

    def test_increase_percentage(self):
        assert format_delta(150, 100) == "↑ 50.0%"

    def test_decrease_percentage(self):
        assert format_delta(80, 100) == "↓ 20.0%"

    def test_large_increase_shown_as_multiplier(self):
        # 100 -> 1200 is +1100% (≥ 900%), shown as a multiplier.
        assert format_delta(1200, 100) == "↑ 12x"

    def test_large_decrease_shown_as_inverse_multiplier(self):
        # 100 -> 5 shrank 95% (value to 1/20th), shown as ↓ 20x.
        assert format_delta(5, 100) == "↓ 20x"

    def test_moderate_decrease_stays_percentage(self):
        # 100 -> 20 is -80%, below the inverse-ratio threshold.
        assert format_delta(20, 100) == "↓ 80.0%"


def test_format_duration_is_compact():
    from autohelix.formatting import format_duration
    assert [format_duration(s) for s in (0, 48, 62, 3599, 3700)] == ["0s", "48s", "1m 02s", "59m 59s", "1h 01m"]
