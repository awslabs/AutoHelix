"""Tests for the preflight_check function."""

import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from autohelix.checks import preflight_check, PreflightIssue, CommandResult, ObserveResult
from autohelix.config import load_config


@pytest.fixture
def project(tmp_path):
    """Create a minimal project with a valid config."""
    config = textwrap.dedent("""\
        goal: Make it faster
        gates:
          - echo ok
        observe:
          - name: bench
            command: 'echo speed: 42'
            metrics:
              speed: higher
    """)
    (tmp_path / "autohelix.yaml").write_text(config)
    return tmp_path


class TestPreflightCheck:
    def test_all_passing(self, project):
        config, raw = load_config(project)
        with patch("autohelix.checks.run_command") as mock_c, \
             patch("autohelix.checks.run_observe_entry") as mock_m, \
             patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            mock_c.return_value = CommandResult(
                command="echo ok", passed=True, output="ok\n", return_code=0,
            )
            mock_m.return_value = ObserveResult(
                command="echo speed: 42", values={"speed": 42.0}, output="speed: 42\n", errors={},
            )
            issues = preflight_check(config, raw, project)
        assert issues == []

    def test_empty_goal_is_error(self, tmp_path):
        (tmp_path / "autohelix.yaml").write_text("goal: ''\n")
        config, raw = load_config(tmp_path)
        with patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            issues = preflight_check(config, raw, tmp_path)
        errors = [i for i in issues if i.level == "error"]
        assert any("goal" in i.message.lower() for i in errors)

    def test_placeholder_goal_is_error(self, tmp_path):
        (tmp_path / "autohelix.yaml").write_text(
            "goal: Describe what you want the agent to achieve.\n"
        )
        config, raw = load_config(tmp_path)
        with patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            issues = preflight_check(config, raw, tmp_path)
        errors = [i for i in issues if i.level == "error"]
        assert any("placeholder" in i.message for i in errors)

    def test_no_constraints_or_metrics_is_warning(self, tmp_path):
        (tmp_path / "autohelix.yaml").write_text("goal: Make it faster\n")
        config, raw = load_config(tmp_path)
        with patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            issues = preflight_check(config, raw, tmp_path)
        warnings = [i for i in issues if i.level == "warning"]
        assert any("No gates or observe entries" in i.message for i in warnings)

    def test_agent_not_found_is_error(self, project):
        config, raw = load_config(project)
        with patch("autohelix.checks.run_command") as mock_c, \
             patch("autohelix.checks.run_observe_entry") as mock_m, \
             patch("autohelix.checks.shutil.which", return_value=None):
            mock_c.return_value = CommandResult(
                command="echo ok", passed=True, output="ok\n", return_code=0,
            )
            mock_m.return_value = ObserveResult(
                command="echo speed: 42", values={"speed": 42.0}, output="speed: 42\n", errors={},
            )
            issues = preflight_check(config, raw, project)
        errors = [i for i in issues if i.level == "error"]
        assert any("not found in PATH" in i.message for i in errors)

    def test_scope_no_match_is_warning(self, tmp_path):
        config_text = textwrap.dedent("""\
            goal: Make it faster
            scope:
              editable: [nonexistent/]
        """)
        (tmp_path / "autohelix.yaml").write_text(config_text)
        config, raw = load_config(tmp_path)
        with patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            issues = preflight_check(config, raw, tmp_path)
        warnings = [i for i in issues if i.level == "warning"]
        assert any("matches no files" in i.message for i in warnings)

    @pytest.mark.parametrize("config, warns", [
        ({"gates": ["pytest tests/"]}, True),
        ({"observe": [{"name": "bench", "command": "python bench.py"}]}, True),
        ({"gates": [{"metric": "speed", "regress": "5%"}],
          "observe": [{"name": "bench", "command": "b", "metrics": {"speed": "higher"}}]}, True),
        ({"gates": ["pytest tests/"], "scope": {"editable": ["src/"]}}, False),
        ({"gates": ["pytest tests/"], "scope": {"frozen": ["tests/"]}}, False),
        ({}, False),
    ])
    def test_no_scope_warns_when_commands_judge_the_agent(self, config, warns):
        from autohelix.checks import scope_warning
        from autohelix.config import Config

        warning = scope_warning(Config.from_dict({"goal": "Make it faster", **config}))
        assert (warning is not None) == warns
        if warns:
            assert "the agent may edit the tests and benchmarks that gate it" in warning
            assert "scope.editable" in warning

    def test_preflight_no_longer_guesses_gate_targets(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "autohelix.yaml").write_text("goal: Make it faster\ngates:\n  - pytest tests/\n")
        config, raw = load_config(tmp_path)
        with patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            issues = preflight_check(config, raw, tmp_path)
        assert not any("scope" in i.message.lower() for i in issues)

    def test_multiple_values_from_one_command(self, tmp_path):
        config_text = textwrap.dedent("""\
            goal: Make it faster
            observe:
              - name: bench
                command: python bench.py
                metrics:
                  throughput: higher
                  latency: lower
        """)
        (tmp_path / "autohelix.yaml").write_text(config_text)
        config, raw = load_config(tmp_path)
        with patch("autohelix.checks.run_observe_entry") as mock_m, \
             patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            mock_m.return_value = ObserveResult(
                command="python bench.py",
                values={"throughput": 1500.0, "latency": 0.5},
                output="throughput: 1500\nlatency: 0.5\n",
                errors={},
            )
            issues = preflight_check(config, raw, tmp_path)
        errors = [i for i in issues if i.level == "error"]
        assert not any("throughput" in i.message or "latency" in i.message for i in errors)
