"""Tests for autohelix.config module."""

import pytest

from autohelix.config import Config, ConfigIssue, ObservableCommand, KNOWN_TOP_LEVEL_KEYS, parse_duration


class TestConfigFromDict:
    def test_minimal(self):
        config = Config.from_dict({"goal": "optimize"})
        assert config.goal == "optimize"
        assert config.constraints == []
        assert config.observables == []

    def test_single_constraint_string(self):
        config = Config.from_dict({"goal": "x", "constraint": "pytest"})
        assert [c.command for c in config.constraints] == ["pytest"]

    def test_constraints_list(self):
        config = Config.from_dict({"goal": "x", "constraints": ["a", "b"]})
        assert [c.command for c in config.constraints] == ["a", "b"]

    def test_constraints_string(self):
        config = Config.from_dict({"goal": "x", "constraints": "single"})
        assert [c.command for c in config.constraints] == ["single"]

    def test_constraint_timeout(self):
        config = Config.from_dict({"goal": "x", "constraints": [
            "pytest",
            {"command": "integration-test", "timeout": 1200},
        ]})
        assert config.constraints[0].timeout == 1800  # default
        assert config.constraints[1].timeout == 1200

    def test_observable_list_entry(self):
        config = Config.from_dict({
            "goal": "x",
            "observables": [{"command": "python bench.py", "values": {"speed": "higher"}}],
        })
        assert len(config.observables) == 1
        assert config.observables[0].command == "python bench.py"
        assert config.observables[0].values == {"speed": "higher"}

    def test_metrics_key_is_canonical(self):
        # `metrics:` is the documented key; parses to the same observables list.
        config = Config.from_dict({
            "goal": "x",
            "metrics": [{"command": "python bench.py", "values": {"speed": "higher"}}],
        })
        assert len(config.observables) == 1
        assert config.observables[0].command == "python bench.py"
        assert config.metric_directions() == {"speed": "higher"}

    def test_observables_is_accepted_alias(self):
        # `observables:` remains an accepted alias for `metrics:`.
        config = Config.from_dict({
            "goal": "x",
            "observables": [{"command": "python bench.py", "values": {"speed": "higher"}}],
        })
        assert len(config.observables) == 1
        assert config.observables[0].command == "python bench.py"

    def test_metrics_takes_precedence_over_observables(self):
        # If both keys are present, the canonical `metrics:` wins.
        config = Config.from_dict({
            "goal": "x",
            "metrics": [{"command": "m", "values": {"a": "higher"}}],
            "observables": [{"command": "o", "values": {"b": "lower"}}],
        })
        assert [o.command for o in config.observables] == ["m"]
        assert config.metric_directions() == {"a": "higher"}

    def test_metrics_key_not_flagged_as_unknown(self):
        # `metrics:` must not trigger the unknown-top-level-key typo warning.
        raw = {"goal": "x", "metrics": [{"command": "b", "values": {"s": "higher"}}]}
        config = Config.from_dict(raw)
        issues = config.validate(raw)
        assert not any("metrics" in i.message and "typo" in i.message for i in issues)

    def test_observable_lower_direction(self):
        config = Config.from_dict({
            "goal": "x",
            "observables": [{"command": "python bench.py", "values": {"latency": "lower"}}],
        })
        assert config.observables[0].values["latency"] == "lower"

    def test_observable_multiple_values(self):
        config = Config.from_dict({
            "goal": "x",
            "observables": [{"command": "python bench.py", "values": {"throughput": "higher", "latency": "lower"}}],
        })
        assert config.observables[0].values == {"throughput": "higher", "latency": "lower"}

    def test_scope_editable(self):
        config = Config.from_dict({
            "goal": "x",
            "scope": {"editable": ["src/"]},
        })
        assert config.editable == ["src/"]
        assert config.frozen == []

    def test_scope_frozen(self):
        config = Config.from_dict({
            "goal": "x",
            "scope": {"frozen": ["tests/"]},
        })
        assert config.frozen == ["tests/"]
        assert config.editable == []

    def test_budget(self):
        config = Config.from_dict({"goal": "x", "budget": {"iterations": 50}})
        assert config.max_iterations == 50

    def test_default_budget(self):
        config = Config.from_dict({"goal": "x"})
        assert config.max_iterations == 5

    @pytest.mark.parametrize("key", ["scope", "budget", "acceptance", "constraints"])
    def test_present_but_empty_key_does_not_crash(self, key):
        # A user who writes `scope:` (etc.) with a blank/commented body yields a
        # None value in YAML; it must parse cleanly, not raise a raw traceback.
        config = Config.from_dict({"goal": "x", key: None})
        assert config.goal == "x"
        # Empty keys behave like the key being absent.
        assert config.editable == []
        assert config.frozen == []
        assert config.constraints == []
        assert config.max_iterations == 5
        assert config.acceptance.metric_gates == []

    def test_metric_gates(self):
        config = Config.from_dict({
            "goal": "x",
            "acceptance": {"metric_gates": [{"metric": "speed", "max_regression_pct": 5}]},
        })
        assert len(config.acceptance.metric_gates) == 1
        assert config.acceptance.metric_gates[0].metric == "speed"
        assert config.acceptance.metric_gates[0].max_regression_pct == 5.0

    def test_reviewer_true(self):
        config = Config.from_dict({"goal": "x", "reviewer": True})
        assert config.reviewer is not None

    def test_reviewer_dict(self):
        config = Config.from_dict({"goal": "x", "reviewer": {"prompt": "custom"}})
        assert config.reviewer is not None
        assert config.reviewer.prompt == "custom"

    def test_reviewer_none_by_default(self):
        config = Config.from_dict({"goal": "x"})
        assert config.reviewer is None

    def test_reviewer_auto_memory_defaults_false(self):
        config = Config.from_dict({"goal": "x", "reviewer": True})
        assert config.reviewer.auto_memory is False

    def test_reviewer_auto_memory_explicit_true(self):
        config = Config.from_dict({"goal": "x", "reviewer": {"auto_memory": True}})
        assert config.reviewer.auto_memory is True

    def test_agent_auto_memory(self):
        config = Config.from_dict({"goal": "x", "agent": {"auto_memory": True}})
        assert config.agent.auto_memory is True

    def test_agent_auto_memory_default_false(self):
        config = Config.from_dict({"goal": "x"})
        assert config.agent.auto_memory is False


