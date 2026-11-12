"""Tests for autohelix.config module."""

import shlex

import pytest

from autohelix.agents import AgentConfig
from autohelix.checks import metric_gate_failure, metric_reference
from autohelix.config import (Config, CommandSpec, MetricGate, ObserveEntry, KNOWN_TOP_LEVEL_KEYS, goal_title,
                              parse_duration, parse_threshold)
from autohelix.history import IterationResult


def errors(data):
    return [i.message for i in Config.from_dict(data).validate(data) if i.level == "error"]


def warnings(data):
    return [i.message for i in Config.from_dict(data).validate(data) if i.level == "warning"]


BENCH = {"name": "bench", "command": "python bench.py", "metrics": {"speed": "higher"}}


class TestConfigFromDict:
    def test_minimal(self):
        config = Config.from_dict({"goal": "optimize"})
        assert config.goal == "optimize"
        assert config.gates == []
        assert config.observe == []
        assert config.stop == []

    def test_gate_commands(self):
        config = Config.from_dict({"goal": "x", "gates": [
            "pytest",
            {"command": "integration-test", "timeout": 1200},
            {"command": "slow", "timeout": "30m"},
        ]})
        assert config.gates == [CommandSpec("pytest"), CommandSpec("integration-test", 1200),
                                CommandSpec("slow", 1800)]
        assert config.gates[0].timeout == 1800  # default
        assert config.gate_commands() == config.gates
        assert config.metric_gates() == []

    def test_single_gate_string(self):
        config = Config.from_dict({"goal": "x", "gates": "pytest"})
        assert [c.command for c in config.gate_commands()] == ["pytest"]

    def test_gates_keep_list_order(self):
        config = Config.from_dict({"goal": "x", "observe": [BENCH], "gates": [
            "a", {"metric": "speed", "regress": "5%"}, {"ask": "Good?"}, {"metric": "speed", "improve": "0%"}]})
        assert [type(gate).__name__ for gate in config.gates] == ["CommandSpec", "MetricGate", "CommandSpec",
                                                                    "MetricGate"]
        assert config.metric_gates() == [MetricGate("speed", "regress", 5.0), MetricGate("speed", "improve", 0.0)]
        assert [c.text for c in config.gate_commands()] == ["a", "ask: Good?"]

    def test_ask_gate_renders_a_quoted_command(self):
        config = Config.from_dict({"goal": "x", "gates": [{
            "ask": "Does it keep the 'public' API?", "yes": "it's kept", "no": "a $name changed",
            "timeout": "20m",
            "agent": {"backend": "codex", "model": "gpt 6", "reasoning_effort": "high", "command": "/opt/codex x"},
        }]})
        gate = config.gates[0]
        assert gate.label == "ask: Does it keep the 'public' API?"
        assert gate.timeout == 1200
        assert shlex.split(gate.command) == [
            "autohelix", "ask", "--yes", "it's kept", "--no", "a $name changed",
            "--agent", "codex", "--model", "gpt 6", "--effort", "high", "--timeout", "1200",
            "--command", "/opt/codex x", "--", "Does it keep the 'public' API?",
        ]

    def test_ask_gate_minimal_and_agent_timeout(self):
        plain = Config.from_dict({"goal": "x", "gates": [{"ask": "Done?"}]}).gates[0]
        assert plain.command == "autohelix ask -- 'Done?'" and plain.timeout == 1800
        timed = Config.from_dict({"goal": "x", "gates": [
            {"ask": "Done?", "timeout": "1h", "agent": {"timeout": "5m"}}]}).gates[0]
        assert shlex.split(timed.command)[2:] == ["--timeout", "300", "--", "Done?"]  # the agent's own limit
        assert timed.timeout == 3600
        assert shlex.split(Config.from_dict({"goal": "x", "gates": [{"ask": "Done?", "timeout": 90}]})
                           .gates[0].command)[2:] == ["--timeout", "90", "--", "Done?"]

    def test_entry_agent_timeout_alone_lengthens_the_command_timeout(self):
        config = Config.from_dict({"goal": "x", "gates": [{"ask": "Done?", "agent": {"timeout": "40m"}}],
                                   "observe": [{"name": "r", "review": "Go", "agent": {"timeout": 300}}]})
        assert config.gates[0].timeout == 40 * 60 + 60
        assert config.observe[0].timeout == 360

    @pytest.mark.parametrize("question", ["--help", "-n ok?", "--yes", "- dash"])
    def test_questions_and_prompts_starting_with_a_dash_stay_text(self, question):
        from click.testing import CliRunner

        import autohelix.judge as judge
        from autohelix.cli import main
        from autohelix.judge import Answer

        config = Config.from_dict({"goal": "x", "gates": [{"ask": question, "yes": "fine"}],
                                   "observe": [{"name": "r", "review": question}]})
        words = shlex.split(config.gates[0].command)
        assert words[-2:] == ["--", question]
        assert shlex.split(config.observe[0].command)[-2:] == ["--", question]
        seen = []

        def fake_ask(asked, cwd, agent, **kwargs):
            seen.append((asked, kwargs["yes"]))
            return Answer(yes=False, reason="checked", cost_usd=0.0)

        original = judge.ask
        judge.ask = fake_ask
        try:
            result = CliRunner().invoke(main, words[1:])
        finally:
            judge.ask = original
        assert result.exit_code == 1, result.output  # asked, and the answer was no
        assert seen == [(question, "fine")]

    def test_metric_gate_thresholds(self):
        for value, expected in (("5%", 5.0), ("0.5%", 0.5), ("0%", 0.0), (" 2.5 % ", 2.5)):
            assert parse_threshold(value) == expected
        for value in ("-1%", True, "five%", "nan%", "inf%", "5", None, [5]):
            with pytest.raises(ValueError):
                parse_threshold(value)
        for value in (5, 0.05, 0):
            with pytest.raises(ValueError, match="write the percentage with %"):
                parse_threshold(value)

    def test_observe_command_entry(self):
        config = Config.from_dict({"goal": "x", "observe": [
            {"name": "bench", "command": "python bench.py", "metrics": {"speed": "higher", "latency": "lower"},
             "capture": ["results/log.json"], "timeout": "15m"},
            {"name": "notes_2", "command": "cat NOTES"},
        ]})
        assert config.observe == [
            ObserveEntry("bench", "python bench.py", {"speed": "higher", "latency": "lower"},
                         ["results/log.json"], 900),
            ObserveEntry("notes_2", "cat NOTES"),
        ]
        assert config.metric_directions() == {"speed": "higher", "latency": "lower"}
        assert config.metric_names() == {"speed", "latency"}
        assert config.metric_source("latency").name == "bench"
        with pytest.raises(KeyError):
            config.metric_source("nope")

    def test_observe_review_entry_renders_a_quoted_command(self):
        config = Config.from_dict({"goal": "x", "observe": [{
            "name": "progress", "review": "List items; report `missing`. Don't trust 'STATUS'.",
            "previous": True, "metrics": {"missing": "lower", "done": "higher"},
            "agent": {"backend": "Claude", "model": "opus"}, "timeout": 600,
        }]})
        entry = config.observe[0]
        assert entry.review == "List items; report `missing`. Don't trust 'STATUS'."
        assert entry.capture == [] and entry.timeout == 600
        assert shlex.split(entry.command) == [
            "autohelix", "review", "--previous",
            "--metric", "missing:lower", "--metric", "done:higher", "--agent", "claude", "--model", "opus",
            "--timeout", "600", "--", "List items; report `missing`. Don't trust 'STATUS'.",
        ]
        assert Config.from_dict({"goal": "x", "observe": [{"name": "r", "review": "Go"}]}).observe[0].command \
            == "autohelix review -- Go"

    def test_stop_commands(self):
        config = Config.from_dict({"goal": "x", "stop": ["a", {"command": "b", "timeout": "1m"}]})
        assert config.stop == [CommandSpec("a"), CommandSpec("b", 60)]

    def test_scope_editable_and_frozen_together(self):
        config = Config.from_dict({"goal": "x", "scope": {"editable": ["src/"], "frozen": ["src/fixtures/"]}})
        assert (config.editable, config.frozen) == (["src/"], ["src/fixtures/"])
        assert not errors({"goal": "x", "gates": ["t"], "scope": {"editable": ["src/"], "frozen": ["tests/"]}})

    def test_scope_one_side(self):
        assert Config.from_dict({"goal": "x", "scope": {"editable": ["src/"]}}).frozen == []
        assert Config.from_dict({"goal": "x", "scope": {"frozen": "tests/"}}).frozen == ["tests/"]

    def test_budget(self):
        config = Config.from_dict({"goal": "x", "budget": {"iterations": 50}})
        assert config.max_iterations == 50

    def test_default_budget(self):
        config = Config.from_dict({"goal": "x"})
        assert config.max_iterations == 5

    @pytest.mark.parametrize("key", ["scope", "budget", "gates", "observe", "stop", "agent"])
    def test_present_but_empty_key_does_not_crash(self, key):
        # A blank/commented body is None in YAML; it behaves like the key being absent.
        config = Config.from_dict({"goal": "x", key: None})
        assert config.goal == "x"
        assert config.editable == [] and config.frozen == []
        assert config.gates == [] and config.observe == []
        assert config.max_iterations == 5
        assert not errors({"goal": "x", key: None})

    def test_agent_keys(self):
        config = Config.from_dict({"goal": "x", "agent": {
            "backend": "codex", "model": "m", "reasoning_effort": "high", "timeout": "30m", "command": "c",
            "settings": {"a": 1}, "extra_args": "--x", "auto_memory": True, "require_notes": True}})
        agent = config.agent
        assert (agent.backend, agent.model, agent.reasoning_effort, agent.timeout, agent.command) == \
            ("codex", "m", "high", 1800, "c")
        assert agent.settings == {"a": 1} and agent.extra_args == ["--x"] and agent.auto_memory
        assert agent.require_notes is not None
        assert Config.from_dict({"goal": "x", "agent": {"timeout": 90}}).agent.timeout == 90

    def test_agent_defaults(self):
        agent = Config.from_dict({"goal": "x"}).agent
        assert (agent.backend, agent.timeout, agent.auto_memory) == ("claude", None, False)

    def test_agent_from_dict_raises_on_malformed(self):
        with pytest.raises(ValueError, match="agent.backend"):
            AgentConfig.from_dict({"type": "claude"})
        assert AgentConfig.from_dict({"backend": "mock"}).backend == "mock"


