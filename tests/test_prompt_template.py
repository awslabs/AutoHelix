"""Tests for autohelix.prompt_template module."""

import pytest

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

    def test_syntax_error_raises_naming_the_template(self):
        """It used to become the agent's prompt, an error banner then the raw template."""
        with pytest.raises(RuntimeError, match=r"prompt_template p\.md, line 1: Unexpected end of template"):
            render_template("{% if unclosed %}", {}, "p.md")

    def test_render_error_raises_naming_the_template(self):
        with pytest.raises(RuntimeError, match="prompt_template p.md could not be rendered: .*has no attribute 'x'"):
            render_template("{{ last.gates.x.y }}", {"last": {"gates": []}}, "p.md")

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
        assert not [i for i in config.validate() if i.level == "error"]

    def test_a_missing_prompt_template_is_a_config_error(self, tmp_path):
        from autohelix.config import load_config

        (tmp_path / "autohelix.yaml").write_text("goal: g\nprompt_template: nope.md\n")
        config, raw = load_config(tmp_path)
        assert any("prompt_template not found" in i.message for i in config.validate())

    @pytest.mark.parametrize("stored", [False, True])
    def test_a_template_that_does_not_parse_is_a_config_error(self, tmp_path, stored):
        """An unclosed `{% if %}` used to reach the agent as its prompt; now config
        validation names the file and line (also a run's stored copy)."""
        from autohelix.config import inlined, load_config

        (tmp_path / "p.md").write_text("ok\n{% if hints %}x\n")
        (tmp_path / "autohelix.yaml").write_text("goal: g\nprompt_template: p.md\n")
        if stored:
            import yaml

            data = inlined(tmp_path / "autohelix.yaml")
            (tmp_path / "stored.yaml").write_text(yaml.safe_dump(data))
            config, _ = load_config(tmp_path, tmp_path / "stored.yaml", source=tmp_path / "autohelix.yaml")
        else:
            config, _ = load_config(tmp_path)
        found = [i.message for i in config.validate() if i.level == "error"]
        assert any(m.startswith("prompt_template p.md, line 2: Unexpected end of template") for m in found), found


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
            "group_runs", "imported"}
        assert variables["title"] == "Title line."
        assert variables["notes_path"] == str(tmp_path / ".autohelix-iteration" / "notes" / "iter-1.md")
        assert variables["last"] is None and variables["discarded"] == [] and variables["rejected_patch"] == ""
        assert variables["imported"] is None

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


def _render(tmp_path, records, iteration, *, patch=False, group=None, members=(), imported=None, seed=True,
            rejected_diff="", hints=False, head=None, missing=()):
    """Render the default template; `members`/`imported` are run ids, their state put in the
    copy unless `missing` names them (it failed to copy)."""
    from autohelix.history import IterationResult
    from autohelix.runs import RunPaths

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
    for run_id in [*members, *([imported] if imported else [])]:
        if run_id not in missing:
            (worktree / ".autohelix-iteration" / "runs" / run_id / "notes").mkdir(parents=True)
    if hints:
        history.paths.hints.write_text("- try a heap\n")
    config = Config.from_dict(CONFIG)
    variables = build_prompt_variables(
        config, history, iteration, worktree, group=group,
        members=[(RunPaths(tmp_path, run_id), {}) for run_id in members],
        imported=(RunPaths(tmp_path, imported), {}) if imported else None, rejected_diff=rejected_diff, head=head)
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


PARENT, MEMBER = "20261112-1100-perf-a1b2", "20261112-1200-other-k3f9"
REJECTED = {"iteration": 1, "accepted": False, "reason": "gate failed: pytest -q",
            "failure_output": "FAILED test_sort.py::test_negative", "tree": "abc",
            "rejected_ref": "refs/autohelix-rejected/x/1",
            "gates": [{"text": "pytest -q", "kind": "command", "passed": False},
                      {"text": "speed regress 5%", "kind": "metric", "passed": None}]}


def test_a_plain_run_has_no_layout_block(tmp_path):
    _, rendered = _render(tmp_path, [BASELINE, REJECTED], 2, patch=True, hints=True)
    assert "Read before starting:\n- .autohelix-iteration/notes/iter-*.md — your own notes from past iterations" in rendered
    assert "Where things are" not in rendered and "runs/" not in rendered and "autohelix/<id>" not in rendered
    assert "the last rejected attempt's change" not in rendered  # said once, with the rejection
    assert rendered.count("rejected.patch") == 1
    assert "  Older iterations: .autohelix-iteration/history.jsonl\n- .autohelix-iteration/hints.md\n" in rendered


NOTES_LINE = ("- .autohelix-iteration/notes/ — yours: this run's notes, editable; numbered by this run's iterations "
              "(iter-1.md onward), separately from other runs' iter-N.md\n")
RUNS_LINE = ("- .autohelix-iteration/runs/<id>/ — other runs' run.json, history.jsonl and notes/ (no iterations/: their "
             "observe outputs aren't copied); read-only (a copy; edits there are discarded)\n")
CODE_LINES = ("- Other runs' code: refs `autohelix/<id>` in this copy's git (not branches: `git branch` doesn't list them;\n"
              "  `git for-each-ref refs/autohelix` does): `git diff HEAD autohelix/<id>`, `git show autohelix/<id>:<file>`,\n"
              "  `git checkout autohelix/<id> -- <file>`.")


