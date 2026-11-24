"""`autohelix run --check`: every check `run` makes before it writes anything, then
nothing (the skill's validation step)."""

import pytest

from tests.helpers import config_file, cli, configure


def _snapshot(project):
    """Every file under the project (its .git included) with its contents."""
    return {str(p.relative_to(project)): p.read_bytes() for p in sorted(project.rglob("*")) if p.is_file()}


def test_a_ready_config_passes_and_nothing_is_written(project):
    configure(project, gates=["true"], scope={"editable": ["code.py"]})
    before = _snapshot(project)
    result = cli(project, "run", "--check")
    assert result.exit_code == 0, result.output
    assert "OK: autohelix/demo/config.yaml is ready; `autohelix run demo` would start a new run." in result.output
    assert _snapshot(project) == before
    assert not list((project / ".autohelix").iterdir())


def test_a_named_config_is_checked(project):
    config_file(project).parent.rename(project / "autohelix" / "perf")
    result = cli(project, "run", "perf", "--check")
    assert result.exit_code == 0, result.output
    assert "`autohelix run perf` would start a new run." in result.output


@pytest.mark.parametrize(("values", "message"), [
    ({"stops": ["true"]}, "Unknown config key 'stops' - possible typo?"),
    ({"agent": {"cli": "mock", "model": ["m"]}}, "agent.model must be a non-empty string"),
    ({"agent": {"cli": "mock", "command": "/opt/claude"}}, "`agent.command` is gone: set AUTOHELIX_CLAUDE_CODE_CMD"),
    ({"agent": {"cli": "claude-code"}, "env": "/nonexistent-agent"},
     "Agent command '/nonexistent-agent' not found in PATH"),
    ({"prompt_template": "p.md"}, "prompt_template p.md, line 1: Unexpected end of template"),
])
def test_what_run_would_refuse_fails_the_check(project, values, message, monkeypatch):
    """These passed the skill's old Python check (a warning or nothing) and then
    failed `run` (or, for the template, reached the agent)."""
    (config_file(project).parent / "p.md").write_text("{% if hints %}\n")  # beside the config
    values = dict(values)
    if "env" in values:
        monkeypatch.setenv("AUTOHELIX_CLAUDE_CODE_CMD", values.pop("env"))
    configure(project, **values)
    before = _snapshot(project)
    result = cli(project, "run", "--check")
    assert result.exit_code == 1, result.output
    assert message in " ".join(result.output.split())
    assert "would start" not in result.output
    assert _snapshot(project) == before


def test_uncommitted_tracked_changes_fail_the_check(project):
    (project / "code.py").write_text("dirty\n")
    result = cli(project, "run", "--check")
    assert result.exit_code == 1
    assert "Uncommitted changes in tracked files" in result.output


def test_the_check_says_what_run_says_about_other_runs_and_names_the_flags(project):
    """`run --check --import X` warns that X's commit isn't in main, as `run` does,
    and its OK line names the command checked, flags included."""
    from autohelix.runs import short_id
    from tests.test_groups import started

    first, _ = started(project, "--land", "manual")  # its commit stays on autohelix/<id>
    short = short_id(first.run_id)
    result = cli(project, "run", "--check", "--import", short)
    assert result.exit_code == 0, result.output
    said = " ".join(result.output.split())
    assert f"{short}'s 1 accepted commit isn't in main: land it first" in said
    assert f"`autohelix run demo --import {short}` would start a new run." in said
    result = cli(project, "run", "--check", "--group", "g", "--land", "auto")
    assert result.exit_code == 0, result.output
    assert "`autohelix run demo --group g --land auto` would start a new run." in " ".join(result.output.split())


def test_the_check_warns_about_an_uncommitted_config_as_run_does(project):
    from autohelix.runs import git

    exclude = project / ".git" / "info" / "exclude"  # autohelix/ shared: not private as setup makes it
    exclude.write_text("".join(line for line in exclude.read_text().splitlines(keepends=True)
                               if line.strip() != "/autohelix/"))
    (config_file(project).parent / "bench.sh").write_text("true\n")
    before = _snapshot(project)
    result = cli(project, "run", "--check")
    assert result.exit_code == 0, result.output
    said = " ".join(result.output.split())
    assert ("commit autohelix/demo/config.yaml first: a commit on main after this run lands makes `resume` refuse. "
            "Or keep autohelix/ private (docs/concepts.md#where-configs-live).") in said
    assert "commit autohelix/demo/ first (bench.sh)" in said
    assert "would start a new run" in said and _snapshot(project) == before
    result = cli(project, "run", "--check", "--land", "manual")
    assert "first: a commit on" not in result.output
    git(project, "add", "-f", "autohelix")
    git(project, "commit", "-qm", "config")
    assert "first: a commit on" not in cli(project, "run", "--check").output


@pytest.mark.parametrize(("text", "message"), [
    ("Goal: {{ goall }}\n", "prompt_template p.md uses variables the prompt doesn't have: goall."),
    ("{{ goal }}{% if last %}{{ last.gates.x.y }}{% endif %}\n", "prompt_template p.md could not be rendered:"),
])
def test_a_template_mistake_fails_the_check(project, text, message):
    """`run --check` said OK for both; `run` then sent a prompt without the goal, or
    failed iteration 1 after the baseline."""
    (config_file(project).parent / "p.md").write_text(text)
    configure(project, prompt_template="p.md")
    result = cli(project, "run", "--check")
    assert result.exit_code == 1, result.output
    assert message in " ".join(result.output.split())
