# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Agent backends for AutoHelix."""

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


@dataclass
class AgentConfig:
    """Configuration for an agent backend."""

    type: str = "claude"
    command: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    timeout_seconds: int | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    extra_args: list[str] = field(default_factory=list)
    auto_memory: bool = False  # Disabled by default; set true to enable Claude Code's built-in memory
    require_notes: RequireNotesConfig | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AgentConfig":
        """Build an agent config from YAML data."""
        data = data or {}

        settings = data.get("settings", {})
        if not isinstance(settings, dict):
            settings = {}

        extra_args = data.get("extra_args", [])
        if isinstance(extra_args, str):
            extra_args = [extra_args]
        elif not isinstance(extra_args, list):
            extra_args = []

        auto_memory = bool(data.get("auto_memory", False))

        # require_notes: bare True -> defaults; dict -> overrides; falsey/absent -> off
        require_notes: RequireNotesConfig | None = None
        raw_require_notes = data.get("require_notes")
        if isinstance(raw_require_notes, dict):
            require_notes = RequireNotesConfig(
                min_chars=int(raw_require_notes.get("min_chars", 50)),
                max_retries=int(raw_require_notes.get("max_retries", 3)),
            )
        elif raw_require_notes:  # True (or any truthy scalar)
            require_notes = RequireNotesConfig()

        return cls(
            type=str(data.get("type", "claude")),
            command=data.get("command"),
            model=data.get("model"),
            reasoning_effort=data.get("reasoning_effort"),
            timeout_seconds=data.get("timeout_seconds"),
            settings=settings,
            extra_args=[str(arg) for arg in extra_args],
            auto_memory=auto_memory,
            require_notes=require_notes,
        )


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


def create_agent(config: AgentConfig, heartbeat_seconds: int = 30) -> AgentBackend:
    """Create an agent backend from configuration."""
    agent_type = config.type.lower()
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
    raise ValueError(f"Unsupported agent type: {config.type}")