class TestConfigValidate:
    def test_valid_config_no_issues(self):
        config = Config.from_dict({
            "goal": "optimize sorting",
            "constraints": ["pytest"],
            "observables": [{"command": "python bench.py", "values": {"speed": "higher"}}],
        })
        assert config.validate() == []

    def test_empty_goal(self):
        config = Config.from_dict({"goal": ""})
        issues = config.validate()
        assert any("'goal' is empty" in i.message for i in issues)
        assert all(i.level in ("error", "warning") for i in issues)

    def test_empty_goal_is_error(self):
        config = Config.from_dict({"goal": ""})
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("'goal' is empty" in i.message for i in errors)

    def test_whitespace_only_goal(self):
        config = Config.from_dict({"goal": "   "})
        issues = config.validate()
        assert any("'goal' is empty" in i.message for i in issues)

    def test_placeholder_goal_is_error(self):
        config = Config.from_dict({"goal": "Describe what you want the agent to achieve."})
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("placeholder" in i.message for i in errors)

    def test_no_constraints_or_observables_is_warning(self):
        config = Config.from_dict({"goal": "optimize"})
        issues = config.validate()
        warnings = [i for i in issues if i.level == "warning"]
        assert any("No constraints or observables" in i.message for i in warnings)

    def test_unknown_keys_are_warnings(self):
        raw = {"goal": "x", "gaol": "typo", "constraints": ["pytest"]}
        config = Config.from_dict(raw)
        issues = config.validate(raw_data=raw)
        warnings = [i for i in issues if i.level == "warning"]
        assert any("Unknown config key 'gaol'" in i.message for i in warnings)

    def test_unknown_agent_type_is_error(self):
        config = Config.from_dict({
            "goal": "x",
            "constraints": ["pytest"],
            "agent": {"type": "gpt4"},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("Unknown agent type" in i.message for i in errors)

    def test_constraints_only_no_warning(self):
        config = Config.from_dict({"goal": "x", "constraints": ["pytest"]})
        issues = config.validate()
        assert not any("No constraints or observables" in i.message for i in issues)

    def test_observables_only_no_warning(self):
        config = Config.from_dict({
            "goal": "x",
            "observables": [{"command": "bench.py", "values": {"s": "higher"}}],
        })
        issues = config.validate()
        assert not any("No constraints or observables" in i.message for i in issues)

    def test_empty_observable_command(self):
        config = Config.from_dict({
            "goal": "x",
            "observables": [{"command": "", "values": {"speed": "higher"}}],
        })
        issues = config.validate()
        assert any("empty" in i.message.lower() for i in issues)

    def test_invalid_metric_direction(self):
        config = Config.from_dict({
            "goal": "x",
            "observables": [{"command": "bench.py", "values": {"speed": "fastest"}}],
        })
        issues = config.validate()
        assert any("invalid direction" in i.message for i in issues)

    def test_unknown_agent_type(self):
        config = Config.from_dict({
            "goal": "x",
            "constraints": ["pytest"],
            "agent": {"type": "gpt4"},
        })
        issues = config.validate()
        assert any("Unknown agent type" in i.message for i in issues)

    def test_known_agent_types_pass(self):
        for agent_type in ["claude", "codex", "opencode"]:
            config = Config.from_dict({
                "goal": "x",
                "constraints": ["pytest"],
                "agent": {"type": agent_type},
            })
            issues = config.validate()
            assert not any("Unknown agent type" in i.message for i in issues)

    def test_settings_ignored_warning_for_claude(self):
        config = Config.from_dict({
            "goal": "x",
            "constraints": ["pytest"],
            "agent": {"type": "claude", "settings": {"foo": "bar"}},
        })
        warnings = [i for i in config.validate() if i.level == "warning"]
        assert any("agent.settings is ignored" in w.message for w in warnings)

    def test_settings_no_warning_for_codex(self):
        config = Config.from_dict({
            "goal": "x",
            "constraints": ["pytest"],
            "agent": {"type": "codex", "settings": {"foo": "bar"}},
        })
        assert not any("agent.settings is ignored" in i.message for i in config.validate())

    def test_no_settings_no_warning(self):
        config = Config.from_dict({
            "goal": "x",
            "constraints": ["pytest"],
            "agent": {"type": "claude"},
        })
        assert not any("agent.settings is ignored" in i.message for i in config.validate())

    def test_observable_no_values_is_valid(self):
        """Observable with no values is valid (stdout-only capture)."""
        config = Config.from_dict({
            "goal": "x",
            "observables": [{"command": "bench.py", "values": {}}],
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert not errors

    def test_negative_timeout(self):
        config = Config.from_dict({
            "goal": "x",
            "constraints": ["pytest"],
            "agent": {"timeout_seconds": -10},
        })
        issues = config.validate()
        assert any("timeout_seconds" in i.message for i in issues)

    def test_string_timeout(self):
        config = Config.from_dict({
            "goal": "x",
            "constraints": ["pytest"],
            "agent": {"timeout_seconds": "five minutes"},
        })
        issues = config.validate()
        assert any("timeout_seconds" in i.message for i in issues)

    def test_unknown_top_level_keys(self):
        raw = {"goal": "x", "gaol": "typo", "constraints": ["pytest"]}
        config = Config.from_dict(raw)
        issues = config.validate(raw_data=raw)
        assert any("Unknown config key 'gaol'" in i.message for i in issues)

    def test_no_unknown_keys_for_valid_config(self):
        raw = {"goal": "x", "constraints": ["pytest"], "observables": [{"command": "bench", "values": {"s": "higher"}}]}
        config = Config.from_dict(raw)
        issues = config.validate(raw_data=raw)
        assert not any("Unknown config key" in i.message for i in issues)

    def test_metric_gate_references_unknown_metric(self):
        config = Config.from_dict({
            "goal": "x",
            "observables": [{"command": "bench.py", "values": {"speed": "higher"}}],
            "acceptance": {"metric_gates": [{"metric": "nonexistent"}]},
        })
        issues = config.validate()
        assert any("unknown metric 'nonexistent'" in i.message for i in issues)

    def test_metric_gate_references_valid_metric(self):
        config = Config.from_dict({
            "goal": "x",
            "observables": [{"command": "bench.py", "values": {"speed": "higher"}}],
            "acceptance": {"metric_gates": [{"metric": "speed"}]},
        })
        issues = config.validate()
        assert not any("unknown metric" in i.message for i in issues)

    def test_editable_and_frozen_mutually_exclusive(self):
        config = Config.from_dict({
            "goal": "x",
            "constraints": ["pytest"],
            "scope": {"editable": ["src/"], "frozen": ["tests/"]},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("Cannot set both" in i.message for i in errors)

    def test_editable_only_no_scope_error(self):
        config = Config.from_dict({
            "goal": "x",
            "constraints": ["pytest"],
            "scope": {"editable": ["src/"]},
        })
        issues = config.validate()
        assert not any("Cannot set both" in i.message for i in issues)

    def test_frozen_only_no_scope_error(self):
        config = Config.from_dict({
            "goal": "x",
            "constraints": ["pytest"],
            "scope": {"frozen": ["tests/"]},
        })
        issues = config.validate()
        assert not any("Cannot set both" in i.message for i in issues)


class TestParseDuration:
    def test_hours(self):
        assert parse_duration("1h") == 3600

    def test_minutes(self):
        assert parse_duration("30m") == 1800

    def test_seconds(self):
        assert parse_duration("90s") == 90

    def test_mixed_hm(self):
        assert parse_duration("2h30m") == 9000

    def test_mixed_hms(self):
        assert parse_duration("1h30m45s") == 5445

    def test_bare_integer(self):
        assert parse_duration(30) == 30

    def test_bare_float(self):
        assert parse_duration(90.5) == 90

    def test_invalid_string(self):
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration("abc")

    def test_negative(self):
        with pytest.raises(ValueError):
            parse_duration("-1h")

    def test_zero(self):
        with pytest.raises(ValueError):
            parse_duration("0m")

    def test_empty_string(self):
        with pytest.raises(ValueError, match="Empty duration"):
            parse_duration("")

    def test_unknown_unit(self):
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration("1d")

    def test_trailing_bare_number(self):
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration("1h30")

    def test_zero_integer(self):
        with pytest.raises(ValueError):
            parse_duration(0)

    def test_negative_integer(self):
        with pytest.raises(ValueError):
            parse_duration(-60)

    def test_duplicate_unit_rejected(self):
        with pytest.raises(ValueError, match="Duplicate unit"):
            parse_duration("1h2h")

    def test_duplicate_minutes_rejected(self):
        with pytest.raises(ValueError, match="Duplicate unit"):
            parse_duration("30m30m")

    def test_uppercase_units(self):
        assert parse_duration("1H") == 3600
        assert parse_duration("30M") == 1800
        assert parse_duration("2H30M") == 9000

    def test_whitespace_between_tokens(self):
        assert parse_duration(" 1h ") == 3600

    def test_whitespace_between_multiple_tokens(self):
        assert parse_duration("1h 30m") == 5400

    def test_three_component_trailing_bare_number(self):
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration("1h30m10")

    def test_whitespace_only_string(self):
        with pytest.raises(ValueError, match="Empty duration"):
            parse_duration("   ")

    def test_decimal_hours_rejected(self):
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration("1.5h")

    def test_large_value(self):
        assert parse_duration("99999h") == 99999 * 3600

    def test_all_zero_components(self):
        with pytest.raises(ValueError, match="positive"):
            parse_duration("0h0m0s")

    def test_zero_hours_nonzero_minutes(self):
        assert parse_duration("0h1m") == 60

    def test_unit_before_number(self):
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration("m30")

    def test_negative_component_in_mixed(self):
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration("1h-30m")

    def test_bool_true_rejected(self):
        with pytest.raises(ValueError, match="bool"):
            parse_duration(True)

    def test_bool_false_rejected(self):
        with pytest.raises(ValueError, match="bool"):
            parse_duration(False)

    def test_infinity_rejected(self):
        with pytest.raises(ValueError, match="finite"):
            parse_duration(float("inf"))

    def test_negative_infinity_rejected(self):
        with pytest.raises(ValueError, match="finite"):
            parse_duration(float("-inf"))

    def test_nan_rejected(self):
        with pytest.raises(ValueError, match="finite"):
            parse_duration(float("nan"))

    def test_order_independence(self):
        assert parse_duration("30m1h") == 5400

    def test_leading_zeros(self):
        assert parse_duration("01h") == 3600


class TestBudgetConfig:
    def test_cost_parsed(self):
        config = Config.from_dict({"goal": "x", "budget": {"cost": 5.0}})
        assert config.max_cost_usd == 5.0

    def test_time_parsed(self):
        config = Config.from_dict({"goal": "x", "budget": {"time": "1h"}})
        assert config.max_time_seconds == 3600

    def test_all_three(self):
        config = Config.from_dict({
            "goal": "x",
            "budget": {"iterations": 10, "cost": 2.5, "time": "30m"},
        })
        assert config.max_iterations == 10
        assert config.max_cost_usd == 2.5
        assert config.max_time_seconds == 1800

    def test_cost_default_none(self):
        config = Config.from_dict({"goal": "x"})
        assert config.max_cost_usd is None

    def test_time_default_none(self):
        config = Config.from_dict({"goal": "x"})
        assert config.max_time_seconds is None

    def test_negative_cost_error(self):
        config = Config.from_dict({
            "goal": "optimize",
            "constraints": ["pytest"],
            "budget": {"cost": -1},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("budget.cost" in i.message for i in errors)

    def test_zero_cost_error(self):
        config = Config.from_dict({
            "goal": "optimize",
            "constraints": ["pytest"],
            "budget": {"cost": 0},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("budget.cost" in i.message for i in errors)

    def test_invalid_time_string_error(self):
        config = Config.from_dict({
            "goal": "optimize",
            "constraints": ["pytest"],
            "budget": {"time": "abc"},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("budget.time" in i.message for i in errors)

    def test_time_integer_treated_as_seconds(self):
        config = Config.from_dict({"goal": "x", "budget": {"time": 120}})
        assert config.max_time_seconds == 120

    def test_invalid_cost_string_error(self):
        config = Config.from_dict({
            "goal": "optimize",
            "constraints": ["pytest"],
            "budget": {"cost": "not_a_number"},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("budget.cost" in i.message for i in errors)

    def test_cost_string_numeric(self):
        config = Config.from_dict({"goal": "x", "budget": {"cost": "5.00"}})
        assert config.max_cost_usd == 5.0

    def test_cost_bool_true_error(self):
        config = Config.from_dict({
            "goal": "optimize",
            "constraints": ["pytest"],
            "budget": {"cost": True},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("budget.cost" in i.message for i in errors)

    def test_cost_bool_false_error(self):
        config = Config.from_dict({
            "goal": "optimize",
            "constraints": ["pytest"],
            "budget": {"cost": False},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("budget.cost" in i.message for i in errors)

    def test_cost_none_stays_none(self):
        config = Config.from_dict({"goal": "x", "budget": {"cost": None}})
        assert config.max_cost_usd is None

    def test_negative_zero_cost_error(self):
        config = Config.from_dict({
            "goal": "optimize",
            "constraints": ["pytest"],
            "budget": {"cost": -0.0},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("budget.cost" in i.message or "cost" in i.message for i in errors)

    def test_time_zero_seconds_error(self):
        config = Config.from_dict({
            "goal": "optimize",
            "constraints": ["pytest"],
            "budget": {"time": "0s"},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("budget.time" in i.message or "time" in i.message for i in errors)

    def test_empty_budget_block(self):
        config = Config.from_dict({"goal": "x", "budget": {}})
        assert config.max_iterations == 5
        assert config.max_cost_usd is None
        assert config.max_time_seconds is None

    def test_invalid_cost_string_has_specific_error(self):
        config = Config.from_dict({
            "goal": "optimize",
            "constraints": ["pytest"],
            "budget": {"cost": "not_a_number"},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("not_a_number" in i.message for i in errors)

    def test_time_bool_true_error(self):
        config = Config.from_dict({
            "goal": "optimize",
            "constraints": ["pytest"],
            "budget": {"time": True},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("budget.time" in i.message or "time" in i.message for i in errors)

    def test_cost_bool_has_specific_error(self):
        config = Config.from_dict({
            "goal": "optimize",
            "constraints": ["pytest"],
            "budget": {"cost": True},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("bool" in i.message for i in errors)
