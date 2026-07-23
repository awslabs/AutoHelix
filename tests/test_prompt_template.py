"""Tests for autohelix.prompt_template module."""

from autohelix.config import Config
from autohelix.history import History
from autohelix.prompt_template import build_prompt_variables, render_template, load_template


class TestRenderTemplate:
    def test_basic_substitution(self):
        result = render_template("Hello {{ name }}!", {"name": "world"})
        assert result == "Hello world!\n"

    def test_multiple_variables(self):
        result = render_template("{{ a }} and {{ b }}", {"a": "X", "b": "Y"})
        assert result == "X and Y\n"

    def test_conditional_block(self):
        template = "{% if show %}visible{% endif %}"
        assert "visible" in render_template(template, {"show": True})
        assert "visible" not in render_template(template, {"show": False})

    def test_loop(self):
        template = "{% for item in items %}{{ item }}\n{% endfor %}"
        result = render_template(template, {"items": ["a", "b", "c"]})
        assert "a" in result
        assert "b" in result
        assert "c" in result

    def test_empty_list_conditional(self):
        template = "{% if items %}has items{% endif %}"
        assert "has items" not in render_template(template, {"items": []})
        assert "has items" in render_template(template, {"items": ["x"]})

    def test_collapses_blank_lines(self):
        template = "line1\n\n\n\n\nline2\n"
        result = render_template(template, {})
        assert "\n\n\n" not in result
        assert "line1" in result
        assert "line2" in result

    def test_trailing_newline(self):
        result = render_template("hello", {})
        assert result.endswith("\n")

    def test_no_leading_trailing_whitespace(self):
        result = render_template("\n\nhello\n\n", {})
        assert result == "hello\n"

    def test_syntax_error_falls_back(self):
        template = "{% if unclosed %}"
        result = render_template(template, {})
        assert "[Template error:" in result

    def test_trim_blocks(self):
        template = "before\n{% if x %}\nyes\n{% endif %}\nafter"
        result = render_template(template, {"x": True})
        assert result == "before\nyes\nafter\n"

    def test_lstrip_blocks(self):
        template = "  {% if x %}yes{% endif %}"
        result = render_template(template, {"x": True})
        assert result == "yes\n"


class TestLoadTemplate:
    def test_default_template(self, tmp_path):
        template = load_template(tmp_path)
        assert "{{ goal }}" in template

    def test_custom_template(self, tmp_path):
        autohelix_dir = tmp_path / ".autohelix"
        autohelix_dir.mkdir()
        (autohelix_dir / "prompt.md").write_text("Custom: {{ goal }}")
        template = load_template(tmp_path)
        assert template == "Custom: {{ goal }}"


class TestBuildPromptVariables:
    def _make_config(self, constraints=None, observables=None, iteration_time_seconds=None):
        return Config(
            goal="test",
            constraints=constraints or [],
            observables=observables or [],
            iteration_time_seconds=iteration_time_seconds,
        )

    def test_has_hints_false_when_no_file(self, tmp_path):
        (tmp_path / ".autohelix").mkdir()
        config = self._make_config()
        history = History(tmp_path)
        variables = build_prompt_variables(config, history, iteration=1, worktree_dir=tmp_path)
        assert variables["has_hints"] is False

    def test_has_hints_true_when_file_exists(self, tmp_path):
        ah = tmp_path / ".autohelix"
        ah.mkdir(parents=True)
        (ah / "hints.md").write_text("- [2026-07-07 10:00] try numpy\n")
        config = self._make_config()
        history = History(tmp_path)
        variables = build_prompt_variables(config, history, iteration=1, worktree_dir=tmp_path)
        assert variables["has_hints"] is True

    def test_iteration_time_is_compact_duration(self, tmp_path):
        # Must be a forward-looking duration ("15m"), NOT the timeout *message*
        # ("timed out after 15 minutes"), which would read as if the run already died.
        (tmp_path / ".autohelix").mkdir()
        config = self._make_config(iteration_time_seconds=900)
        history = History(tmp_path)
        variables = build_prompt_variables(config, history, iteration=1, worktree_dir=tmp_path)
        assert variables["iteration_time"] == "15m"

    def test_iteration_time_empty_when_unset(self, tmp_path):
        (tmp_path / ".autohelix").mkdir()
        config = self._make_config(iteration_time_seconds=None)
        history = History(tmp_path)
        variables = build_prompt_variables(config, history, iteration=1, worktree_dir=tmp_path)
        assert variables["iteration_time"] == ""

    def test_default_template_renders_sane_time_budget(self, tmp_path):
        # End-to-end: the rendered "Time budget:" line the agent actually sees.
        (tmp_path / ".autohelix").mkdir()
        config = self._make_config(iteration_time_seconds=900)
        history = History(tmp_path)
        variables = build_prompt_variables(config, history, iteration=1, worktree_dir=tmp_path)
        rendered = render_template(load_template(tmp_path), variables)
        assert "Time budget: 15m." in rendered
        assert "timed out" not in rendered
