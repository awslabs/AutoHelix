"""`autohelix run --check`: every check `run` makes before it writes anything, then
nothing (the skill's validation step)."""

import pytest

from tests.helpers import cli, configure


def _snapshot(project):
    """Every file under the project (its .git included) with its contents."""
    return {str(p.relative_to(project)): p.read_bytes() for p in sorted(project.rglob("*")) if p.is_file()}


def test_a_ready_config_passes_and_nothing_is_written(project):
    configure(project, gates=["true"], scope={"editable": ["code.py"]})
    before = _snapshot(project)
    result = cli(project, "run", "--check")
    assert result.exit_code == 0, result.output
    assert "OK: autohelix.yaml is ready; `autohelix run` would start a new run." in result.output
    assert _snapshot(project) == before
    assert not list((project / ".autohelix").iterdir())


def test_a_named_config_is_checked(project):
    (project / "autohelix").mkdir()
    (project / "autohelix" / "perf.yaml").write_text((project / "autohelix.yaml").read_text())
    (project / "autohelix.yaml").unlink()
    from autohelix.runs import git

    git(project, "add", "-A")
    git(project, "commit", "-qm", "named")
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
    (project / "p.md").write_text("{% if hints %}\n")
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
