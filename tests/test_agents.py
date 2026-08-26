"""Tests for agent backend line parsing (_handle_line)."""

import json

import pytest

from autohelix.agents import AgentConfig, AgentEvent, AgentEventKind
from autohelix.agents.claudecode import ClaudeCodeAgent
from autohelix.agents.codex import CodexAgent
from autohelix.agents.opencode import OpenCodeAgent


def _collect_events(agent, line: str) -> list[AgentEvent]:
    """Feed a line to _handle_line and collect emitted events."""
    events: list[AgentEvent] = []
    agent._handle_line(line, events.append)
    return events


class TestClaudeCodeHandleLine:
    def _agent(self):
        return ClaudeCodeAgent(config=AgentConfig())

    def test_invalid_json_emits_raw(self):
        events = _collect_events(self._agent(), "not json at all")
        assert len(events) == 1
        assert events[0].kind == AgentEventKind.RAW
        assert events[0].text == "not json at all"

    def test_system_event(self):
        line = json.dumps({"type": "system", "subtype": "init", "model": "opus"})
        events = _collect_events(self._agent(), line)
        assert len(events) == 1
        assert events[0].kind == AgentEventKind.STATUS
        assert "init" in events[0].text
        assert "opus" in events[0].text

    def test_system_event_no_model(self):
        line = json.dumps({"type": "system", "subtype": "ready"})
        events = _collect_events(self._agent(), line)
        assert len(events) == 1
        assert events[0].text == "ready"

    def test_stream_text_delta(self):
        agent = self._agent()
        line = json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hello"},
            },
        })
        events = _collect_events(agent, line)
        assert len(events) == 1
        assert events[0].kind == AgentEventKind.ASSISTANT_TEXT
        assert events[0].text == "hello"

    def test_stream_text_delta_accumulates(self):
        agent = self._agent()
        line1 = json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hel"},
            },
        })
        line2 = json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "lo"},
            },
        })
        _collect_events(agent, line1)
        events = _collect_events(agent, line2)
        assert events[0].text == "hello"

    def test_stream_thinking_delta(self):
        agent = self._agent()
        line = json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "thinking_delta", "thinking": "pondering"},
            },
        })
        events = _collect_events(agent, line)
        assert events[0].kind == AgentEventKind.THINKING

    def test_tool_use_block_start(self):
        agent = self._agent()
        line = json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "name": "Read"},
            },
        })
        events = _collect_events(agent, line)
        assert len(events) == 1
        assert events[0].kind == AgentEventKind.TOOL_USE
        assert events[0].text == "Read"

    def test_content_block_stop_flushes(self):
        agent = self._agent()
        # Start streaming text
        delta_line = json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "final"},
            },
        })
        _collect_events(agent, delta_line)
        # Stop should flush
        stop_line = json.dumps({
            "type": "stream_event",
            "event": {"type": "content_block_stop"},
        })
        events = _collect_events(agent, stop_line)
        assert len(events) == 1
        assert events[0].kind == AgentEventKind.ASSISTANT_TEXT
        assert events[0].metadata.get("final") is True

    def test_message_delta_stop_reason(self):
        agent = self._agent()
        line = json.dumps({
            "type": "stream_event",
            "event": {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
            },
        })
        events = _collect_events(agent, line)
        assert any(e.kind == AgentEventKind.STATUS and "end_turn" in e.text for e in events)

    def test_assistant_tool_use_block(self):
        agent = self._agent()
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ],
            },
        })
        events = _collect_events(agent, line)
        assert len(events) == 1
        assert events[0].kind == AgentEventKind.TOOL_INPUT
        assert events[0].metadata.get("tool_name") == "Bash"

    def test_user_tool_result(self):
        agent = self._agent()
        line = json.dumps({
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "content": "file contents here"},
                ],
            },
        })
        events = _collect_events(agent, line)
        assert len(events) == 1
        assert events[0].kind == AgentEventKind.TOOL_RESULT

    def test_user_tool_result_string_format(self):
        agent = self._agent()
        line = json.dumps({
            "type": "user",
            "tool_use_result": "some output",
        })
        events = _collect_events(agent, line)
        assert len(events) == 1
        assert events[0].kind == AgentEventKind.TOOL_RESULT
        assert events[0].text == "some output"

    def test_result_event(self):
        agent = self._agent()
        line = json.dumps({"type": "result", "subtype": "success", "duration_ms": 1234})
        events = _collect_events(agent, line)
        assert len(events) == 1
        assert events[0].kind == AgentEventKind.RESULT
        assert "success" in events[0].text
        assert "1234" in events[0].text

    def test_result_event_captures_usage(self):
        agent = self._agent()
        line = json.dumps({
            "type": "result",
            "subtype": "success",
            "duration_ms": 5000,
            "total_cost_usd": 0.1234,
            "usage": {"input_tokens": 10000, "output_tokens": 2000},
        })
        _collect_events(agent, line)
        assert agent._usage["input_tokens"] == 10000
        assert agent._usage["output_tokens"] == 2000
        assert agent._usage["cost_usd"] == 0.1234
        assert agent._usage["duration_ms"] == 5000

    def test_result_event_no_usage(self):
        agent = self._agent()
        line = json.dumps({"type": "result", "subtype": "success"})
        _collect_events(agent, line)
        assert agent._usage == {}

    def test_input_json_delta(self):
        agent = self._agent()
        # First register a tool
        start_line = json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "name": "Edit"},
            },
        })
        _collect_events(agent, start_line)
        # Then send input delta
        delta_line = json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"file":'},
            },
        })
        events = _collect_events(agent, delta_line)
        assert len(events) == 1
        assert events[0].kind == AgentEventKind.TOOL_INPUT
        assert events[0].metadata.get("tool_name") == "Edit"


