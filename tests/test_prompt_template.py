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

    def test_the_documented_variables(self, tmp_path):
        (tmp_path / ".autohelix").mkdir()
        variables = build_prompt_variables(Config(goal="Title line.\nMore."), History(tmp_path), iteration=1,
                                           worktree_dir=tmp_path)
        assert set(variables) == {
            "goal", "title", "iteration", "worktree", "history", "gates", "observe", "stop", "editable",
            "frozen", "notes_path", "hints", "rejected_patch", "rejected_diff", "discarded", "discarded_frozen", "last", "last_accepted", "recent_iterations", "time_limit", "group",
            "group_runs"}
        assert variables["title"] == "Title line."
        assert variables["notes_path"] == str(tmp_path / ".autohelix-iteration" / "notes" / "iter-1.md")
        assert variables["last"] is None and variables["discarded"] == [] and variables["rejected_patch"] == ""

    def test_hints_empty_when_no_file(self, tmp_path):
        (tmp_path / ".autohelix").mkdir()
        variables = build_prompt_variables(self._make_config(), History(tmp_path), iteration=1, worktree_dir=tmp_path)
        assert variables["hints"] == ""

    def test_hints_is_the_copys_file(self, tmp_path):
        ah = tmp_path / ".autohelix"
        ah.mkdir(parents=True)
        (ah / "hints.md").write_text("- [2026-07-07 10:00] try numpy\n")
        variables = build_prompt_variables(self._make_config(), History(tmp_path), iteration=1, worktree_dir=tmp_path)
        assert variables["hints"] == ".autohelix-iteration/hints.md"

    def test_time_limit_is_compact_duration(self, tmp_path):
        # Must be a forward-looking duration ("15m"), NOT the timeout *message*
        # ("timed out after 15 minutes"), which would read as if the run already died.
        (tmp_path / ".autohelix").mkdir()
        config = self._make_config(iteration_time_seconds=900)
        variables = build_prompt_variables(config, History(tmp_path), iteration=1, worktree_dir=tmp_path)
        assert variables["time_limit"] == "15m"
        rendered = render_template(load_template(config), variables)
        assert "Time limit: 15m." in rendered and "timed out" not in rendered

    def test_time_limit_empty_when_unset(self, tmp_path):
        (tmp_path / ".autohelix").mkdir()
        variables = build_prompt_variables(self._make_config(), History(tmp_path), iteration=1, worktree_dir=tmp_path)
        assert variables["time_limit"] == ""
        assert "Time limit" not in render_template(load_template(self._make_config()), variables)

    def test_gates_observe_and_scope_render(self, tmp_path):
        (tmp_path / ".autohelix").mkdir()
        config = Config.from_dict({
            "goal": "Make it fast.",
            "gates": ["pytest -q", {"ask": "Keeps the API?"}, {"metric": "speed", "regress": "5%"},
                      {"metric": "speed", "improve": "0%"}],
            "observe": [{"name": "bench", "command": "python bench.py", "metrics": {"speed": "higher"}},
                        {"name": "review", "review": "Review the\ndiff."}],
            "scope": {"editable": ["src/"], "frozen": ["src/fixtures/"]},
        })
        variables = build_prompt_variables(config, History(tmp_path), iteration=1, worktree_dir=tmp_path)
        assert variables["gates"] == [
            {"text": "pytest -q", "kind": "command"}, {"text": "ask: Keeps the API?", "kind": "command"},
            {"text": "speed: at most 5% worse than the last accepted iteration", "kind": "metric"},
            {"text": "speed: better than the last accepted iteration", "kind": "metric"},
        ]
        assert variables["observe"] == [
            {"name": "bench", "kind": "command", "command": "python bench.py", "metric_labels": ["speed (↑)"]},
            {"name": "review", "kind": "review", "prompt": "Review the diff.", "metric_labels": []},
        ]
        rendered = render_template(load_template(config), variables)
        assert "Gates (must pass" in rendered
        assert "bench: python bench.py → speed (↑)" in rendered
        assert "review: review: Review the diff." in rendered
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


# --- the default template after each kind of iteration ---------------------------

CONFIG = {
    "goal": "Make sort fast.",
    "gates": ["pytest -q", {"metric": "speed", "regress": "5%"}],
    "observe": [{"name": "bench", "command": "python bench.py", "metrics": {"speed": "higher"}},
                {"name": "progress", "review": "Track the PRD."}],
    "scope": {"editable": ["sort.py"]},
}


