"""Which model ran: every agent call records the CLI, the model the CLI's output named
and the executable started (`agents.call_fields`); each segment's start record the main
agent's executable, and `resume` warns when it changed; the run header, `view`'s run
screen and the web viewer's run page show it."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from autohelix import terminal
from autohelix.agents import AgentConfig
from autohelix.agents.claudecode import ClaudeCodeAgent
from autohelix.agents.codex import CodexAgent
from autohelix.agents.opencode import OpenCodeAgent
from autohelix.agents.runner import ProcessRunResult
from autohelix.cli import main
from autohelix.config import Config
from autohelix.history import History
from autohelix.judge import ask, judge_config
from autohelix.run_status import read_segments
from autohelix.runs import short_id
from autohelix.usage_log import read_usage
from autohelix.viewer import RunData
from tests.helpers import configure, latest_run


def _executable(folder: Path, name: str) -> str:
    path = folder / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return str(path)


def _call(agent, lines, tmp_path):
    def fake_run_process(**kwargs):
        for line in lines:
            kwargs["line_callback"](json.dumps(line))
        return ProcessRunResult(exit_code=0)

    module = type(agent).__module__
    with patch(f"{module}.run_process", side_effect=fake_run_process):
        return agent.run(copy_path=tmp_path, prompt="p", iteration=1, log_path=tmp_path / "agent.log",
                         event_callback=lambda event: None)


@pytest.mark.parametrize("agent_class, cli, variable, lines, model", [
    (ClaudeCodeAgent, "claude-code", "AUTOHELIX_CLAUDE_CODE_CMD",
     [{"type": "system", "subtype": "init", "model": "claude-opus-4-1"},
      {"type": "result", "subtype": "success", "total_cost_usd": 0.1, "usage": {}}], "claude-opus-4-1"),
    (CodexAgent, "codex", "AUTOHELIX_CODEX_CMD",
     [{"type": "thread.started", "model": "gpt-5-codex"},
      {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}], "gpt-5-codex"),
    (OpenCodeAgent, "opencode", "AUTOHELIX_OPENCODE_CMD",  # its events name no model
     [{"type": "step_finish", "part": {"reason": "stop", "cost": 0.1}}], None),
])
def test_each_backend_records_its_cli_reported_model_and_executable(agent_class, cli, variable, lines, model,
                                                                     tmp_path, monkeypatch):
    wrapper = _executable(tmp_path, "wrapper")
    monkeypatch.setenv(variable, wrapper)
    result = _call(agent_class(AgentConfig(cli=cli, model="configured")), lines, tmp_path)
    assert (result.usage["cli"], result.usage["model"], result.usage["executable"]) == (cli, model, wrapper)


def test_claude_code_takes_the_model_from_the_init_event_only(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOHELIX_CLAUDE_CODE_CMD", _executable(tmp_path, "claude"))
    lines = [{"type": "system", "subtype": "hook_response", "model": "other"},
             {"type": "result", "subtype": "success", "usage": {}}]
    assert _call(ClaudeCodeAgent(AgentConfig(model="opus")), lines, tmp_path).usage["model"] is None


def test_a_name_in_the_variable_is_resolved_on_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    path = _executable(bin_dir, "claude-bedrock")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AUTOHELIX_CLAUDE_CODE_CMD", "claude-bedrock")
    assert ClaudeCodeAgent(AgentConfig()).executable == path


def test_the_agent_line(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    plain = tmp_path / "path"
    plain.mkdir()
    claude = _executable(plain, "claude")
    monkeypatch.setenv("PATH", str(plain))
    wrapper = _executable(home / "bin", "claude-bedrock")
    assert terminal.agent_text("claude-code", "opus", wrapper) == "Claude Code · opus · via ~/bin/claude-bedrock"
    assert terminal.agent_text("claude-code", None, claude) == "Claude Code · (its default)"
    assert terminal.agent_text("codex", "gpt-5") == "Codex · gpt-5"
    fields = dict(terminal.run_fields(Config.from_dict({"goal": "g", "agent": {"cli": "opencode"}}), ""))
    assert fields["Agent"] == "OpenCode · (its default)"


def _cli(project, *args):
    result = CliRunner().invoke(main, [*args, "-p", str(project)] if args[0] in ("run", "resume") else list(args))
    assert result.exit_code == 0, result.output
    return result


def test_a_run_records_which_model_ran_and_shows_it(project, monkeypatch):
    """The main agent's call, an `ask:` judge's and the run summary's record the CLI,
    the reported model and the executable; the header shows the configured model,
    `view` and the run page the reported one."""
    configure(project, agent={"cli": "mock", "model": "configured-model",
                              "mock": {"change_file": "code.py", "model": "reported-model"}},
              gates=[{"ask": "ok?"}])
    monkeypatch.setenv("AUTOHELIX_MOCK_CMD", "/opt/mock-agent")
    monkeypatch.chdir(project)
    output = _cli(project, "run").output
    assert "Agent      mock · configured-model" in output
    paths = latest_run(project)
    [record] = [r for r in History(project, paths.run_id).load() if r.iteration == 1]
    assert {k: record.usage[k] for k in ("cli", "model", "executable")} == {
        "cli": "mock", "model": "reported-model", "executable": "/opt/mock-agent"}
    [judge] = [call for call in read_usage(paths.usage(1)) if call["status"] == "ok"]
    assert (judge["cli"], judge["model"], judge["executable"]) == ("mock", "reported-model", "/opt/mock-agent")
    [summary] = [call for call in read_usage(paths.summary_usage) if call["status"] != "started"]
    assert (summary["cli"], summary["model"], summary["executable"]) == ("mock", "reported-model", "/opt/mock-agent")

    assert "Agent      mock · reported-model" in _cli(project, "view", short_id(paths.run_id), "--once").output
    assert RunData(project, paths.run_id).state()["config"]["agent"] == "mock · reported-model"
    [segment] = read_segments(paths.segments)
    assert segment["executable"] == "/opt/mock-agent"
    from autohelix.terminal import version
    assert segment["autohelix_version"] == version()  # which AutoHelix ran the segment, as its header says


def test_resume_warns_when_the_executable_changed(project, monkeypatch):
    monkeypatch.setenv("AUTOHELIX_MOCK_CMD", "/opt/agent-a")
    _cli(project, "run", "--no-report")
    assert "now starts" not in _cli(project, "resume", "--no-report").output  # the same one
    monkeypatch.setenv("AUTOHELIX_MOCK_CMD", "/opt/agent-b")
    output = " ".join(_cli(project, "resume", "--no-report").output.split())
    assert "The agent now starts /opt/agent-b (the run's last segment started /opt/agent-a)" in output
    assert [s["executable"] for s in read_segments(latest_run(project).segments)] == [
        "/opt/agent-a", "/opt/agent-a", "/opt/agent-b"]


def test_a_judge_by_hand_records_its_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOHELIX_USAGE_FILE", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("AUTOHELIX_LOG_DIR", str(tmp_path / "logs"))
    ask("Done?", tmp_path, judge_config(AgentConfig(cli="mock")))
    [done] = read_usage(tmp_path / "usage.jsonl")  # the call's final record
    assert done["cli"] == "mock" and "agent" not in done
    assert done["model"] is None and done["executable"] is None
