# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""OpenCode CLI backend."""

import json
from pathlib import Path

from autohelix.agents import (
    AgentConfig, AgentEvent, AgentEventKind, AgentRunResult, EventCallback, call_fields, command_path,
    infrastructure_reason,
)
from autohelix.agents.runner import format_timeout_seconds, run_process
from autohelix.sandbox import build_copy_env


class OpenCodeAgent:
    """Run OpenCode as the AutoHelix agent backend."""

    display_name = "OpenCode"

    def __init__(self, config: AgentConfig, heartbeat_seconds: int = 30):
        self.config = config
        self.heartbeat_seconds = heartbeat_seconds
        # Resolved once, when the backend is created: a variable that is gone is a
        # ConfigError before anything runs. Recorded with every call (`call_fields`).
        self.executable = command_path("opencode")
        self._usage: dict[str, object] = {}
        self._final_text: str | None = None
        # The last `error` event's message and `infrastructure_reason`: OpenCode can exit 0
        # after one (an invalid key), so an error event fails the call whatever the exit code.
        self._error: str | None = None
        self._infrastructure: str | None = None

    def _command_path(self) -> str:
        return self.executable

    def _build_command(self, prompt: str) -> list[str]:
        cmd = [
            self._command_path(),
            "run",
            "--format", "json",
        ]
        if self.config.model:
            cmd.extend(["--model", self.config.model])
        if self.config.effort:
            cmd.extend(["--variant", self.config.effort])
        cmd.extend(self.config.extra_args)
        # `--` (yargs: what follows is the `message` positional): a flag in extra_args
        # that takes several values (`--file`) doesn't take the prompt.
        cmd.extend(["--", prompt])
        return cmd

    def _emit(self, event_callback: EventCallback, kind: AgentEventKind, text: str, **metadata: object) -> None:
        if text:
            event_callback(AgentEvent(kind=kind, text=text, metadata=metadata))

    def _handle_line(self, line: str, event_callback: EventCallback) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            self._emit(event_callback, AgentEventKind.RAW, line)
            return
        if not isinstance(payload, dict):  # `0`, `[1]` on stderr (a wrapper's): output, not an event
            self._emit(event_callback, AgentEventKind.RAW, line)
            return

        event_type = payload.get("type")
        part = payload.get("part", {})

        if event_type == "step_start":
            self._emit(event_callback, AgentEventKind.STATUS, "step started")
            return

        if event_type == "text":
            text = part.get("text", "").strip()
            if text:
                self._final_text = text
                self._emit(event_callback, AgentEventKind.ASSISTANT_TEXT, text)
            return

        if event_type == "tool_use":
            tool = part.get("tool", "tool")
            state = part.get("state", {})
            tool_input = state.get("input", {})

            title = tool_input.get("description") or tool_input.get("command") or tool
            self._emit(event_callback, AgentEventKind.TOOL_USE, f"{tool}: {title}")

            output = state.get("output", "").strip()
            if output:
                self._emit(event_callback, AgentEventKind.TOOL_RESULT, output)
            return

        if event_type == "error":
            error = payload.get("error")
            error = error if isinstance(error, dict) else {}
            data = error.get("data") if isinstance(error.get("data"), dict) else {}
            message = data.get("message") or error.get("message") or error.get("name") or "OpenCode reported an error"
            self._error = str(message)
            # 401/403 seen for real (an invalid key); 429/5xx not yet: an error event is
            # assumed to come after OpenCode's own retries (`isRetryable` aside).
            self._infrastructure = infrastructure_reason(data.get("statusCode"))
            self._emit(event_callback, AgentEventKind.ERROR, self._error)
            return

        if event_type == "step_finish":
            reason = part.get("reason", "unknown")
            tokens = part.get("tokens", {})
            cost = part.get("cost")
            if isinstance(tokens, dict):
                for key in ("input_tokens", "output_tokens"):
                    if tokens.get(key):
                        self._usage[key] = self._usage.get(key, 0) + tokens[key]
            if cost is not None:
                self._usage["cost_usd"] = self._usage.get("cost_usd", 0) + cost
                self._usage["cost_source"] = "backend"
            detail = f"reason={reason}"
            if tokens:
                detail += f" tokens={json.dumps(tokens, sort_keys=True)}"
            if cost is not None:
                detail += f" cost=${cost:.4f}"
            self._emit(event_callback, AgentEventKind.RESULT, detail)
            return

        self._emit(event_callback, AgentEventKind.STATUS, event_type or "event")

    def run(
        self,
        copy_path: Path,
        prompt: str,
        iteration: int,
        log_path: Path,
        event_callback: EventCallback,
        project_path: Path | None = None,
    ) -> AgentRunResult:
        self._usage = {}
        self._final_text = None
        self._error = None
        self._infrastructure = None
        # Build env with remapped PYTHONPATH for the copy
        env = build_copy_env(project_path, copy_path) if project_path else None

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
                error=f"'{command}' not found. Set AUTOHELIX_OPENCODE_CMD.",
                usage=call_fields("opencode", self.executable, None),
            )

        error = None
        if result.timed_out:
            error = format_timeout_seconds(self.config.timeout)
            event_callback(AgentEvent(kind=AgentEventKind.WARNING, text=f"Agent {error}"))
            # Its finished steps' costs only: the call's cost is unknown (kept apart).
            if "cost_usd" in self._usage:
                self._usage["partial_cost_usd"] = self._usage.pop("cost_usd")
                self._usage.pop("cost_source", None)
            self._usage["cost_unavailable_reason"] = "OpenCode timed out before reporting its whole cost."
        elif self._error is not None:
            error = self._error
        elif result.exit_code != 0:
            error = f"OpenCode exited with status {result.exit_code}"
        infrastructure = self._infrastructure if self._error is not None and not result.timed_out else None
        # OpenCode's JSON events, as parsed here, name no model (unconfirmed): None.
        return AgentRunResult(success=error is None, exit_code=result.exit_code, error=error,
                              usage={**self._usage, **call_fields("opencode", self.executable, None)},
                              final_text=self._final_text, infrastructure=infrastructure)
