"""The `agent:` block: cli, model, effort, timeout, extra_args, auto_memory; the keys,
values, flags and variables that are gone are errors naming their replacement."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from autohelix.agents import AgentConfig, command_path, command_variable
from autohelix.agents.claudecode import ClaudeCodeAgent
from autohelix.agents.codex import CodexAgent
from autohelix.agents.opencode import OpenCodeAgent
from autohelix.checks import preflight_check
from autohelix.cli import main
from autohelix.config import Config


def _errors(agent) -> list[str]:
    return [issue.message for issue in Config.from_dict({"goal": "g", "agent": agent}).validate()
            if issue.level == "error"]


def test_the_agent_block():
    config = AgentConfig.from_dict({"cli": "codex", "model": "gpt-5", "effort": "high", "timeout": "45m",
                                    "extra_args": ["--x"], "auto_memory": True})
    assert (config.cli, config.model, config.effort, config.timeout, config.extra_args, config.auto_memory) == (
        "codex", "gpt-5", "high", 2700, ["--x"], True)


def test_the_default_cli_is_claude_code_with_its_own_model():
    config = AgentConfig.from_dict(None)
    assert (config.cli, config.model, config.effort) == ("claude-code", None, None)


@pytest.mark.parametrize("agent, message", [
    ({"backend": "codex"}, "`agent.backend` is now `agent.cli`"),
    ({"reasoning_effort": "high"}, "`agent.reasoning_effort` is now `agent.effort`"),
    ({"command": "/opt/claude"}, "set AUTOHELIX_CLAUDE_CODE_CMD"),
    ({"settings": {"a": 1}}, "pass the CLI's own options in `agent.extra_args`"),
    ({"require_notes": True}, "`agent.require_notes` is gone"),
    ({"cli": "claude"}, "`agent.cli: claude` is now `claude-code`"),
    ({"cli": "bogus"}, "Unknown agent.cli 'bogus'"),
    ({"cli": "codex", "mock": {}}, "Unknown key agent.mock"),  # only the hidden test CLI takes it
])
def test_removed_keys_and_values_name_the_replacement(agent, message):
    errors = _errors(agent)
    assert any(message in error for error in errors), errors


@pytest.mark.parametrize("entry_agent, message", [
    ({"backend": "codex"}, "`agent.backend` is now `agent.cli`"),
    ({"reasoning_effort": "high"}, "`agent.reasoning_effort` is now `agent.effort`"),
    ({"command": "/opt/codex"}, "AUTOHELIX_CODEX_CMD"),
    ({"cli": "claude"}, "is now `claude-code`"),
])
def test_an_entry_agent_takes_cli_model_effort(entry_agent, message):
    config = Config.from_dict({"goal": "g", "gates": [{"ask": "ok?", "agent": entry_agent}]})
    errors = [issue.message for issue in config.validate() if issue.level == "error"]
    assert any(message in error for error in errors), errors


def test_an_entry_agent_renders_to_cli_flags():
    config = Config.from_dict({"goal": "g", "gates": [{"ask": "ok?", "agent": {"cli": "codex", "model": "m",
                                                                                "effort": "low"}}],
                               "observe": [{"name": "r", "review": "look", "agent": {"cli": "claude-code"}}]})
    assert config.validate() == []
    assert "--cli codex --model m --effort low" in config.gates[0].command
    assert "--cli claude-code" in config.observe[0].command
    assert config.agent_commands() == [("agent", "claude-code"), ("ask: ok?", "codex"),
                                       ("observe r", "claude-code")]


@pytest.mark.parametrize("cli, agent_class, name", [
    ("claude-code", ClaudeCodeAgent, "claude"), ("codex", CodexAgent, "codex"), ("opencode", OpenCodeAgent, "opencode")])
def test_the_executable_comes_from_the_variable_else_path(cli, agent_class, name, monkeypatch, tmp_path):
    assert command_variable(cli) == {"claude-code": "AUTOHELIX_CLAUDE_CODE_CMD", "codex": "AUTOHELIX_CODEX_CMD",
                                     "opencode": "AUTOHELIX_OPENCODE_CMD"}[cli]
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv(command_variable(cli), raising=False)
    assert agent_class(AgentConfig(cli=cli))._command_path() == name
    (tmp_path / name).write_text("#!/bin/sh\n")
    (tmp_path / name).chmod(0o755)
    assert command_path(cli) == str(tmp_path / name)
    monkeypatch.setenv(command_variable(cli), "/opt/wrapper")
    assert agent_class(AgentConfig(cli=cli))._command_path() == "/opt/wrapper"


def test_effort_is_passed_through_to_each_cli():
    assert ["--effort", "high"] == _after(ClaudeCodeAgent(AgentConfig(effort="high"))._build_command("p"), "--effort")
    assert 'model_reasoning_effort="high"' in CodexAgent(AgentConfig(cli="codex", effort="high"))._build_command("p")
    assert ["--variant", "high"] == _after(OpenCodeAgent(AgentConfig(cli="opencode", effort="high"))._build_command("p"),
                                           "--variant")


def _after(cmd: list[str], flag: str) -> list[str]:
    return cmd[cmd.index(flag):cmd.index(flag) + 2]


def test_the_old_claude_variable_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOHELIX_CLAUDE_CMD", "/opt/claude")
    with pytest.raises(ValueError, match="AUTOHELIX_CLAUDE_CMD is now AUTOHELIX_CLAUDE_CODE_CMD"):
        command_path("claude-code")
    issues = preflight_check(Config.from_dict({"goal": "g", "agent": {"cli": "mock"}}), tmp_path)
    assert [i.message for i in issues if i.level == "error"] == [
        "AUTOHELIX_CLAUDE_CMD is now AUTOHELIX_CLAUDE_CODE_CMD: set that instead (and unset AUTOHELIX_CLAUDE_CMD)"]


@pytest.mark.parametrize("command", ["ask", "review"])
@pytest.mark.parametrize("args, message", [
    (["--agent", "codex"], "--agent is now --cli"),
    (["--command", "/opt/codex"], "--command is gone: set AUTOHELIX_CLAUDE_CODE_CMD or AUTOHELIX_CODEX_CMD"),
    (["--cli", "claude"], "'claude' is now 'claude-code'"),
])
def test_ask_and_review_flags_that_are_gone(command, args, message, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, [command, *args, "--", "ok?"])
    assert result.exit_code == 2 and message in result.output, result.output


@pytest.mark.parametrize("command", ["ask", "review"])
def test_ask_and_review_refuse_the_old_variable(command, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTOHELIX_CLAUDE_CMD", "/opt/claude")
    result = CliRunner().invoke(main, [command, "--cli", "codex", "--", "ok?"])
    assert result.exit_code == 2 and "AUTOHELIX_CLAUDE_CMD is now AUTOHELIX_CLAUDE_CODE_CMD" in result.output


def test_no_check_notes_hook_is_shipped():
    assert not (Path(__file__).parent.parent / "src" / "autohelix" / "bin" / "check_notes.sh").exists()