class TestClaudeCodeBuildCommand:
    def test_no_model_flag_by_default(self):
        """When no model is configured, --model should not appear in the command."""
        agent = ClaudeCodeAgent(config=AgentConfig())
        cmd = agent._build_command("test prompt")
        assert "--model" not in cmd

    def test_model_flag_when_configured(self):
        """When model is explicitly set, --model should appear."""
        agent = ClaudeCodeAgent(config=AgentConfig(model="claude-sonnet-4-6"))
        cmd = agent._build_command("test prompt")
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-sonnet-4-6"

    def test_prompt_is_last_arg(self):
        agent = ClaudeCodeAgent(config=AgentConfig())
        cmd = agent._build_command("do the thing")
        assert cmd[-1] == "do the thing"


class TestClaudeCodeRunResult:
    def test_json_error_fails_even_when_process_exits_zero(self, tmp_path):
        from unittest.mock import patch

        from autohelix.agents.runner import ProcessRunResult

        agent = ClaudeCodeAgent(config=AgentConfig())

        def fake_run_process(**kwargs):
            kwargs["line_callback"](json.dumps({
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": "API Error: invalid model",
                "duration_ms": 10,
                "total_cost_usd": 0,
                "usage": {},
            }))
            return ProcessRunResult(exit_code=0)

        with patch(
            "autohelix.agents.claudecode.run_process",
            side_effect=fake_run_process,
        ):
            result = agent.run(
                worktree_path=tmp_path,
                prompt="test",
                iteration=1,
                log_path=tmp_path / "agent.log",
                event_callback=lambda event: None,
            )

        assert not result.success
        assert result.exit_code == 0
        assert result.error == "API Error: invalid model"


