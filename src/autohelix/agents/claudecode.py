# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Claude Code backend."""

import json
import os
from pathlib import Path

from autohelix.agents import (
    AgentConfig, AgentEvent, AgentEventKind, AgentRunResult, EventCallback, call_fields, command_path,
    infrastructure_reason,
)
from autohelix.agents.runner import format_timeout_seconds, run_process
from autohelix.sandbox import build_copy_env


def split_settings(args: list[str]) -> tuple[str | None, list[str]]:
    """`extra_args`' `--settings X` / `--settings=X` (the last one: Claude Code uses only
    the last, as a whole) and the other arguments."""
    value, rest, args = None, [], list(args)
    while args:
        arg = args.pop(0)
        if arg == "--settings":
            if not args:
                from autohelix.config import ConfigError  # config imports the agents package

                raise ConfigError("agent.extra_args: --settings has no value.")
            value = args.pop(0)
        elif arg.startswith("--settings="):
            value = arg.removeprefix("--settings=")
        else:
            rest.append(arg)
    return value, rest


def merged_settings(value: str | None, hooks: dict[str, list], base: Path) -> dict:
    """Claude Code settings: the user's `--settings` value (a JSON string, or a file,
    relative to `base`), with AutoHelix's `hooks` added to the user's hook lists."""
    from autohelix.config import ConfigError  # config imports the agents package

    settings: object = {}
    if value is not None:
        problem = f"agent.extra_args: --settings {value} can't be read"
        try:
            text = value if value.lstrip().startswith("{") else (base / Path(value).expanduser()).read_text()
            settings = json.loads(text)
        except (OSError, UnicodeDecodeError, ValueError) as e:
            raise ConfigError(f"{problem}: {e}.") from None
        if not isinstance(settings, dict):
            raise ConfigError(f"{problem}: it isn't a JSON object.")
    merged = dict(settings)
    own = merged.get("hooks", {})
    if not isinstance(own, dict) or not all(isinstance(entries, list) for entries in own.values()):
        raise ConfigError(f"agent.extra_args: --settings {value}: its hooks aren't an object of lists.")
    if hooks:
        merged["hooks"] = {**own, **{event: [*own.get(event, []), *entries] for event, entries in hooks.items()}}
    return merged