def _seed(worktree, records):
    """The iteration folders seeding would put in the copy: the record, failing gates', observe entries'."""
    for record in records:
        folder = worktree / ".autohelix-iteration" / "iterations" / f"{record['iteration']:03d}"
        folder.mkdir(parents=True, exist_ok=True)
        for k, gate in enumerate(record.get("gates") or [], start=1):
            if gate.get("passed") is False and gate.get("kind", "command") == "command":
                (folder / "gates" / str(k)).mkdir(parents=True, exist_ok=True)
        for item in record.get("observe") or []:
            (folder / "observe" / item["name"]).mkdir(parents=True, exist_ok=True)


def _render(tmp_path, records, iteration, *, patch=False, group=None, members=(), seed=True):
    from autohelix.history import IterationResult

    (tmp_path / ".autohelix").mkdir(parents=True, exist_ok=True)
    history = History(tmp_path)
    for record in records:
        history.append(IterationResult(**record))
    worktree = tmp_path / "copy"
    (worktree / ".autohelix-iteration").mkdir(parents=True, exist_ok=True)
    if patch:
        (worktree / ".autohelix-iteration" / "rejected.patch").write_text("diff --git a/sort.py b/sort.py\n")
    if seed:
        _seed(worktree, records)
    config = Config.from_dict(CONFIG)
    variables = build_prompt_variables(config, history, iteration, worktree, group=group, members=members)
    return variables, render_template(load_template(config), variables)


BASELINE = {"iteration": 0, "accepted": True, "metrics": {"speed": 10.0}, "reason": "baseline",
            "gates": [{"text": "pytest -q", "kind": "command", "passed": True},
                      {"text": "speed regress 5%", "kind": "metric", "passed": None}],
            "observe": [{"name": "bench", "ok": True, "error": None, "metrics": {"speed": 10.0}},
                        {"name": "progress", "ok": True, "error": None, "metrics": {}}]}


def test_first_iteration(tmp_path):
    variables, rendered = _render(tmp_path, [BASELINE], 1)
    assert variables["last"]["iteration"] == 0
    assert "This is iteration 1. Baseline is iter-0." in rendered
    assert "Last accepted: the baseline (iter-0); your copy holds its code." in rendered
    assert "  bench: .autohelix-iteration/iterations/000/observe/bench/" in rendered
    assert "rejected.patch" not in rendered and "were discarded" not in rendered
    assert "- iter 0: speed=10.0 (accepted)" in rendered
    assert f"write to {tmp_path / 'copy' / '.autohelix-iteration' / 'notes' / 'iter-1.md'}." in rendered


def test_after_an_accepted_iteration(tmp_path):
    accepted = {**BASELINE, "iteration": 1, "reason": None, "summary": "Use insertion sort",
                "gates": [{"text": "pytest -q", "kind": "command", "passed": True},
                          {"text": "speed regress 5%", "kind": "metric", "passed": True}],
                "observe": [{"name": "bench", "ok": True, "error": None, "metrics": {"speed": 12.0}},
                            {"name": "progress", "ok": False, "error": "exited 2", "metrics": {}}]}
    variables, rendered = _render(tmp_path, [BASELINE, accepted], 2)
    assert "Last accepted: iter-1; your copy holds its code." in rendered
    assert "Its gate results:\n  ✓ pytest -q\n  ✓ speed regress 5%" in rendered
    assert "Read the latest observations (its):" in rendered
    assert "  progress: .autohelix-iteration/iterations/001/observe/progress/ (failed: exited 2)" in rendered
    assert "- iter 1: speed=10.0 (accepted) — Use insertion sort" in rendered
    assert "    observe progress failed: exited 2" in rendered
    assert "was rejected" not in rendered


def test_after_a_rejection_with_patch_and_discarded_paths(tmp_path):
    rejected = {"iteration": 1, "accepted": False, "reason": "gate failed: pytest -q",
                "failure_output": "FAILED test_sort.py::test_negative", "summary": "Try radix sort",
                "discarded": ["tests/test_sort.py"], "tree": "abc", "rejected_ref": "refs/autohelix-rejected/x/1",
                "gates": [{"text": "pytest -q", "kind": "command", "passed": False},
                          {"text": "speed regress 5%", "kind": "metric", "passed": None}]}
    variables, rendered = _render(tmp_path, [BASELINE, rejected], 2, patch=True)
    assert variables["rejected_patch"] == ".autohelix-iteration/rejected.patch"
    assert variables["discarded"] == ["tests/test_sort.py"]
    assert "Iteration 1 was rejected: gate failed: pytest -q" in rendered
    assert ("Its rejected attempt's gate results:\n"
            "  ✗ pytest -q (output: .autohelix-iteration/iterations/001/gates/1/)\n"
            "  · speed regress 5% (not reached)") in rendered
    assert "Failure output:\n    FAILED test_sort.py::test_negative" in rendered
    assert ("The previous attempt was rejected (failure output above). Its changes are in "
            "`.autohelix-iteration/rejected.patch`; `git apply` it if it was close, or start over.") in rendered
    assert "Your changes to tests/test_sort.py were discarded: outside the editable scope." in rendered
    assert "- iter 1: rejected (gate failed: pytest -q) — Try radix sort" in rendered
    assert "Last accepted" not in rendered


