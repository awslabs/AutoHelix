"""Tests for the preflight_check function."""

import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from autohelix.checks import preflight_check, PreflightIssue, ConstraintResult, ObservableResult
from autohelix.config import load_config, Config, ObservableCommand


@pytest.fixture
def project(tmp_path):
    """Create a minimal project with a valid config."""
    config = textwrap.dedent("""\
        goal: Make it faster
        constraints:
          - echo ok
        observables:
          - command: 'echo speed: 42'
            values:
              speed: higher
    """)
    (tmp_path / "autohelix.yaml").write_text(config)
    return tmp_path


class TestPreflightCheck:
    def test_all_passing(self, project):
        config, raw = load_config(project)
        with patch("autohelix.checks.run_constraint") as mock_c, \
             patch("autohelix.checks.run_observable") as mock_m, \
             patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            mock_c.return_value = ConstraintResult(
                command="echo ok", passed=True, output="ok\n", return_code=0,
            )
            mock_m.return_value = ObservableResult(
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
        assert any("No constraints or observables" in i.message for i in warnings)

    def test_agent_not_found_is_error(self, project):
        config, raw = load_config(project)
        with patch("autohelix.checks.run_constraint") as mock_c, \
             patch("autohelix.checks.run_observable") as mock_m, \
             patch("autohelix.checks.shutil.which", return_value=None):
            mock_c.return_value = ConstraintResult(
                command="echo ok", passed=True, output="ok\n", return_code=0,
            )
            mock_m.return_value = ObservableResult(
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

    def test_no_scope_warns_about_constraint_targets(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_foo.py").write_text("")
        config_text = textwrap.dedent("""\
            goal: Make it faster
            constraints:
              - pytest tests/
        """)
        (tmp_path / "autohelix.yaml").write_text(config_text)
        config, raw = load_config(tmp_path)
        with patch("autohelix.checks.run_constraint") as mock_c, \
             patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            mock_c.return_value = ConstraintResult(
                command="pytest tests/", passed=True, output="ok\n", return_code=0,
            )
            issues = preflight_check(config, raw, tmp_path)
        warnings = [i for i in issues if i.level == "warning"]
        assert any("No scope set" in i.message and "tests/" in i.message for i in warnings)

    def test_no_scope_warning_absent_when_editable_set(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_foo.py").write_text("")
        (tmp_path / "src").mkdir()
        config_text = textwrap.dedent("""\
            goal: Make it faster
            constraints:
              - pytest tests/
            scope:
              editable: [src/]
        """)
        (tmp_path / "autohelix.yaml").write_text(config_text)
        config, raw = load_config(tmp_path)
        with patch("autohelix.checks.run_constraint") as mock_c, \
             patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            mock_c.return_value = ConstraintResult(
                command="pytest tests/", passed=True, output="ok\n", return_code=0,
            )
            issues = preflight_check(config, raw, tmp_path)
        warnings = [i for i in issues if i.level == "warning"]
        assert not any("No scope set" in i.message for i in warnings)

    def test_no_scope_no_warning_when_no_targets_found(self, tmp_path):
        config_text = textwrap.dedent("""\
            goal: Make it faster
            constraints:
              - echo ok
        """)
        (tmp_path / "autohelix.yaml").write_text(config_text)
        config, raw = load_config(tmp_path)
        with patch("autohelix.checks.run_constraint") as mock_c, \
             patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            mock_c.return_value = ConstraintResult(
                command="echo ok", passed=True, output="ok\n", return_code=0,
            )
            issues = preflight_check(config, raw, tmp_path)
        warnings = [i for i in issues if i.level == "warning"]
        assert not any("No scope set" in i.message for i in warnings)

    def test_multiple_values_from_one_command(self, tmp_path):
        config_text = textwrap.dedent("""\
            goal: Make it faster
            observables:
              - command: python bench.py
                values:
                  throughput: higher
                  latency: lower
        """)
        (tmp_path / "autohelix.yaml").write_text(config_text)
        config, raw = load_config(tmp_path)
        with patch("autohelix.checks.run_observable") as mock_m, \
             patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            mock_m.return_value = ObservableResult(
                command="python bench.py",
                values={"throughput": 1500.0, "latency": 0.5},
                output="throughput: 1500\nlatency: 0.5\n",
                errors={},
            )
            issues = preflight_check(config, raw, tmp_path)
        errors = [i for i in issues if i.level == "error"]
        assert not any("throughput" in i.message or "latency" in i.message for i in errors)