def _count(value: object) -> bool:
    """Whether a result field is a number of tokens or milliseconds."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


class ClaudeCodeAgent:
    """Run Claude Code as the AutoHelix agent backend."""

    display_name = "Claude Code"

    def __init__(self, config: AgentConfig, heartbeat_seconds: int = 30):
        self.config = config
        self.heartbeat_seconds = heartbeat_seconds
        # Resolved once, when the backend is created: a variable that is gone is a
        # ConfigError before anything runs. Recorded with every call (`call_fields`).
        self.executable = command_path("claude-code")
        self._tool_names: dict[int, str] = {}
        self._tool_inputs: dict[int, str] = {}
        self._active_kind: AgentEventKind | None = None
        self._active_text = ""
        self._active_metadata: dict[str, object] = {}
        self._usage: dict[str, object] = {}
        self._incremental_input: int = 0
        self._incremental_output: int = 0
        self._result_error: str | None = None
        self._infrastructure: str | None = None  # the last `api_error` result's (`infrastructure_reason`)
        self._final_text: str | None = None
        self._structured_output = None
        self._model: str | None = None  # the model the `init` event names

    def _command_path(self) -> str:
        return self.executable

    def _build_command(self, prompt: str) -> list[str]:
        cmd = [
            self._command_path(),
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
        ]
        if self.config.read_only:
            # Read-only tools only; no project settings or hooks, no MCP servers.
            cmd.extend(["--tools", "Read,Grep,Glob", "--setting-sources", "user", "--strict-mcp-config"])
        else:
            cmd.append("--dangerously-skip-permissions")
        if self.config.model:
            cmd.extend(["--model", self.config.model])
        if self.config.effort:
            cmd.extend(["--effort", self.config.effort])
        if self.config.system_prompt:
            cmd.extend(["--append-system-prompt", self.config.system_prompt])
        if self.config.output_schema:
            cmd.extend(["--json-schema", json.dumps(self.config.output_schema)])
        if self.config.read_only:
            cmd.extend(["--", prompt])
            return cmd
        # If autohelix has prepared a settings file with our hooks (e.g. the
        # time_left injector), load it via --settings without touching the user
        # ~/.claude/settings.json.
        autohelix_settings = os.environ.get("AUTOHELIX_CLAUDE_SETTINGS")
        if autohelix_settings and os.path.exists(autohelix_settings):
            cmd.extend(["--settings", autohelix_settings])
        # A pending wakeup keeps `claude -p` alive after its answer (up to 15 minutes).
        # Separate --disallowedTools flags combine: a user's own in extra_args still applies.
        cmd.extend(["--disallowedTools", "ScheduleWakeup"])
        cmd.extend(self.config.extra_args)
        # `--`: a flag in extra_args that takes several values doesn't take the prompt.
        cmd.extend(["--", prompt])
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

    def _handle_line(self, line: str, event_callback: EventCallback, *, replay: bool = False) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            self._emit(event_callback, AgentEventKind.RAW, line)
            return

        event_type = payload.get("type")

        if event_type == "system":
            subtype = payload.get("subtype", "event")
            model = payload.get("model")
            if subtype == "init" and isinstance(model, str) and model:
                self._model = model
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
                    if replay and block.get("type") in {"text", "thinking"}:
                        kind = AgentEventKind.ASSISTANT_TEXT if block["type"] == "text" else AgentEventKind.THINKING
                        self._emit(event_callback, kind, str(block.get(block["type"]) or ""))
                    if block.get("type") == "tool_use":
                        name = block.get("name", "Tool")
                        if replay:
                            self._emit(event_callback, AgentEventKind.TOOL_USE, name)
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
            if isinstance(payload.get("result"), str):
                self._final_text = payload["result"]
            self._structured_output = payload.get("structured_output")
            duration_ms = payload.get("duration_ms")
            duration_text = f" in {duration_ms}ms" if duration_ms is not None else ""
            if payload.get("is_error"):
                self._result_error = str(
                    payload.get("result") or f"Claude Code returned {subtype}"
                )
                # Credentials or the model service (401/403, 429/5xx), as the CLI reports
                # them; an earlier `completed` result doesn't change it: the call failed.
                # The last failed result decides.
                self._infrastructure = None
                if payload.get("terminal_reason") == "api_error":
                    self._infrastructure = infrastructure_reason(payload.get("api_error_status"))
                self._emit(
                    event_callback,
                    AgentEventKind.ERROR,
                    self._result_error,
                )
            # A session can end several turns (a background task's continuation): each
            # result's tokens and duration are its turn's own, summed; its total_cost_usd
            # is the session's, so the last one counts.
            cost_usd = payload.get("total_cost_usd")
            usage = payload.get("usage", {})
            if isinstance(usage, dict):
                for key in ("input_tokens", "output_tokens"):
                    if _count(usage.get(key)) and usage[key] > 0:
                        self._usage[key] = self._usage.get(key, 0) + usage[key]
            if cost_usd is not None:
                self._usage["cost_usd"] = cost_usd
                self._usage["cost_source"] = "backend"
            if _count(duration_ms):
                self._usage["duration_ms"] = self._usage.get("duration_ms", 0) + duration_ms
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
        copy_path: Path,
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
        self._result_error = None
        self._infrastructure = None
        self._final_text = None
        self._structured_output = None
        self._model = None

        # Build env with remapped PYTHONPATH for the copy
        env = build_copy_env(project_path, copy_path) if project_path else None
        # auto_memory decides, whatever the harness's own environment says.
        env = env or os.environ.copy()
        if self.config.auto_memory:
            env.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)
        else:
            env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        if self.config.timeout is not None:
            # `claude -p` stops background tasks 10 minutes after the last turn; with a
            # deadline (which kills the whole process group) wait for them instead.
            env.setdefault("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "0")

        try:
            result = run_process(
                cmd=self._build_command(prompt),
                cwd=copy_path,
                log_path=log_path,
                line_callback=lambda line: self._handle_line(line, event_callback),
                heartbeat_seconds=self.heartbeat_seconds,
                timeout_seconds=self.config.timeout,
                env=env,
            )
        except FileNotFoundError:
            command = self._command_path()
            return AgentRunResult(
                success=False,
                exit_code=None,
                error=f"'{command}' not found. Set AUTOHELIX_CLAUDE_CODE_CMD.",
                usage=call_fields("claude-code", self.executable, None),
            )

        if result.timed_out:
            event_callback(
                AgentEvent(
                    kind=AgentEventKind.WARNING,
                    text=f"Agent {format_timeout_seconds(self.config.timeout)}",
                )
            )

        if not self._usage.get("input_tokens") and self._incremental_input:
            self._usage["input_tokens"] = self._incremental_input
        if not self._usage.get("output_tokens") and self._incremental_output:
            self._usage["output_tokens"] = self._incremental_output

        error = self._result_error
        if result.timed_out:
            error = format_timeout_seconds(self.config.timeout)
        elif result.exit_code != 0 and error is None:
            error = f"Claude Code exited with status {result.exit_code}"

        success = result.exit_code == 0 and error is None
        return AgentRunResult(
            success=success,
            exit_code=result.exit_code,
            error=error,
            usage={**self._usage, **call_fields("claude-code", self.executable, self._model)},
            final_text=self._final_text,
            structured_output=self._structured_output,
            infrastructure=None if success or result.timed_out else self._infrastructure,
        )
