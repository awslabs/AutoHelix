# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Codex CLI backend."""

import json
import os
import re
import subprocess
from pathlib import Path

from autohelix.agents import AgentConfig, AgentEvent, AgentEventKind, AgentRunResult, EventCallback
from autohelix.agents.runner import format_timeout_seconds, run_process
from autohelix.sandbox import build_worktree_env


class CodexAgent:
    """Run Codex CLI as the AutoHelix agent backend."""

    display_name = "Codex"
    _MIN_HOOK_VERSION = (0, 141, 0)

    def __init__(self, config: AgentConfig, heartbeat_seconds: int = 30):
        self.config = config
        self.heartbeat_seconds = heartbeat_seconds
        self._usage: dict[str, object] = {}

    def _command_path(self) -> str:
        return self.config.command or os.environ.get("AUTOHELIX_CODEX_CMD") or "codex"

    @staticmethod
    def _parse_version(text: str) -> tuple[int, int, int] | None:
        match = re.search(r"codex(?:-cli)?\s+(\d+)\.(\d+)\.(\d+)", text, re.IGNORECASE)
        if not match:
            match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
        if not match:
            return None
        return tuple(int(part) for part in match.groups())

    @classmethod
    def _format_version(cls, version: tuple[int, int, int]) -> str:
        return ".".join(str(part) for part in version)

    def _time_left_hook_path(self) -> str | None:
        return os.environ.get("AUTOHELIX_CODEX_TIME_LEFT_HOOK") or None

    def _time_left_hook_args(self, hook_path: str) -> list[str]:
        command = json.dumps(hook_path)
        hook = f'{{type="command",command={command},timeout=5}}'
        return [
            "--dangerously-bypass-hook-trust",
            "-c",
            f'hooks.PostToolUse=[{{matcher="",hooks=[{hook}]}}]',
            "-c",
            f"hooks.UserPromptSubmit=[{{hooks=[{hook}]}}]",
        ]

    def _validate_time_left_hook_support(self, hook_path: str) -> str | None:
        if not Path(hook_path).exists():
            return f"Codex time-left hook not found: {hook_path}"

        command = self._command_path()
        try:
            result = subprocess.run(
                [command, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except FileNotFoundError:
            return f"'{command}' not found. Set AUTOHELIX_CODEX_CMD or agent.command."
        except subprocess.TimeoutExpired:
            return f"Could not verify Codex CLI version: '{command} --version' timed out."

        output = f"{result.stdout}\n{result.stderr}".strip()
        version = self._parse_version(output)
        required = self._format_version(self._MIN_HOOK_VERSION)
        if version is None:
            return (
                "budget.iteration_time for Codex requires codex-cli "
                f">= {required}; could not parse version from: {output or '<empty>'}"
            )
        if version < self._MIN_HOOK_VERSION:
            found = self._format_version(version)
            return (
                "budget.iteration_time for Codex requires codex-cli "
                f">= {required}; found {found}. Upgrade Codex or remove budget.iteration_time."
            )
        return None

    def _build_command(self, prompt: str) -> list[str]:
        cmd = [
            self._command_path(),
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        if self.config.model:
            cmd.extend(["--model", self.config.model])
        if self.config.reasoning_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{self.config.reasoning_effort}"'])
        for key, value in self.config.settings.items():
            cmd.extend(["-c", f"{key}={json.dumps(value)}"])
        time_left_hook = self._time_left_hook_path()
        if time_left_hook:
            cmd.extend(self._time_left_hook_args(time_left_hook))
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
        if event_type in {"thread.started", "turn.started"}:
            self._emit(event_callback, AgentEventKind.STATUS, event_type)
            return
        if event_type == "turn.completed":
            usage = payload.get("usage", {})
            if isinstance(usage, dict):
                if usage.get("input_tokens"):
                    self._usage["input_tokens"] = usage["input_tokens"]
                if usage.get("output_tokens"):
                    self._usage["output_tokens"] = usage["output_tokens"]
            if usage:
                self._emit(event_callback, AgentEventKind.RESULT, json.dumps(usage, sort_keys=True))
            else:
                self._emit(event_callback, AgentEventKind.RESULT, "turn.completed")
            return
        if event_type in {"item.started", "item.completed"}:
            item = payload.get("item", {})
            if not isinstance(item, dict):
                return
            item_type = item.get("type")
            if item_type == "agent_message":
                self._emit(event_callback, AgentEventKind.ASSISTANT_TEXT, str(item.get("text", "")).strip())
                return
            if item_type == "command_execution":
                command = str(item.get("command", "")).strip()
                if event_type == "item.started":
                    self._emit(event_callback, AgentEventKind.TOOL_USE, command)
                    return
                output = str(item.get("aggregated_output", "")).strip()
                status = str(item.get("status", "")).strip()
                exit_code = item.get("exit_code")
                if output:
                    self._emit(event_callback, AgentEventKind.TOOL_RESULT, output, exit_code=exit_code, status=status)
                else:
                    self._emit(
                        event_callback,
                        AgentEventKind.TOOL_RESULT,
                        f"{command} exited with status={status} exit_code={exit_code}",
                    )
                return
            self._emit(event_callback, AgentEventKind.STATUS, f"{event_type}:{item_type}")
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
                error=f"'{command}' not found. Set AUTOHELIX_CODEX_CMD or agent.command.",
            )

        if result.timed_out:
            event_callback(
                AgentEvent(
                    kind=AgentEventKind.WARNING,
                    text=f"Agent {format_timeout_seconds(self.config.timeout_seconds)}",
                )
            )
        return AgentRunResult(success=result.exit_code == 0, exit_code=result.exit_code, usage=self._usage)