class TestTitle:
    def test_short_first_line_is_the_title(self):
        assert goal_title("Make sort_list faster.\nDetails here.") == "Make sort_list faster."
        assert Config.from_dict({"goal": "  Fix it  \nmore"}).title == "Fix it"

    def test_long_line_cut_at_first_sentence_end(self):
        goal = "Implement every item in PRD.md. " + "Then keep going with more and more words " * 3
        assert goal_title(goal) == "Implement every item in PRD.md."

    def test_long_line_without_early_sentence_end_cut_with_ellipsis(self):
        goal = "word " * 40
        title = goal_title(goal)
        assert title.endswith("…") and len(title) <= 80
        assert not title[:-1].endswith(" ")

    def test_long_first_line_warns(self):
        assert any("first line is its title" in w for w in warnings({"goal": "x" * 81, "gates": ["t"]}))
        assert not any("title" in w for w in warnings({"goal": "Short.\n" + "x" * 200, "gates": ["t"]}))


class TestOldKeys:
    @pytest.mark.parametrize("data, message", [
        ({"constraints": ["pytest"]}, "`constraints:` is now `gates:`"),
        ({"constraint": "pytest"}, "`constraint:` is now `gates:`"),
        ({"metrics": []}, "`metrics:` is now `observe:`"),
        ({"observables": []}, "`observables:` is now `observe:`"),
        ({"acceptance": {"metric_gates": []}}, "`acceptance:` is now a `gates:` entry"),
        ({"reviewer": True}, "`reviewer:` is now an `observe:` entry with `name:` and `review: <prompt>`"),
        ({"agent": {"type": "claude"}}, "`agent.type` is now `agent.backend`"),
        ({"agent": {"timeout_seconds": 60}}, "`agent.timeout_seconds` is now `agent.timeout`"),
        ({"agent": {"settings": {"model": "m"}}}, "`agent.settings.model` is now `agent.model`"),
        ({"budget": {"iteration_time": "1h"}}, "`budget.iteration_time` is now `agent.timeout`"),
        ({"observe": [{"name": "b", "command": "x", "values": {"s": "higher"}}]}, "`values:` is now `metrics:`"),
        ({"observe": [{"name": "b", "command": "x", "capture_stdout": False}]},
         "`capture_stdout:` is gone: stdout is always saved"),
    ])
    def test_old_key_is_an_error_naming_the_replacement(self, data, message):
        data = {"goal": "g", **data}
        found = errors(data)
        assert any(message in e for e in found), found

    def test_old_top_level_keys_are_not_also_typo_warnings(self):
        assert not any("possible typo" in w for w in warnings({"goal": "g", "constraints": ["t"]}))


