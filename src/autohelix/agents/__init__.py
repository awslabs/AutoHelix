# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Agent backends for AutoHelix."""

import os
import shutil
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Protocol


class AgentEventKind(StrEnum):
    """Normalized event kinds emitted by agent backends."""

    STATUS = "status"
    THINKING = "thinking"
    ASSISTANT_TEXT = "assistant_text"
    TOOL_USE = "tool_use"
    TOOL_INPUT = "tool_input"
    TOOL_RESULT = "tool_result"
    WARNING = "warning"
    ERROR = "error"
    RAW = "raw"
    RESULT = "result"


@dataclass
class RequireNotesConfig:
    """Enforce that the agent writes iteration notes before finishing (claude only).

    Implemented via a Stop hook that blocks the agent from ending its turn until
    the iteration's notes file has at least `min_chars` non-whitespace characters,
    re-prompting up to `max_retries` times before giving up.
    """

    min_chars: int = 50
    max_retries: int = 3


# The keys of `agent:`. Renamed keys are errors that name their replacement.
AGENT_KEYS = ("backend", "model", "reasoning_effort", "timeout", "command", "settings", "extra_args",
              "auto_memory", "require_notes")
RENAMED_AGENT_KEYS = {
    "type": "`agent.type` is now `agent.backend`",
    "timeout_seconds": "`agent.timeout_seconds` is now `agent.timeout` (a duration such as 30m)",
}


def _require_notes(value: Any, errors: list[str]) -> RequireNotesConfig | None:
    """`require_notes:`: true for the defaults, a mapping to override them, false or absent for off."""
    if value is None or value is False:
        return None
    if value is True:
        return RequireNotesConfig()
    if not isinstance(value, dict):
        errors.append(f"agent.require_notes must be true, false or a mapping, got {value!r}")
        return None
    config = RequireNotesConfig()
    for key in value:
        if key not in ("min_chars", "max_retries"):
            errors.append(f"Unknown key agent.require_notes.{key} (known: min_chars, max_retries)")
    for key in ("min_chars", "max_retries"):
        if key in value:
            if type(value[key]) is not int:
                errors.append(f"agent.require_notes.{key} must be an integer, got {value[key]!r}")
            else:
                setattr(config, key, value[key])
    return config


@dataclass
class AgentConfig:
    """Configuration for an agent backend."""

    backend: str = "claude"
    command: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    timeout: int | None = None  # seconds: the hard kill, and time-left hints where supported
    settings: dict[str, Any] = field(default_factory=dict)
    extra_args: list[str] = field(default_factory=list)
    auto_memory: bool = False  # Disabled by default; set true to enable Claude Code's built-in memory
    require_notes: RequireNotesConfig | None = None
    # Judge mode (`autohelix ask`), set in code, never from YAML: no edit tools, no
    # project settings/hooks/MCP, no extra_args or settings pass-through.
    read_only: bool = False
    system_prompt: str | None = None  # appended to the backend's system prompt
    output_schema: dict[str, Any] | None = None  # JSON Schema for the final answer

    @classmethod
    def parse(cls, data: Any) -> tuple["AgentConfig", list[str]]:
        """Build an agent config from YAML data; returns it with the errors found."""
        from autohelix.config import parse_duration

        errors: list[str] = []
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return cls(), ["`agent:` must be a mapping"]
        for key in data:
            if key in RENAMED_AGENT_KEYS:
                errors.append(RENAMED_AGENT_KEYS[key])
            elif key not in AGENT_KEYS:
                errors.append(f"Unknown key agent.{key} (known: {', '.join(AGENT_KEYS)})")

        settings = data.get("settings") or {}
        if not isinstance(settings, dict):
            errors.append("agent.settings must be a mapping")
            settings = {}
        if "model" in settings:
            errors.append("`agent.settings.model` is now `agent.model`")

        extra_args = data.get("extra_args", [])
        if isinstance(extra_args, str):
            extra_args = [extra_args]
        elif not isinstance(extra_args, list):
            errors.append("agent.extra_args must be a list of arguments")
            extra_args = []

        timeout = None
        if data.get("timeout") is not None:
            try:
                timeout = parse_duration(data["timeout"])
            except (ValueError, OverflowError) as e:
                errors.append(f"agent.timeout: {data['timeout']!r} is not a valid duration ({e})")

        backend = agent_string(data, "backend", "agent", errors) or "claude"
        auto_memory = data.get("auto_memory", False)
        if not isinstance(auto_memory, bool):  # `"false"` would be true
            errors.append(f"agent.auto_memory must be true or false, got {auto_memory!r}")
            auto_memory = False

        return cls(
            backend=backend.lower(),
            command=agent_string(data, "command", "agent", errors),
            model=agent_string(data, "model", "agent", errors),
            reasoning_effort=agent_string(data, "reasoning_effort", "agent", errors),
            timeout=timeout,
            settings=settings,
            extra_args=[str(arg) for arg in extra_args],
            auto_memory=auto_memory,
            require_notes=_require_notes(data.get("require_notes"), errors),
        ), errors

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AgentConfig":
        """Build an agent config from YAML data; raises ValueError if it is malformed."""
        config, errors = cls.parse(data)
        if errors:
            raise ValueError("; ".join(errors))
        return config


