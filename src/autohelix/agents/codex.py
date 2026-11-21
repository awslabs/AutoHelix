# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Codex CLI backend."""

import json
import os
import re
import subprocess
import time
from pathlib import Path

from autohelix.agents import (
    AgentConfig, AgentEvent, AgentEventKind, AgentRunResult, EventCallback, call_fields, command_path,
    infrastructure_reason,
)
from autohelix.agents.codex_cost import TOKEN_FIELDS, estimate_cost
from autohelix.agents.runner import format_timeout_seconds, run_process
from autohelix.sandbox import build_copy_env


# Codex's only signal for a rejected key: `turn.failed`'s message text.
_AUTH_STATUS = re.compile(r"\bunexpected status (401|403)\b")


class CodexAgent:
    """Run Codex CLI as the AutoHelix agent backend."""

    display_name = "Codex"
    _MIN_HOOK_VERSION = (0, 141, 0)

    def __init__(self, config: AgentConfig, heartbeat_seconds: int = 30):
        self.config = config
        self.heartbeat_seconds = heartbeat_seconds
        # Resolved once, when the backend is created: a variable that is gone is a
        # ConfigError before anything runs. Recorded with every call (`call_fields`).
        self.executable = command_path("codex")
        self._usage: dict[str, object] = {}
        self._turn_error: str | None = None
        self._infrastructure: str | None = None  # from `turn.failed` (`_AUTH_STATUS`)
        self._model: str | None = None  # the model Codex's output names, if it does
        self._completed = False
        self._final_text: str | None = None
        self._schema_path: Path | None = None

    def _command_path(self) -> str:
        return self.executable

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
            return f"'{command}' not found. Set AUTOHELIX_CODEX_CMD."
        except subprocess.TimeoutExpired:
            return f"Could not verify Codex CLI version: '{command} --version' timed out."

        output = f"{result.stdout}\n{result.stderr}".strip()
        version = self._parse_version(output)
        required = self._format_version(self._MIN_HOOK_VERSION)
        if version is None:
            return (
                "Time-left hints for Codex need codex-cli "
                f">= {required}; could not parse version from: {output or '<empty>'}"
            )
        if version < self._MIN_HOOK_VERSION:
            found = self._format_version(version)
            return (
                "Time-left hints for Codex need codex-cli "
                f">= {required}; found {found}. Upgrade Codex to get them."
            )
        return None

    def _build_command(self, prompt: str) -> list[str]:
        cmd = [self._command_path(), "exec", "--json"]
        if self.config.read_only:
            cmd.extend(["--sandbox", "read-only", "--skip-git-repo-check"])
        else:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        if self.config.model:
            cmd.extend(["--model", self.config.model])
        if self.config.effort:
            cmd.extend(["-c", f'model_reasoning_effort="{self.config.effort}"'])
        if self.config.read_only:
            # A judge's instructions are AutoHelix's only: no AGENTS.md from the judged files.
            cmd.extend(["-c", "project_doc_max_bytes=0"])
            if self._schema_path:
                cmd.extend(["--output-schema", str(self._schema_path)])
            # Codex has no flag to append to its system prompt, so the judge's
            # instructions lead the prompt.
            system = self.config.system_prompt
            cmd.append(f"{system}\n\n{prompt}" if system else prompt)
            return cmd
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

        if not isinstance(payload, dict):
            self._emit(event_callback, AgentEventKind.RAW, line)
            return

        event_type = payload.get("type")
        if isinstance(payload.get("model"), str) and payload["model"]:
            self._model = payload["model"]  # unconfirmed: which events name it (if any) varies by version
        if event_type in {"thread.started", "turn.started"}:
            self._emit(event_callback, AgentEventKind.STATUS, event_type)
            return
        if event_type == "turn.failed":
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(
                    error.get("message")
                    or error.get("type")
                    or json.dumps(error, sort_keys=True)
                )
            elif error:
                detail = str(error)
            else:
                detail = "Codex turn failed"
            self._turn_error = detail
            if match := _AUTH_STATUS.search(detail):
                self._infrastructure = infrastructure_reason(int(match.group(1)))
            self._emit(event_callback, AgentEventKind.ERROR, detail)
            return
        if event_type == "turn.completed":
            self._completed = True
            usage = payload.get("usage", {})
            # Completion usage is a cumulative snapshot, not a per-tool delta.
            for key in (*TOKEN_FIELDS, "output_includes_reasoning"):
                self._usage.pop(key, None)
            if isinstance(usage, dict):
                for key in TOKEN_FIELDS:
                    if key in usage:
                        self._usage[key] = usage[key]
                if isinstance(usage.get("output_includes_reasoning"), bool):
                    self._usage["output_includes_reasoning"] = usage["output_includes_reasoning"]
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
                text = str(item.get("text", "")).strip()
                if event_type == "item.completed" and text:
                    self._final_text = text
                self._emit(event_callback, AgentEventKind.ASSISTANT_TEXT, text)
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
        copy_path: Path,
        prompt: str,
        iteration: int,
        log_path: Path,
        event_callback: EventCallback,
        project_path: Path | None = None,
    ) -> AgentRunResult:
        self._usage = {}
        self._turn_error = None
        self._infrastructure = None
        self._model = None
        self._completed = False
        self._final_text = None
        self._schema_path = None
        if self.config.read_only and self.config.output_schema:
            self._schema_path = log_path.with_suffix(".schema.json")
            self._schema_path.write_text(json.dumps(self.config.output_schema))
        started = time.monotonic()

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
                error=f"'{command}' not found. Set AUTOHELIX_CODEX_CMD.",
                usage=call_fields("codex", self.executable, None),
            )

        if result.timed_out:
            event_callback(
                AgentEvent(
                    kind=AgentEventKind.WARNING,
                    text=f"Agent {format_timeout_seconds(self.config.timeout)}",
                )
            )

        error = self._turn_error
        if result.timed_out:
            error = format_timeout_seconds(self.config.timeout)
        elif result.exit_code != 0 and error is None:
            error = f"Codex exited with status {result.exit_code}"

        self._usage["duration_ms"] = int((time.monotonic() - started) * 1000)
        self._usage["usage_complete"] = self._completed and error is None
        # Priced from the model Codex's output names, else agent.model (an estimate needs one).
        model = self._model or self.config.model
        if not self._usage["usage_complete"]:
            self._usage["cost_unavailable_reason"] = "Codex did not report complete successful usage."
        elif not model:
            self._usage["cost_unavailable_reason"] = "set agent.model (Codex's output names no model)"
        else:
            self._usage.update(estimate_cost(
                self._usage, model, output_includes_reasoning=self._usage.get("output_includes_reasoning", True),
            ))
        if self._usage.get("cost_unavailable_reason") and error is None:
            self._emit(
                event_callback, AgentEventKind.WARNING,
                f"Cost unavailable: {self._usage['cost_unavailable_reason']}",
            )

        success = result.exit_code == 0 and error is None
        return AgentRunResult(
            success=success,
            exit_code=result.exit_code,
            error=error,
            usage={**self._usage, **call_fields("codex", self.executable, self._model)},
            final_text=self._final_text,
            infrastructure=None if success or result.timed_out else self._infrastructure,
        )