class TestEntryErrors:
    @pytest.mark.parametrize("gate, message", [
        ({"command": "x", "retries": 2}, "unknown key `retries`"),
        ({"ask": "Q?", "rubric": "r"}, "unknown key `rubric`"),
        ({"metric": "speed", "regress": "5%", "extra": 1}, "unknown key `extra`"),
        ({"command": "x", "ask": "Q?"}, "exactly one of `command:`, `ask:` or `metric:`"),
        ({"timeout": 5}, "exactly one of `command:`, `ask:` or `metric:`"),
        ({"metric": "speed"}, "exactly one of `regress: N%` or `improve: N%`"),
        ({"metric": "speed", "regress": "5%", "improve": "1%"}, "exactly one of `regress: N%` or `improve: N%`"),
        ({"metric": "speed", "regress": "-5%"}, "regress: must be a percentage of 0% or more"),
        ({"metric": "nope", "improve": "0%"}, "metric 'nope' is not declared"),
        ({"metric": "speed", "regress": 0.05}, "write the percentage with %, e.g. 0.05%"),
        ({"ask": "Q?", "timeout": "5m", "agent": {"timeout": "5m"}}, "must be shorter than the entry's timeout"),
        ({"ask": "Q?", "agent": {"backend": "gemini"}}, "(must be claude or codex)"),
        ({"command": ""}, "`command:` must be a non-empty string"),
        ({"command": "x", "timeout": "soon"}, "timeout: 'soon' is not a valid duration"),
        ({"command": "x", "timeout": 0}, "is not a valid duration"),
        ({"ask": "Q?", "agent": {"backend": "opencode"}}, "OpenCode has no read-only mode"),
        ({"ask": "Q?", "agent": {"settings": {}}}, "unknown key `settings`"),
        ({"ask": "Q?", "agent": {"extra_args": ["--x"]}}, "unknown key `extra_args`"),
        ({"ask": "Q?", "agent": "claude"}, "`agent:` must be a mapping"),
        ("", "the command is empty"),
        (["pytest"], "expected a command or a mapping"),
    ])
    def test_gate_entry_errors(self, gate, message):
        found = errors({"goal": "g", "observe": [BENCH], "gates": [gate]})
        assert any(message in e for e in found), found

    @pytest.mark.parametrize("entry, message", [
        ({"command": "x"}, "needs a `name:`"),
        ({"name": "Bench", "command": "x"}, "must be lowercase letters"),
        ({"name": "-b", "command": "x"}, "must be lowercase letters"),
        ({"name": "b" * 65, "command": "x"}, "must be lowercase letters"),
        ({"name": "b"}, "exactly one of `command:` or `review:`"),
        ({"name": "b", "command": "x", "review": "y"}, "exactly one of `command:` or `review:`"),
        ({"name": "b", "command": "x", "previous": True}, "unknown key `previous`"),
        ({"name": "b", "review": "y", "capture": ["f"]}, "unknown key `capture`"),
        ({"name": "b", "command": "x", "metrics": {"s": "up"}}, "invalid direction"),
        ({"name": "b", "command": "x", "metrics": {"a-b": "higher"}}, "must be letters, digits or _"),
        ({"name": "b", "command": "x", "metrics": ["s"]}, "`metrics:` must map metric names"),
        ({"name": "b", "review": "y", "previous": "yes"}, "`previous:` must be true or false"),
        ({"name": "b", "review": "y", "agent": {"backend": "opencode"}}, "OpenCode has no read-only mode"),
        ({"name": "b", "review": "y", "agent": {"auto_memory": True}}, "unknown key `auto_memory`"),
        ({"name": "b", "review": "y", "timeout": 60, "agent": {"timeout": 90}}, "must be shorter than the entry's"),
        ("python bench.py", "expected a mapping"),
    ])
    def test_observe_entry_errors(self, entry, message):
        found = errors({"goal": "g", "observe": [entry]})
        assert any(message in e for e in found), found

    def test_names_are_unique(self):
        found = errors({"goal": "g", "observe": [{"name": "b", "command": "x"}, {"name": "b", "command": "y"}]})
        assert any("two entries are named `b`" in e for e in found)

    def test_metric_names_are_unique_across_entries(self):
        found = errors({"goal": "g", "observe": [
            {"name": "a", "command": "x", "metrics": {"s": "higher"}},
            {"name": "b", "review": "y", "metrics": {"s": "lower"}}]})
        assert any("Metric 's' is declared twice" in e for e in found)

    def test_stop_entry_errors(self):
        assert any("unknown key `ask`" in e for e in errors({"goal": "g", "stop": [{"ask": "Done?"}]}))

    def test_top_level_opencode_needs_a_backend_on_ask_and_review_entries(self):
        data = {"goal": "g", "agent": {"backend": "OpenCode"},
                "gates": ["pytest", {"ask": "Q?"}, {"ask": "R?", "agent": {"backend": "claude"}}],
                "observe": [{"name": "bench", "command": "x"}, {"name": "r", "review": "y"}]}
        found = sorted(e for e in errors(data) if "read-only" in e)
        assert found == ["gates entry 2: opencode has no read-only mode; set agent.backend on this entry",
                         "observe entry `r`: opencode has no read-only mode; set agent.backend on this entry"]

    def test_backends_are_lowercased(self):
        config = Config.from_dict({"goal": "g", "agent": {"backend": "Codex"},
                                   "gates": [{"ask": "Q?", "agent": {"backend": "CLAUDE"}}]})
        assert config.agent.backend == "codex"
        assert shlex.split(config.gates[0].command)[2:4] == ["--agent", "claude"]
        assert not errors({"goal": "g", "agent": {"backend": "Codex"}})

    @pytest.mark.parametrize("value, message", [
        ({"min_chars": "x"}, "agent.require_notes.min_chars must be an integer"),
        ({"max_retries": 1.5}, "agent.require_notes.max_retries must be an integer"),
        ({"min_char": 10}, "Unknown key agent.require_notes.min_char"),
        ([1], "agent.require_notes must be true, false or a mapping"),
        ("yes", "agent.require_notes must be true, false or a mapping"),
    ])
    def test_require_notes_errors(self, value, message):
        found = errors({"goal": "g", "agent": {"require_notes": value}})
        assert any(message in e for e in found), found

    def test_require_notes_values(self):
        notes = Config.from_dict({"goal": "g", "agent": {"require_notes": {"min_chars": 10}}}).agent.require_notes
        assert (notes.min_chars, notes.max_retries) == (10, 3)

    @pytest.mark.parametrize("value", [0, -1, True, "abc", 2.5, "3"])
    def test_iterations_must_be_a_positive_integer(self, value):
        found = errors({"goal": "g", "budget": {"iterations": value}})
        assert any("budget.iterations must be a positive integer" in e for e in found), found

    def test_unknown_agent_key_is_an_error(self):
        assert any("Unknown key agent.colour" in e for e in errors({"goal": "g", "agent": {"colour": "blue"}}))