class TestClaudeCodeAutoMemory:
    def test_auto_memory_disabled_sets_env(self):
        """When auto_memory is False, env should include CLAUDE_CODE_DISABLE_AUTO_MEMORY."""
        import os
        from unittest.mock import patch, MagicMock
        from autohelix.agents.claudecode import ClaudeCodeAgent

        agent = ClaudeCodeAgent(config=AgentConfig(auto_memory=False))
        agent._usage = {}

        # Patch run_process to capture the env argument
        captured_env = {}
        def fake_run_process(**kwargs):
            captured_env.update(kwargs.get("env") or {})
            result = MagicMock()
            result.exit_code = 0
            result.timed_out = False
            return result

        with patch("autohelix.agents.claudecode.run_process", side_effect=fake_run_process):
            from pathlib import Path
            agent.run(
                worktree_path=Path("/tmp/fake"),
                prompt="test",
                iteration=1,
                log_path=Path("/tmp/fake.log"),
                event_callback=lambda e: None,
                project_path=Path("/tmp/project"),
            )
        assert captured_env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1"

    def test_auto_memory_default_sets_env(self):
        """auto_memory defaults to False, so env should set the disable flag."""
        from unittest.mock import patch, MagicMock
        from autohelix.agents.claudecode import ClaudeCodeAgent

        agent = ClaudeCodeAgent(config=AgentConfig())
        agent._usage = {}

        captured_env = {}
        def fake_run_process(**kwargs):
            captured_env.update(kwargs.get("env") or {})
            result = MagicMock()
            result.exit_code = 0
            result.timed_out = False
            return result

        with patch("autohelix.agents.claudecode.run_process", side_effect=fake_run_process):
            from pathlib import Path
            agent.run(
                worktree_path=Path("/tmp/fake"),
                prompt="test",
                iteration=1,
                log_path=Path("/tmp/fake.log"),
                event_callback=lambda e: None,
                project_path=Path("/tmp/project"),
            )
        assert captured_env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1"

    def test_auto_memory_enabled_no_env(self):
        """When auto_memory is True, env should not set the disable flag."""
        from unittest.mock import patch, MagicMock
        from autohelix.agents.claudecode import ClaudeCodeAgent

        agent = ClaudeCodeAgent(config=AgentConfig(auto_memory=True))
        agent._usage = {}

        captured_env = {}
        def fake_run_process(**kwargs):
            captured_env.update(kwargs.get("env") or {})
            result = MagicMock()
            result.exit_code = 0
            result.timed_out = False
            return result

        with patch("autohelix.agents.claudecode.run_process", side_effect=fake_run_process):
            from pathlib import Path
            agent.run(
                worktree_path=Path("/tmp/fake"),
                prompt="test",
                iteration=1,
                log_path=Path("/tmp/fake.log"),
                event_callback=lambda e: None,
                project_path=Path("/tmp/project"),
            )
        assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY" not in captured_env


class TestClaudeCodeSummarizeToolResult:
    def _agent(self):
        return ClaudeCodeAgent(config=AgentConfig())

    def test_dict_with_stdout(self):
        assert self._agent()._summarize_tool_result({"stdout": "output"}) == "output"

    def test_dict_with_stderr(self):
        assert self._agent()._summarize_tool_result({"stderr": "err"}) == "err"

    def test_dict_with_filepath(self):
        result = self._agent()._summarize_tool_result({"filePath": "/foo/bar.py"})
        assert "bar.py" in result

    def test_dict_with_file_object(self):
        result = self._agent()._summarize_tool_result({
            "file": {"filePath": "main.py", "totalLines": 100},
        })
        assert "main.py" in result
        assert "100" in result


