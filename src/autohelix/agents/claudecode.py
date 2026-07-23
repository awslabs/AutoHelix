# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Claude Code backend."""

import json
import os
import shutil
from pathlib import Path

from autohelix.agents import AgentConfig, AgentEvent, AgentEventKind, AgentRunResult, EventCallback
from autohelix.agents.runner import format_timeout_seconds, run_process
from autohelix.sandbox import build_worktree_env


class ClaudeCodeAgent:
    """Run Claude Code as the AutoHelix agent backend."""

    display_name = "Claude Code"

    def __init__(self, config: AgentConfig, heartbeat_seconds: int = 30):
        self.config = config
        self.heartbeat_seconds = heartbeat_seconds
        self._tool_names: dict[int, str] = {}
        self._tool_inputs: dict[int, str] = {}
        self._active_kind: AgentEventKind | None = None
        self._active_text = ""
        self._active_metadata: dict[str, object] = {}
        self._usage: dict[str, object] = {}
        self._incremental_input: int = 0
        self._incremental_output: int = 0

    def _command_path(self) -> str:
        if self.config.command:
            return self.config.command

        env_command = os.environ.get("AUTOHELIX_CLAUDE_CMD")
        if env_command:
            return env_command

        resolved_claude = shutil.which("claude")
        if resolved_claude:
            return resolved_claude

        return "claude"

    def _build_command(self, prompt: str) -> list[str]:
        cmd = [
            self._command_path(),
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--dangerously-skip-permissions",
        ]
        if self.config.model:
            cmd.extend(["--model", self.config.model])
        if self.config.reasoning_effort:
            cmd.extend(["--effort", self.config.reasoning_effort])
        # If autohelix has prepared a settings file with our hooks (e.g. the
        # time_left injector), load it via --settings without touching the user
        # ~/.claude/settings.json.
        autohelix_settings = os.environ.get("AUTOHELIX_CLAUDE_SETTINGS")
        if autohelix_settings and os.path.exists(autohelix_settings):
            cmd.extend(["--settings", autohelix_settings])
        cmd.extend(self.config.extra_args)
        cmd.append(prompt)
        return cmd

    def _emit_stream(
        self,
        event_callback: EventCallback,
        kind: AgentEventKind,
        text: str,
        **metadata: object,
    ) -> None:
        self._active_kind = kind
        self._active_text = text
        self._active_metadata = dict(metadata)
        stream_metadata = {"stream": True, **metadata}
        event_callback(AgentEvent(kind=kind, text=text, metadata=stream_metadata))

    def _flush_stream(self, event_callback: EventCallback) -> None:
        if self._active_kind is None or not self._active_text:
            self._active_kind = None
            self._active_text = ""
            self._active_metadata = {}
            return
        final_metadata = {"final": True, **self._active_metadata}
        event_callback(AgentEvent(kind=self._active_kind, text=self._active_text, metadata=final_metadata))
        self._active_kind = None
        self._active_text = ""
        self._active_metadata = {}

    def _emit(self, event_callback: EventCallback, kind: AgentEventKind, text: str, **metadata: object) -> None:
        if text:
            event_callback(AgentEvent(kind=kind, text=text, metadata=metadata))

    def _handle_line(self, line: str, event_callback: EventCallback) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            self._emit(event_callback, AgentEventKind.RAW, line)
            return

        event_type = payload.get("type")

        if event_type == "system":
            subtype = payload.get("subtype", "event")
            model = payload.get("model")
            detail = f" model={model}" if model else ""
            self._emit(event_callback, AgentEventKind.STATUS, f"{subtype}{detail}")
            return

        if event_type == "stream_event":
            event = payload.get("event", {})
            stream_type = event.get("type")
            if stream_type == "content_block_start":
                content_block = event.get("content_block", {})
                block_type = content_block.get("type")
                if block_type == "tool_use":
                    tool_name = content_block.get("name", "Tool")
                    index = event.get("index")
                    if isinstance(index, int):
                        self._tool_names[index] = tool_name
                        self._tool_inputs[index] = ""
                    self._emit(event_callback, AgentEventKind.TOOL_USE, tool_name)
            elif stream_type == "content_block_delta":
                delta = event.get("delta", {})
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        current = self._active_text + text if self._active_kind == AgentEventKind.ASSISTANT_TEXT else text
                        self._emit_stream(event_callback, AgentEventKind.ASSISTANT_TEXT, current)
                elif delta_type == "thinking_delta":
                    text = delta.get("thinking", "")
                    if text:
                        current = self._active_text + text if self._active_kind == AgentEventKind.THINKING else text
                        self._emit_stream(event_callback, AgentEventKind.THINKING, current)
                elif delta_type == "input_json_delta":
                    index = event.get("index")
                    if isinstance(index, int):
                        tool_name = self._tool_names.get(index, "tool")
                        current = self._tool_inputs.get(index, "") + str(delta.get("partial_json", ""))
                        self._tool_inputs[index] = current
                        self._emit_stream(event_callback, AgentEventKind.TOOL_INPUT, current, tool_name=tool_name)
                elif delta_type == "signature_delta":
                    signature = delta.get("signature", "")
                    if signature:
                        self._emit(event_callback, AgentEventKind.STATUS, f"signature {len(signature)} chars")
            elif stream_type in {"content_block_stop", "message_stop"}:
                self._flush_stream(event_callback)
            elif stream_type == "message_start":
                message = event.get("message", {})
                msg_usage = message.get("usage", {})
                input_tokens = msg_usage.get("input_tokens", 0)
                if input_tokens:
                    self._incremental_input += input_tokens
            elif stream_type == "message_delta":
                self._flush_stream(event_callback)
                event_usage = event.get("usage", {})
                output_tokens = event_usage.get("output_tokens", 0)
                if output_tokens:
                    self._incremental_output += output_tokens
                delta = event.get("delta", {})
                stop_reason = delta.get("stop_reason")
                if stop_reason:
                    self._emit(event_callback, AgentEventKind.STATUS, f"stop_reason={stop_reason}")
            return

        if event_type == "assistant":
            message = payload.get("message", {})
            if isinstance(message, dict):
                for block in message.get("content", []):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        name = block.get("name", "Tool")
                        tool_input = block.get("input", {})
                        text = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
                        self._emit(event_callback, AgentEventKind.TOOL_INPUT, text, tool_name=name)
            return

        if event_type == "user":
            message = payload.get("message", {})
            if isinstance(message, dict):
                for block in message.get("content", []):
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    text = str(block.get("content") or "").strip()
                    if text:
                        self._emit(
                            event_callback,
                            AgentEventKind.TOOL_RESULT,
                            text,
                            is_error=bool(block.get("is_error")),
                        )
                    return

            tool_result = payload.get("tool_use_result")
            if isinstance(tool_result, str):
                self._emit(event_callback, AgentEventKind.TOOL_RESULT, tool_result.strip())
                return
            if isinstance(tool_result, dict):
                summary = self._summarize_tool_result(tool_result)
                self._emit(event_callback, AgentEventKind.TOOL_RESULT, summary)
            return

        if event_type == "result":
            subtype = payload.get("subtype", "unknown")
            duration_ms = payload.get("duration_ms")
            duration_text = f" in {duration_ms}ms" if duration_ms is not None else ""
            # Extract usage/cost data
            cost_usd = payload.get("total_cost_usd")
            usage = payload.get("usage", {})
            if isinstance(usage, dict):
                if usage.get("input_tokens"):
                    self._usage["input_tokens"] = usage["input_tokens"]
                if usage.get("output_tokens"):
                    self._usage["output_tokens"] = usage["output_tokens"]
            if cost_usd is not None:
                self._usage["cost_usd"] = cost_usd
            if duration_ms is not None:
                self._usage["duration_ms"] = duration_ms
            cost_text = f" ${cost_usd:.4f}" if cost_usd is not None else ""
            self._emit(event_callback, AgentEventKind.RESULT, f"{subtype}{duration_text}{cost_text}")

    def _summarize_tool_result(self, tool_result: dict) -> str:
        stdout = str(tool_result.get("stdout") or "").strip()
        stderr = str(tool_result.get("stderr") or "").strip()
        if stdout:
            return stdout
        if stderr:
            return stderr
        file_path = tool_result.get("filePath")
        if file_path:
            return f"updated {file_path}"
        file_obj = tool_result.get("file")
        if isinstance(file_obj, dict):
            nested_path = file_obj.get("filePath", "file")
            total_lines = file_obj.get("totalLines")
            suffix = f" ({total_lines} lines)" if total_lines else ""
            return f"read {nested_path}{suffix}"
        return json.dumps(tool_result, ensure_ascii=False, sort_keys=True)

    def run(
        self,
        worktree_path: Path,
        prompt: str,
        iteration: int,
        log_path: Path,
        event_callback: EventCallback,
        project_path: Path | None = None,
    ) -> AgentRunResult:
        self._tool_names.clear()
        self._tool_inputs.clear()
        self._active_kind = None
        self._active_text = ""
        self._active_metadata = {}
        self._usage = {}
        self._incremental_input = 0
        self._incremental_output = 0

        # Build env with remapped PYTHONPATH for worktree
        env = build_worktree_env(project_path, worktree_path) if project_path else None
        if not self.config.auto_memory:
            env = env or os.environ.copy()
            env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"

        try:
            result = run_process(
                cmd=self._build_command(prompt),
                cwd=worktree_path,
                log_path=log_path,
                line_callback=lambda line: self._handle_line(line, event_callback),
                heartbeat_seconds=self.heartbeat_seconds,
                timeout_seconds=self.config.timeout_seconds,
                env=env,
            )
        except FileNotFoundError:
            command = self._command_path()
            return AgentRunResult(
                success=False,
                exit_code=None,
                error=f"'{command}' not found. Set AUTOHELIX_CLAUDE_CMD or agent.command.",
            )

        if result.timed_out:
            event_callback(
                AgentEvent(
                    kind=AgentEventKind.WARNING,
                    text=f"Agent {format_timeout_seconds(self.config.timeout_seconds)}",
                )
            )

        if not self._usage.get("input_tokens") and self._incremental_input:
            self._usage["input_tokens"] = self._incremental_input
        if not self._usage.get("output_tokens") and self._incremental_output:
            self._usage["output_tokens"] = self._incremental_output

        return AgentRunResult(success=result.exit_code == 0, exit_code=result.exit_code, usage=self._usage)