class TestDurations:
    @pytest.mark.parametrize("where, data", [
        ("agent.timeout", {"agent": {"timeout": "{}"}}),
        ("budget.time", {"budget": {"time": "{}"}}),
    ])
    def test_durations_everywhere(self, where, data):
        def make(value):
            import json
            return json.loads(json.dumps(data).replace('"{}"', json.dumps(value)))

        for value, seconds in ((90, 90), ("90s", 90), ("30m", 1800), ("2h30m", 9000)):
            config = Config.from_dict({"goal": "g", **make(value)})
            assert (config.agent.timeout if where == "agent.timeout" else config.max_time_seconds) == seconds
        for value in ("soon", 0, -5, True, 1e300, "31d", "721h", 30 * 86400 + 1):
            assert any(where in e for e in errors({"goal": "g", **make(value)})), value
        assert not errors({"goal": "g", **make(30 * 86400)})

    def test_entry_timeouts(self):
        config = Config.from_dict({"goal": "g", "gates": [{"command": "x", "timeout": "2m"}],
                                   "observe": [{"name": "b", "command": "y", "timeout": 45}],
                                   "stop": [{"command": "z", "timeout": "1h"}]})
        assert (config.gates[0].timeout, config.observe[0].timeout, config.stop[0].timeout) == (120, 45, 3600)