def test_group_runs(tmp_path):
    (tmp_path / ".autohelix").mkdir()
    variables, rendered = _render(tmp_path, [BASELINE], 1, group="perf", members=[MEMBER])
    assert variables["group"] == "perf" and len(variables["group_runs"]) == 1
    assert ("Where things are (read before starting):\n" + NOTES_LINE +
            "- .autohelix-iteration/iterations/NNN/ — this run's own last 5 iterations and its last accepted one") in rendered
    assert (RUNS_LINE +
            "  - Group `perf` members (other runs alongside this one, running or finished; their state as of this iteration's start):\n"
            f"    `.autohelix-iteration/runs/{MEMBER}/` (0 iterations, finished; code at `autohelix/{MEMBER}`)\n"
            + CODE_LINES) in rendered
    assert "name that run in your summary" in rendered
    assert "Imported" not in rendered and "Read before starting:" not in rendered


def test_a_member_whose_state_isnt_in_the_copy_is_left_out(tmp_path):
    other = "20261112-1300-third-z9y8"
    variables, rendered = _render(tmp_path, [BASELINE], 1, group="perf", members=[MEMBER, other], missing=[other])
    assert [line.split("`")[1] for line in variables["group_runs"]] == [f".autohelix-iteration/runs/{MEMBER}/"]
    assert other not in rendered
    _, rendered = _render(tmp_path / "alone", [BASELINE], 1, group="perf", members=[other], missing=[other])
    assert "Where things are" not in rendered and "Read before starting:" in rendered


def test_an_import(tmp_path):
    from autohelix.history import IterationResult

    (tmp_path / ".autohelix").mkdir()
    parent = History(tmp_path, PARENT)
    for record in (BASELINE, {**BASELINE, "iteration": 1, "reason": None, "metrics": {"speed": 12.5}}):
        parent.append(IterationResult(**record))
    variables, rendered = _render(tmp_path, [BASELINE], 1, imported=PARENT)
    line = (f"`.autohelix-iteration/runs/{PARENT}/` (1 iteration, latest accepted speed 12.5, finished; "
            f"code at `autohelix/{PARENT}`)")
    assert variables["imported"] == {"id": PARENT, "short": "a1b2", "path": f".autohelix-iteration/runs/{PARENT}/",
                                     "description": line, "same_code": False}  # no repository: unknown
    assert ("  - Imported (this run continues from a1b2; its notes and history are where that work left off):\n"
            f"    {line}\n"
            "    Your copy holds this run's code, not necessarily a1b2's: "
            f"`git diff HEAD autohelix/{PARENT}` shows what differs.\n"
            "- Other runs' code:") in rendered
    assert NOTES_LINE in rendered
    assert "Group" not in rendered and "name that run in your summary" not in rendered


def test_an_import_says_when_the_copys_code_differs_from_its_tip(tmp_path):
    import subprocess

    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    git("init", "-q")
    (tmp_path / "sort.py").write_text("v1\n")
    git("add", "sort.py")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "v1")
    first = git("rev-parse", "HEAD")
    git("update-ref", f"refs/autohelix/{PARENT}", first)
    variables, rendered = _render(tmp_path, [BASELINE], 1, imported=PARENT, head=first)
    assert variables["imported"]["same_code"] is True
    assert "Your copy holds this run's code" not in rendered and "  - Imported (" in rendered

    (tmp_path / "sort.py").write_text("v2\n")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "v2")
    variables, rendered = _render(tmp_path / "later", [BASELINE], 1, imported=PARENT, head=git("rev-parse", "HEAD"))
    assert variables["imported"]["same_code"] is False
    assert "    Your copy holds this run's code, not necessarily a1b2's" in rendered


def test_an_import_in_a_group_after_a_rejection(tmp_path):
    (tmp_path / ".autohelix").mkdir()
    _, rendered = _render(tmp_path, [BASELINE, REJECTED], 2, patch=True, group="perf", members=[MEMBER],
                          imported=PARENT, hints=True)
    imported = rendered.index("  - Imported (this run continues from a1b2")
    assert imported < rendered.index("  - Group `perf` members") < rendered.index("- Other runs' code:")
    assert ("  Older iterations: .autohelix-iteration/history.jsonl\n"
            "- .autohelix-iteration/rejected.patch — the last rejected attempt's change\n"
            "- .autohelix-iteration/hints.md — hints from the person running this run\n" + RUNS_LINE) in rendered
    assert "Its changes are in `.autohelix-iteration/rejected.patch`; `git apply` it" in rendered
    assert "name that run in your summary" in rendered


def test_a_large_rejected_change_is_in_the_block(tmp_path):
    _, rendered = _render(tmp_path, [BASELINE, REJECTED], 2, imported=PARENT, rejected_diff="git diff abc def")
    assert ("- `git diff abc def` — the last rejected attempt's change (too large for a file)\n" + RUNS_LINE) in rendered
    _, plain = _render(tmp_path / "plain", [BASELINE, REJECTED], 2, rejected_diff="git diff abc def")
    assert "too large for a file" not in plain and "too large for a patch file" in plain


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
