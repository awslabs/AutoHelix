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
        (tmp_path / ".autohelix").mkdir()
        (tmp_path / ".autohelix" / "prompt.md").write_text("Old copy: {{ goal }}")  # no longer read
        template = load_template(Config.from_dict({"goal": "g"}))
        assert "{{ goal }}" in template and "Old copy" not in template

    def test_prompt_template_is_relative_to_the_config(self, tmp_path):
        from autohelix.config import load_config

        (tmp_path / "configs" / "prompts").mkdir(parents=True)
        (tmp_path / "configs" / "prompts" / "agent.md").write_text("Custom: {{ goal }}")
        (tmp_path / "configs" / "a.yaml").write_text("goal: g\nprompt_template: prompts/agent.md\n")
        config, raw = load_config(tmp_path, tmp_path / "configs" / "a.yaml")
        assert load_template(config) == "Custom: {{ goal }}"
        assert not [i for i in config.validate(raw) if i.level == "error"]

    def test_a_missing_prompt_template_is_a_config_error(self, tmp_path):
        from autohelix.config import load_config

        (tmp_path / "autohelix.yaml").write_text("goal: g\nprompt_template: nope.md\n")
        config, raw = load_config(tmp_path)
        assert any("prompt_template not found" in i.message for i in config.validate(raw))


class TestBuildPromptVariables:
    def _make_config(self, iteration_time_seconds=None):
        config = Config(goal="test")
        config.agent.timeout = iteration_time_seconds
        return config

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
        rendered = render_template(load_template(config), variables)
        assert "Time budget: 15m." in rendered
        assert "timed out" not in rendered

    def test_gates_observe_and_scope_render(self, tmp_path):
        (tmp_path / ".autohelix").mkdir()
        config = Config.from_dict({
            "goal": "Make it fast.",
            "gates": ["pytest -q", {"ask": "Keeps the API?"}, {"metric": "speed", "regress": "5%"},
                      {"metric": "speed", "improve": 0}],
            "observe": [{"name": "bench", "command": "python bench.py", "metrics": {"speed": "higher"}},
                        {"name": "review", "review": "Review the diff."}],
            "scope": {"editable": ["src/"], "frozen": ["src/fixtures/"]},
        })
        variables = build_prompt_variables(config, History(tmp_path), iteration=1, worktree_dir=tmp_path)
        assert variables["constraints"] == [
            "pytest -q", "ask: Keeps the API?",
            "speed: at most 5% worse than the last accepted iteration",
            "speed: better than the last accepted iteration",
        ]
        rendered = render_template(load_template(config), variables)
        assert "Gates (must pass" in rendered
        assert "bench: python bench.py → speed (↑)" in rendered
        assert 'review: autohelix review "Review the diff."' in rendered
        assert "Editable (only changes to these files will be kept): src/" in rendered
        assert "Frozen (changes to these files will be reverted, even inside the editable ones): src/fixtures/" in rendered
        assert "review.md" not in rendered

    def test_frozen_alone_renders(self, tmp_path):
        (tmp_path / ".autohelix").mkdir()
        config = Config.from_dict({"goal": "g", "scope": {"frozen": ["tests/"]}})
        variables = build_prompt_variables(config, History(tmp_path), iteration=1, worktree_dir=tmp_path)
        rendered = render_template(load_template(config), variables)
        assert "Frozen (changes to these files will be reverted): tests/" in rendered
        assert "Editable" not in rendered