def _record(iteration, metrics, accepted=True, commit="c"):
    return IterationResult(iteration=iteration, accepted=accepted, metrics=metrics,
                           commit=None if iteration == 0 else commit)


class TestMetricGateReference:
    def test_reference_is_the_tip_commit_record(self):
        records = [_record(0, {"s": 10}), _record(1, {"s": 12}), _record(2, {"s": 30}, accepted=False),
                   _record(3, {"s": 11})]
        assert metric_reference(records, "s") == 11

    def test_baseline_until_a_commit(self):
        assert metric_reference([_record(0, {"s": 10}), _record(1, {"s": 9}, accepted=False)], "s") == 10

    def test_accepted_no_op_does_not_re_measure(self):
        records = [_record(0, {"s": 10}), _record(1, {"s": 12}), _record(2, {"s": 15}, commit=None)]
        assert metric_reference(records, "s") == 12

    def test_walks_back_to_a_record_with_the_metric(self):
        records = [_record(0, {"s": 10, "t": 1}), _record(1, {"s": 12, "t": 2}), _record(2, {"s": 13})]
        assert metric_reference(records, "t") == 2
        assert metric_reference(records, "u") is None


class TestMetricGateFailure:
    def regress(self, pct):
        return MetricGate("s", "regress", pct)

    def improve(self, pct):
        return MetricGate("s", "improve", pct)

    def test_regress(self):
        assert metric_gate_failure(self.regress(5), "higher", 96, 100) is None
        assert metric_gate_failure(self.regress(5), "higher", 95, 100) is None  # exactly at the bound
        assert "5.1% worse than 100" in metric_gate_failure(self.regress(5), "higher", 94.9, 100)
        assert metric_gate_failure(self.regress(5), "lower", 105, 100) is None
        assert metric_gate_failure(self.regress(5), "lower", 106, 100)
        assert metric_gate_failure(self.regress(0), "higher", 99.99, 100)
        assert metric_gate_failure(self.regress(0), "higher", 100, 100) is None

    def test_improve(self):
        assert metric_gate_failure(self.improve(0), "higher", 100.01, 100) is None
        assert "does not improve" in metric_gate_failure(self.improve(0), "higher", 100, 100)
        assert metric_gate_failure(self.improve(5), "higher", 105, 100) is None
        assert metric_gate_failure(self.improve(5), "higher", 1.05, 1.0) is None  # float noise at the bound
        assert "by only 4.0%" in metric_gate_failure(self.improve(5), "higher", 104, 100)
        assert metric_gate_failure(self.improve(10), "lower", 0.9, 1.0) is None
        assert metric_gate_failure(self.improve(10), "lower", 0.95, 1.0)

    def test_negative_reference_uses_its_absolute_value(self):
        assert metric_gate_failure(self.regress(10), "higher", -1.05, -1.0) is None
        assert metric_gate_failure(self.regress(10), "higher", -1.2, -1.0)
        assert metric_gate_failure(self.improve(10), "higher", -0.9, -1.0) is None

    def test_near_zero(self):
        assert metric_gate_failure(self.regress(50), "lower", 0, 0) is None
        assert metric_gate_failure(self.regress(50), "lower", 0.001, 0)  # no worse than 0
        assert metric_gate_failure(self.improve(50), "lower", -0.001, 0) is None  # any strict improvement
        assert metric_gate_failure(self.improve(50), "lower", 0, 0)
        assert metric_gate_failure(self.improve(0), "lower", 0, 1) is None  # count 1 -> 0

    def test_non_finite_values_fail(self):
        for value in (float("inf"), float("-inf"), float("nan")):
            assert "not a finite number" in metric_gate_failure(self.regress(5), "higher", value, 100)
            assert "not a finite number" in metric_gate_failure(self.improve(0), "lower", value, 100)

    def test_non_finite_references_are_skipped(self):
        records = [_record(0, {"s": 10}), _record(1, {"s": float("inf")}), _record(2, {"s": float("nan")})]
        assert metric_reference(records, "s") == 10

    def test_missing_value_fails_and_missing_reference_passes(self):
        assert "was not measured" in metric_gate_failure(self.regress(5), "higher", None, 100)
        assert metric_gate_failure(self.improve(5), "higher", 1, None) is None


