"""Tests for the preflight_check function."""

import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from autohelix.checks import preflight_check, PreflightIssue, CommandResult, ObserveResult
from autohelix.config import load_config


def _write(folder: Path, text: str) -> Path:
    """The folder's only experiment config, autohelix/p/config.yaml."""
    path = folder / "autohelix" / "p" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def project(tmp_path):
    """Create a minimal project with a valid config."""
    config = textwrap.dedent("""\
        goal: Make it faster
        gates:
          - echo ok
        observe:
          - name: bench
            command: 'echo "##autohelix[speed=42]"'
            metrics:
              speed: higher
    """)
    _write(tmp_path, config)
    return tmp_path


class TestPreflightCheck:
    def test_all_passing(self, project):
        config = load_config(project)
        with patch("autohelix.checks.run_command") as mock_c, \
             patch("autohelix.checks.run_observe_entry") as mock_m, \
             patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            mock_c.return_value = CommandResult(
                command="echo ok", passed=True, output="ok\n", return_code=0,
            )
            mock_m.return_value = ObserveResult(
                command="echo speed: 42", values={"speed": 42.0}, output="speed: 42\n", errors={},
            )
            issues = preflight_check(config, project)
        assert issues == []

    def test_empty_goal_is_error(self, tmp_path):
        _write(tmp_path, "goal: ''\n")
        config = load_config(tmp_path)
        with patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            issues = preflight_check(config, tmp_path)
        errors = [i for i in issues if i.level == "error"]
        assert any("goal" in i.message.lower() for i in errors)

    def test_placeholder_goal_is_error(self, tmp_path):
        _write(tmp_path, "goal: Describe what you want the agent to achieve.\n")
        config = load_config(tmp_path)
        with patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            issues = preflight_check(config, tmp_path)
        errors = [i for i in issues if i.level == "error"]
        assert any("placeholder" in i.message for i in errors)

    def test_no_constraints_or_metrics_is_warning(self, tmp_path):
        _write(tmp_path, "goal: Make it faster\n")
        config = load_config(tmp_path)
        with patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            issues = preflight_check(config, tmp_path)
        warnings = [i for i in issues if i.level == "warning"]
        assert any("No gates or observe entries" in i.message for i in warnings)

    def test_agent_not_found_is_error(self, project):
        config = load_config(project)
        with patch("autohelix.checks.run_command") as mock_c, \
             patch("autohelix.checks.run_observe_entry") as mock_m, \
             patch("autohelix.checks.shutil.which", return_value=None):
            mock_c.return_value = CommandResult(
                command="echo ok", passed=True, output="ok\n", return_code=0,
            )
            mock_m.return_value = ObserveResult(
                command="echo speed: 42", values={"speed": 42.0}, output="speed: 42\n", errors={},
            )
            issues = preflight_check(config, project)
        errors = [i for i in issues if i.level == "error"]
        assert any("not found in PATH" in i.message for i in errors)

    def test_scope_no_match_is_warning(self, tmp_path):
        config_text = textwrap.dedent("""\
            goal: Make it faster
            scope:
              editable: [nonexistent/]
        """)
        _write(tmp_path, config_text)
        config = load_config(tmp_path)
        with patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            issues = preflight_check(config, tmp_path)
        warnings = [i for i in issues if i.level == "warning"]
        assert any("matches no committed file" in i.message for i in warnings)

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
        _write(tmp_path, "goal: Make it faster\ngates:\n  - pytest tests/\n")
        config = load_config(tmp_path)
        with patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            issues = preflight_check(config, tmp_path)
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
        _write(tmp_path, config_text)
        config = load_config(tmp_path)
        with patch("autohelix.checks.run_observe_entry") as mock_m, \
             patch("autohelix.checks.shutil.which", return_value="/usr/bin/claude"):
            mock_m.return_value = ObserveResult(
                command="python bench.py",
                values={"throughput": 1500.0, "latency": 0.5},
                output="throughput: 1500\nlatency: 0.5\n",
                errors={},
            )
            issues = preflight_check(config, tmp_path)
        errors = [i for i in issues if i.level == "error"]
        assert not any("throughput" in i.message or "latency" in i.message for i in errors)


# ── Scope patterns counted over `git ls-files` with the harness's matcher [01-1, 03-5] ──


def _committed(folder: Path, files: dict[str, str], untracked: dict[str, str] | None = None) -> Path:
    import subprocess
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=folder, check=True)
    for name, text in {**files, **(untracked or {})}.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text)
    subprocess.run(["git", "add", "--", *files], cwd=folder, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "c"],
                   cwd=folder, check=True)
    return folder


def _scope_warnings(folder: Path, scope: str) -> list[str]:
    _write(folder, f"goal: g\nagent: {{cli: mock}}\nscope:\n{scope}")
    config = load_config(folder)
    return [i.message for i in preflight_check(config, folder) if "scope." in i.message]


def test_preflight_matches_like_the_harness(tmp_path):
    folder = _committed(tmp_path / "p", {"tests/test_a.py": "", "src/a.py": "", "top.py": ""},
                        untracked={"notes/plan.md": ""})
    assert _scope_warnings(folder, "  frozen: [tests/**/*.py, ./src/, '*.py']\n") == []
    # Untracked files don't count; `*` stays within a folder.
    assert _scope_warnings(folder, "  editable: [notes/, 'tests/*.md']\n") == [
        "scope.editable 'notes/' matches no committed file",
        "scope.editable 'tests/*.md' matches no committed file",
    ]