@dataclass
class AgentEvent:
    """A normalized backend event suitable for display."""

    kind: AgentEventKind | str
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentRunResult:
    """Result from running an agent backend."""

    success: bool
    exit_code: int | None = None
    error: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    final_text: str | None = None  # the agent's last message
    structured_output: Any = None  # schema-constrained answer, where the backend supports it


EventCallback = Callable[[AgentEvent], None]


class AgentBackend(Protocol):
    """Runtime interface implemented by each backend."""

    display_name: str

    def run(
        self,
        worktree_path: Path,
        prompt: str,
        iteration: int,
        log_path: Path,
        event_callback: EventCallback,
        project_path: Path | None = None,
    ) -> AgentRunResult:
        """Execute the agent inside the given worktree."""


# Every `agent.backend` (`create_agent`), and those an `ask:`/`review:` judge can use
# (a read-only mode; mock for tests).
BACKENDS = ("claude", "codex", "opencode", "mock")
JUDGE_BACKENDS = ("claude", "codex", "mock")


def command_path(backend: str, command: str | None = None) -> str:
    """The executable a backend runs: `agent.command`, else `$AUTOHELIX_<BACKEND>_CMD`,
    else the backend's name found on PATH, else the bare name (which then fails to start).
    The backends and preflight both resolve it here."""
    if command:
        return command
    if env_command := os.environ.get(f"AUTOHELIX_{backend.upper()}_CMD"):
        return env_command
    return shutil.which(backend) or backend


def agent_string(data: dict, key: str, where: str, errors: list[str]) -> str | None:
    """`data[key]` when it is a non-empty string, None when absent; anything else is an
    error (`where.key must be a string`): a list as `command` would crash the start."""
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value
    hint = " (put arguments in agent.extra_args)" if key == "command" and isinstance(value, list) else ""
    errors.append(f"{where}.{key} must be a non-empty string, got {value!r}{hint}")
    return None


def create_agent(config: AgentConfig, heartbeat_seconds: int = 30) -> AgentBackend:
    """Create an agent backend from configuration."""
    agent_type = config.backend.lower()
    if agent_type == "claude":
        from autohelix.agents.claudecode import ClaudeCodeAgent

        return ClaudeCodeAgent(config=config, heartbeat_seconds=heartbeat_seconds)
    if agent_type == "codex":
        from autohelix.agents.codex import CodexAgent

        return CodexAgent(config=config, heartbeat_seconds=heartbeat_seconds)
    if agent_type == "opencode":
        from autohelix.agents.opencode import OpenCodeAgent

        return OpenCodeAgent(config=config, heartbeat_seconds=heartbeat_seconds)
    if agent_type == "mock":
        from autohelix.agents.mock import MockAgent

        return MockAgent(config=config, heartbeat_seconds=heartbeat_seconds)
    raise ValueError(f"Unsupported agent backend: {config.backend}")