class TestConfigValidate:
    def test_valid_config_no_issues(self):
        data = {"goal": "optimize sorting", "gates": ["pytest", {"metric": "speed", "regress": "5%"}],
                "observe": [BENCH], "stop": ["true"], "budget": {"iterations": 3}}
        assert Config.from_dict(data).validate(data) == []

    def test_known_top_level_keys(self):
        assert KNOWN_TOP_LEVEL_KEYS == {"goal", "agent", "prompt_template", "scope", "gates", "observe", "stop",
                                        "budget", "land", "report", "name"}

    def test_empty_goal(self):
        config = Config.from_dict({"goal": ""})
        issues = config.validate()
        assert any("'goal' is empty" in i.message and i.level == "error" for i in issues)

    def test_whitespace_only_goal(self):
        assert any("'goal' is empty" in e for e in errors({"goal": "   "}))

    def test_placeholder_goal_is_error(self):
        assert any("placeholder" in e for e in errors({"goal": "Describe what you want the agent to achieve."}))

    def test_no_gates_or_observe_is_warning(self):
        assert any("No gates or observe entries" in w for w in warnings({"goal": "optimize"}))
        assert not any("No gates" in w for w in warnings({"goal": "x", "gates": ["pytest"]}))
        assert not any("No gates" in w for w in warnings({"goal": "x", "observe": [BENCH]}))

    def test_unknown_top_level_keys_are_warnings(self):
        assert any("Unknown config key 'gaol'" in w for w in warnings({"goal": "x", "gaol": "typo", "gates": ["p"]}))
        assert not any("Unknown config key" in w for w in warnings({"goal": "x", "gates": ["p"], "observe": [BENCH]}))

    def test_unknown_agent_backend_is_error(self):
        assert any("Unknown agent backend" in e for e in errors({"goal": "x", "agent": {"backend": "gpt4"}}))
        for backend in ["claude", "codex", "opencode", "mock"]:
            assert not any("Unknown agent backend" in e for e in errors({"goal": "x", "agent": {"backend": backend}}))

    def test_settings_ignored_warning_for_claude(self):
        assert any("agent.settings is ignored" in w
                   for w in warnings({"goal": "x", "agent": {"backend": "claude", "settings": {"foo": "bar"}}}))
        assert not any("agent.settings is ignored" in w
                       for w in warnings({"goal": "x", "agent": {"backend": "codex", "settings": {"foo": "bar"}}}))
        assert not any("agent.settings is ignored" in w for w in warnings({"goal": "x", "agent": {}}))

    def test_observe_entry_without_metrics_is_valid(self):
        assert not errors({"goal": "x", "observe": [{"name": "log", "command": "cat log"}]})

    def test_invalid_agent_timeout(self):
        assert any("agent.timeout" in e for e in errors({"goal": "x", "agent": {"timeout": -10}}))
        assert any("agent.timeout" in e for e in errors({"goal": "x", "agent": {"timeout": "five minutes"}}))


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

    def test_digit_string_is_seconds(self):
        assert parse_duration("90") == 90 and parse_duration(" 90 ") == 90
        for value in ("0", "9999999999"):
            with pytest.raises(ValueError):
                parse_duration(value)

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
        assert parse_duration("720h") == 30 * 24 * 3600

    def test_over_30_days_rejected(self):
        for value in ("721h", "99999h", 1e300, 30 * 24 * 3600 + 1):
            with pytest.raises(ValueError, match="at most 30 days"):
                parse_duration(value)

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
            "gates": ["pytest"],
            "budget": {"cost": -1},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("budget.cost" in i.message for i in errors)

    def test_zero_cost_error(self):
        config = Config.from_dict({
            "goal": "optimize",
            "gates": ["pytest"],
            "budget": {"cost": 0},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("budget.cost" in i.message for i in errors)

    def test_invalid_time_string_error(self):
        config = Config.from_dict({
            "goal": "optimize",
            "gates": ["pytest"],
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
            "gates": ["pytest"],
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
            "gates": ["pytest"],
            "budget": {"cost": True},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("budget.cost" in i.message for i in errors)

    def test_cost_bool_false_error(self):
        config = Config.from_dict({
            "goal": "optimize",
            "gates": ["pytest"],
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
            "gates": ["pytest"],
            "budget": {"cost": -0.0},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("budget.cost" in i.message or "cost" in i.message for i in errors)

    def test_time_zero_seconds_error(self):
        config = Config.from_dict({
            "goal": "optimize",
            "gates": ["pytest"],
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
            "gates": ["pytest"],
            "budget": {"cost": "not_a_number"},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("not_a_number" in i.message for i in errors)

    def test_time_bool_true_error(self):
        config = Config.from_dict({
            "goal": "optimize",
            "gates": ["pytest"],
            "budget": {"time": True},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("budget.time" in i.message or "time" in i.message for i in errors)

    def test_cost_bool_has_specific_error(self):
        config = Config.from_dict({
            "goal": "optimize",
            "gates": ["pytest"],
            "budget": {"cost": True},
        })
        issues = config.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("bool" in i.message for i in errors)


def test_ask_gate_accepts_unquoted_yes_and_no_keys():
    """YAML reads unquoted `yes:` / `no:` keys as booleans; they still mean --yes / --no."""
    import yaml

    config = Config.from_dict(yaml.safe_load(
        "goal: g\ngates:\n  - ask: Keeps the API?\n    yes: kept\n    no: changed\n"))
    assert not [i for i in config.validate() if i.level == "error"]
    assert shlex.split(config.gates[0].command) == [
        "autohelix", "ask", "--yes", "kept", "--no", "changed", "--", "Keeps the API?"]