def test_preflight_in_a_folder_with_glob_characters(tmp_path):
    """The project path is never read as a pattern: no false warning for `[` or `*`."""
    folder = _committed(tmp_path / "proj[1]*", {"src/a.py": ""})
    assert _scope_warnings(folder, "  editable: [src/]\n") == []


def test_preflight_in_a_subdirectory_counts_its_own_files(tmp_path):
    repo = _committed(tmp_path / "repo", {"sub/src/a.py": "", "src/other.py": ""})
    assert _scope_warnings(repo / "sub", "  editable: [src/a.py]\n") == []
    assert _scope_warnings(repo / "sub", "  editable: [src/other.py]\n") == [
        "scope.editable 'src/other.py' matches no committed file"]


# ── Every agent's real command, as its backend resolves it [03-3] ──


def _agent_errors(folder: Path, monkeypatch, config: str, **env: str) -> list[str]:
    """Preflight's agent errors with nothing on PATH but `bin/` (empty unless a test adds to it)."""
    (folder / "bin").mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(folder / "bin"))
    for cli in ("CLAUDE_CODE", "CODEX", "OPENCODE"):
        monkeypatch.delenv(f"AUTOHELIX_{cli}_CMD", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _write(folder, config)
    return [i.message for i in preflight_check(load_config(folder), folder)
            if i.level == "error" and "Agent command" in i.message]


def _executable(path: Path) -> str:
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


def test_the_documented_command_override_is_honoured(tmp_path, monkeypatch):
    """`AUTOHELIX_CLAUDE_CODE_CMD`, which the backend runs, is what preflight checks."""
    wrapper = _executable(tmp_path / "my-claude")
    assert _agent_errors(tmp_path, monkeypatch, "goal: g\n", AUTOHELIX_CLAUDE_CODE_CMD=wrapper) == []
    assert _agent_errors(tmp_path, monkeypatch, "goal: g\n") == [
        "Agent command 'claude' not found in PATH; install Claude Code, or set AUTOHELIX_CLAUDE_CODE_CMD"]
    _executable(tmp_path / "bin" / "claude")
    assert _agent_errors(tmp_path, monkeypatch, "goal: g\n") == []


def test_judge_clis_are_checked(tmp_path, monkeypatch):
    """An `ask:` or `review:` judge whose CLI isn't installed used to pass preflight
    and then fail every iteration's check."""
    config = textwrap.dedent("""\
        goal: g
        agent: {cli: mock}
        gates:
          - ask: Keeps the API?
            agent: {cli: codex}
        observe:
          - name: notes
            review: Review it
            agent: {cli: claude-code}
          - name: inherits
            review: Review it too
    """)
    nowhere = {"AUTOHELIX_CLAUDE_CODE_CMD": "/nowhere/claude"}
    assert _agent_errors(tmp_path, monkeypatch, config, **nowhere) == [
        "Agent command 'codex' not found in PATH (used by `ask: Keeps the API?`); install Codex, or set "
        "AUTOHELIX_CODEX_CMD",
        "Agent command '/nowhere/claude' not found in PATH (used by `observe notes`); install Claude Code, or set "
        "AUTOHELIX_CLAUDE_CODE_CMD",
    ]
    assert _agent_errors(tmp_path, monkeypatch, config, AUTOHELIX_CODEX_CMD=_executable(tmp_path / "cx"),
                         **nowhere) == [
        "Agent command '/nowhere/claude' not found in PATH (used by `observe notes`); install Claude Code, or set "
        "AUTOHELIX_CLAUDE_CODE_CMD"]


def test_a_judge_on_the_main_cli_shares_its_executable(tmp_path, monkeypatch):
    main = _executable(tmp_path / "claude-main")
    config = "goal: g\ngates: [{ask: 'Done?'}]\n"
    assert _agent_errors(tmp_path, monkeypatch, config, AUTOHELIX_CLAUDE_CODE_CMD=main) == []
    config = "goal: g\ngates: [{ask: 'Done?', agent: {cli: codex}}]\n"
    assert _agent_errors(tmp_path, monkeypatch, config, AUTOHELIX_CLAUDE_CODE_CMD=main) == [
        "Agent command 'codex' not found in PATH (used by `ask: Done?`); install Codex, or set AUTOHELIX_CODEX_CMD"]


@pytest.mark.parametrize("cli, module, cls", [
    ("claude-code", "claudecode", "ClaudeCodeAgent"), ("codex", "codex", "CodexAgent"),
    ("opencode", "opencode", "OpenCodeAgent")])
def test_backends_resolve_their_command_as_preflight_does(tmp_path, monkeypatch, cli, module, cls):
    import importlib

    from autohelix.agents import EXECUTABLES, AgentConfig, command_path, command_variable

    agent_class = getattr(importlib.import_module(f"autohelix.agents.{module}"), cls)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv(command_variable(cli), "/opt/wrapper")
    assert agent_class(AgentConfig(cli=cli))._command_path() == command_path(cli) == "/opt/wrapper"
    monkeypatch.delenv(command_variable(cli))
    assert agent_class(AgentConfig(cli=cli))._command_path() == EXECUTABLES[cli]
