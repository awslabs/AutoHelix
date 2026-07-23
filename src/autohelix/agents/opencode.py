# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""OpenCode CLI backend."""

import json
import os
import shutil
from pathlib import Path

from autohelix.agents import AgentConfig, AgentEvent, AgentEventKind, AgentRunResult, EventCallback
from autohelix.agents.runner import format_timeout_seconds, run_process
from autohelix.sandbox import build_worktree_env


class OpenCodeAgent:
    """Run OpenCode as the AutoHelix agent backend."""

    display_name = "OpenCode"

    def __init__(self, config: AgentConfig, heartbeat_seconds: int = 30):
        self.config = config
        self.heartbeat_seconds = heartbeat_seconds
        self._usage: dict[str, object] = {}

    def _command_path(self) -> str:
        if self.config.command:
            return self.config.command

        env_command = os.environ.get("AUTOHELIX_OPENCODE_CMD")
        if env_command:
            return env_command

        resolved = shutil.which("opencode")
        if resolved:
            return resolved

        return "opencode"

    def _build_command(self, prompt: str) -> list[str]:
        cmd = [
            self._command_path(),
            "run",
            "--format", "json",
        ]
        if self.config.model:
            cmd.extend(["--model", self.config.model])
        if self.config.reasoning_effort:
            cmd.extend(["--variant", self.config.reasoning_effort])
        cmd.extend(self.config.extra_args)
        cmd.append(prompt)
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

        event_type = payload.get("type")
        part = payload.get("part", {})

        if event_type == "step_start":
            self._emit(event_callback, AgentEventKind.STATUS, "step started")
            return

        if event_type == "text":
            text = part.get("text", "").strip()
            if text:
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
        worktree_path: Path,
        prompt: str,
        iteration: int,
        log_path: Path,
        event_callback: EventCallback,
        project_path: Path | None = None,
    ) -> AgentRunResult:
        self._usage = {}
        # Build env with remapped PYTHONPATH for worktree
        env = build_worktree_env(project_path, worktree_path) if project_path else None

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
                error=f"'{command}' not found. Set AUTOHELIX_OPENCODE_CMD or agent.command.",
            )

        if result.timed_out:
            event_callback(AgentEvent(kind=AgentEventKind.WARNING, text=f"Agent {format_timeout_seconds(self.config.timeout_seconds)}"))
        return AgentRunResult(success=result.exit_code == 0, exit_code=result.exit_code, usage=self._usage)