def test_rejection_after_observe_shows_what_it_produced(tmp_path):
    rejected = {"iteration": 1, "accepted": False, "reason": "metric gate: speed 5 is 50.0% worse than 10",
                "gates": [{"text": "pytest -q", "kind": "command", "passed": True},
                          {"text": "speed regress 5%", "kind": "metric", "passed": False}],
                "observe": [{"name": "bench", "ok": True, "error": None, "metrics": {"speed": 5.0}}]}
    _, rendered = _render(tmp_path, [BASELINE, rejected], 2)
    assert "Read the latest observations (what the rejected attempt produced):" in rendered
    assert "  bench: .autohelix-iteration/iterations/001/observe/bench/" in rendered
    assert "rejected.patch" not in rendered  # no kept tree, no patch


def test_group_runs(tmp_path):
    from autohelix.runs import RunPaths

    (tmp_path / ".autohelix").mkdir()
    member = RunPaths(tmp_path, "20261112-1200-other-k3f9")
    variables, rendered = _render(tmp_path, [BASELINE], 1, group="perf", members=[(member, {})])
    assert variables["group"] == "perf" and len(variables["group_runs"]) == 1
    assert "Other runs in group `perf`" in rendered
    assert "`.autohelix-iteration/runs/20261112-1200-other-k3f9/`" in rendered


def test_the_prompt_names_only_folders_that_are_in_the_copy(tmp_path):
    rejected = {"iteration": 1, "accepted": False, "reason": "metric gate failed",
                "gates": [{"text": "pytest -q", "kind": "command", "passed": False},
                          {"text": "speed regress 5%", "kind": "metric", "passed": False}],
                "observe": [{"name": "bench", "ok": False, "error": "exited 1", "metrics": {}}]}
    variables, rendered = _render(tmp_path, [BASELINE, rejected], 2, seed=False)
    assert [g["path"] for g in variables["last"]["gates"]] == ["", ""]
    assert variables["last"]["observe"][0]["path"] == ""
    assert variables["last_accepted"] == {"iteration": 0, "path": ""}
    assert "  ✗ pytest -q\n  ✗ speed regress 5%\n" in rendered and "(output:" not in rendered
    assert "  bench (failed: exited 1)" in rendered
    assert "Your copy holds the last accepted code (the baseline)." in rendered

    variables, rendered = _render(tmp_path / "seeded", [BASELINE, rejected], 2)
    # A metric gate runs nothing: never a folder, failed or not.
    assert [g["path"] for g in variables["last"]["gates"]] == [".autohelix-iteration/iterations/001/gates/1/", ""]
    assert "  ✗ pytest -q (output: .autohelix-iteration/iterations/001/gates/1/)\n  ✗ speed regress 5%\n" in rendered
    assert "  bench: .autohelix-iteration/iterations/001/observe/bench/ (failed: exited 1)" in rendered
    assert variables["last_accepted"] == {"iteration": 0, "path": ".autohelix-iteration/iterations/000/"}
    assert ("Your copy holds the last accepted code (the baseline; its record and observations: "
            ".autohelix-iteration/iterations/000/).") in rendered


def test_the_prompt_describes_the_copys_iteration_folders(tmp_path):
    _, rendered = _render(tmp_path, [BASELINE], 1)
    assert ("- .autohelix-iteration/iterations/NNN/ — the last 5 iterations and the last accepted one: record.json,\n"
            "  gates/<k>/ of failed gates (k: position in the list above; stdout.txt, stderr.txt, ask.log),\n"
            "  observe/<name>/ (stdout.txt, stderr.txt, captured files, review.log). "
            "ask.log/review.log are judge transcripts, often long.\n"
            "  Older iterations: .autohelix-iteration/history.jsonl") in rendered
    from autohelix.iteration import SEEDED_ITERATIONS

    assert f"the last {SEEDED_ITERATIONS} iterations" in rendered