class TestCodexHandleLine:
    def _agent(self):
        return CodexAgent(config=AgentConfig(type="codex"))

    def test_build_command_without_time_hook(self, monkeypatch):
        monkeypatch.delenv("AUTOHELIX_CODEX_TIME_LEFT_HOOK", raising=False)
        cmd = self._agent()._build_command("hello")
        assert "--dangerously-bypass-hook-trust" not in cmd
        assert not any("hooks.PostToolUse" in arg for arg in cmd)

    def test_build_command_with_time_hook(self, monkeypatch):
        monkeypatch.setenv("AUTOHELIX_CODEX_TIME_LEFT_HOOK", "/tmp/autohelix hook.sh")
        cmd = self._agent()._build_command("hello")
        assert "--dangerously-bypass-hook-trust" in cmd
        assert any("hooks.PostToolUse" in arg for arg in cmd)
        assert any("hooks.UserPromptSubmit" in arg for arg in cmd)
        assert any('command="/tmp/autohelix hook.sh"' in arg for arg in cmd)

    def test_parse_version(self):
        assert CodexAgent._parse_version("codex-cli 0.141.0") == (0, 141, 0)
        assert CodexAgent._parse_version("codex-cli 0.140.0") == (0, 140, 0)
        assert CodexAgent._parse_version("npm 11.17.0\ncodex-cli 0.141.0") == (0, 141, 0)
        assert CodexAgent._parse_version("unknown") is None

    def test_invalid_json_emits_raw(self):
        events = _collect_events(self._agent(), "garbage")
        assert events[0].kind == AgentEventKind.RAW

    @pytest.mark.parametrize("value", ["quoted text", [1, 2], 3, True, None])
    def test_non_object_json_emits_raw(self, value):
        line = json.dumps(value)
        events = _collect_events(self._agent(), line)
        assert len(events) == 1
        assert events[0].kind == AgentEventKind.RAW
        assert events[0].text == line

    def test_thread_started(self):
        line = json.dumps({"type": "thread.started"})
        events = _collect_events(self._agent(), line)
        assert events[0].kind == AgentEventKind.STATUS

    def test_turn_completed_with_usage(self):
        line = json.dumps({"type": "turn.completed", "usage": {"tokens": 500}})
        events = _collect_events(self._agent(), line)
        assert events[0].kind == AgentEventKind.RESULT

    def test_turn_completed_captures_token_usage(self):
        agent = self._agent()
        line = json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 8000, "output_tokens": 1500},
        })
        _collect_events(agent, line)
        assert agent._usage["input_tokens"] == 8000
        assert agent._usage["output_tokens"] == 1500

    def test_turn_completed_no_usage(self):
        line = json.dumps({"type": "turn.completed"})
        events = _collect_events(self._agent(), line)
        assert events[0].kind == AgentEventKind.RESULT
        assert events[0].text == "turn.completed"

    def test_turn_failed_captures_error(self):
        agent = self._agent()
        line = json.dumps({
            "type": "turn.failed",
            "error": {"message": "credentials expired"},
        })
        events = _collect_events(agent, line)
        assert events[0].kind == AgentEventKind.ERROR
        assert events[0].text == "credentials expired"
        assert agent._turn_error == "credentials expired"

    def test_item_started_command_execution(self):
        line = json.dumps({
            "type": "item.started",
            "item": {"type": "command_execution", "command": "pytest"},
        })
        events = _collect_events(self._agent(), line)
        assert events[0].kind == AgentEventKind.TOOL_USE
        assert events[0].text == "pytest"

    def test_item_completed_command_with_output(self):
        line = json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "ls",
                "aggregated_output": "file1\nfile2",
                "status": "done",
                "exit_code": 0,
            },
        })
        events = _collect_events(self._agent(), line)
        assert events[0].kind == AgentEventKind.TOOL_RESULT
        assert "file1" in events[0].text

    def test_item_completed_command_no_output(self):
        line = json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "rm foo",
                "aggregated_output": "",
                "status": "done",
                "exit_code": 0,
            },
        })
        events = _collect_events(self._agent(), line)
        assert events[0].kind == AgentEventKind.TOOL_RESULT
        assert "rm foo" in events[0].text

    def test_agent_message(self):
        line = json.dumps({
            "type": "item.started",
            "item": {"type": "agent_message", "text": "I'll help you"},
        })
        events = _collect_events(self._agent(), line)
        assert events[0].kind == AgentEventKind.ASSISTANT_TEXT

    def test_unknown_event_type(self):
        line = json.dumps({"type": "something.new"})
        events = _collect_events(self._agent(), line)
        assert events[0].kind == AgentEventKind.STATUS


class TestCodexRunResult:
    def test_turn_failed_fails_even_when_process_exits_zero(
        self, tmp_path, monkeypatch
    ):
        from autohelix.agents.runner import ProcessRunResult

        agent = CodexAgent(config=AgentConfig(type="codex"))

        def fake_run_process(**kwargs):
            kwargs["line_callback"](json.dumps({
                "type": "turn.failed",
                "error": {"message": "credentials expired"},
            }))
            return ProcessRunResult(exit_code=0)

        monkeypatch.setattr(
            "autohelix.agents.codex.run_process",
            fake_run_process,
        )
        result = agent.run(
            worktree_path=tmp_path,
            prompt="test",
            iteration=1,
            log_path=tmp_path / "agent.log",
            event_callback=lambda event: None,
        )

        assert not result.success
        assert result.exit_code == 0
        assert result.error == "credentials expired"


class TestOpenCodeHandleLine:
    def _agent(self):
        return OpenCodeAgent(config=AgentConfig(type="opencode"))

    def test_invalid_json_emits_raw(self):
        events = _collect_events(self._agent(), "not json")
        assert events[0].kind == AgentEventKind.RAW

    def test_step_start(self):
        line = json.dumps({"type": "step_start"})
        events = _collect_events(self._agent(), line)
        assert events[0].kind == AgentEventKind.STATUS

    def test_text_event(self):
        line = json.dumps({"type": "text", "part": {"text": "Analysis complete"}})
        events = _collect_events(self._agent(), line)
        assert events[0].kind == AgentEventKind.ASSISTANT_TEXT
        assert events[0].text == "Analysis complete"

    def test_text_event_empty(self):
        line = json.dumps({"type": "text", "part": {"text": "  "}})
        events = _collect_events(self._agent(), line)
        assert len(events) == 0

    def test_tool_use_event(self):
        line = json.dumps({
            "type": "tool_use",
            "part": {
                "tool": "Bash",
                "state": {
                    "input": {"command": "ls -la"},
                    "output": "total 8\ndrwxr-xr-x",
                },
            },
        })
        events = _collect_events(self._agent(), line)
        assert len(events) == 2
        assert events[0].kind == AgentEventKind.TOOL_USE
        assert "Bash" in events[0].text
        assert "ls -la" in events[0].text
        assert events[1].kind == AgentEventKind.TOOL_RESULT

    def test_tool_use_with_description(self):
        line = json.dumps({
            "type": "tool_use",
            "part": {
                "tool": "Read",
                "state": {
                    "input": {"description": "Reading main.py"},
                    "output": "",
                },
            },
        })
        events = _collect_events(self._agent(), line)
        assert "Reading main.py" in events[0].text

    def test_step_finish(self):
        line = json.dumps({
            "type": "step_finish",
            "part": {"reason": "done", "tokens": {"input": 100, "output": 50}, "cost": 0.0123},
        })
        events = _collect_events(self._agent(), line)
        assert events[0].kind == AgentEventKind.RESULT
        assert "done" in events[0].text
        assert "0.0123" in events[0].text

    def test_step_finish_captures_usage(self):
        agent = self._agent()
        line = json.dumps({
            "type": "step_finish",
            "part": {"reason": "done", "tokens": {"input_tokens": 5000, "output_tokens": 1000}, "cost": 0.05},
        })
        _collect_events(agent, line)
        assert agent._usage["input_tokens"] == 5000
        assert agent._usage["output_tokens"] == 1000
        assert agent._usage["cost_usd"] == 0.05

    def test_step_finish_accumulates_usage(self):
        agent = self._agent()
        line1 = json.dumps({
            "type": "step_finish",
            "part": {"reason": "done", "tokens": {"input_tokens": 3000, "output_tokens": 500}, "cost": 0.03},
        })
        line2 = json.dumps({
            "type": "step_finish",
            "part": {"reason": "done", "tokens": {"input_tokens": 2000, "output_tokens": 300}, "cost": 0.02},
        })
        _collect_events(agent, line1)
        _collect_events(agent, line2)
        assert agent._usage["input_tokens"] == 5000
        assert agent._usage["output_tokens"] == 800
        assert abs(agent._usage["cost_usd"] - 0.05) < 1e-10

    def test_step_finish_no_cost(self):
        line = json.dumps({
            "type": "step_finish",
            "part": {"reason": "end", "tokens": {}},
        })
        events = _collect_events(self._agent(), line)
        assert events[0].kind == AgentEventKind.RESULT

    def test_unknown_type(self):
        line = json.dumps({"type": "new_event_type"})
        events = _collect_events(self._agent(), line)
        assert events[0].kind == AgentEventKind.STATUS
