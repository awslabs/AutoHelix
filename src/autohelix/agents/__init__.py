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


# The keys of `agent:`. Removed and renamed keys are errors that name their replacement.
AGENT_KEYS = ("cli", "model", "effort", "timeout", "extra_args", "auto_memory")
RENAMED_AGENT_KEYS = {
    "type": "`agent.type` is now `agent.cli` (claude-code, codex or opencode)",
    "backend": "`agent.backend` is now `agent.cli` (claude-code, codex or opencode)",
    "reasoning_effort": "`agent.reasoning_effort` is now `agent.effort`",
    "timeout_seconds": "`agent.timeout_seconds` is now `agent.timeout` (a duration such as 30m)",
    "command": ("`agent.command` is gone: set AUTOHELIX_CLAUDE_CODE_CMD, AUTOHELIX_CODEX_CMD or "
                "AUTOHELIX_OPENCODE_CMD to the executable (else the CLI's name on PATH)"),
    "settings": "`agent.settings` is gone: pass the CLI's own options in `agent.extra_args`",
    "require_notes": "`agent.require_notes` is gone: ask for notes in the goal or the prompt template",
}
# Old `cli:` values, errors naming the new one.
RENAMED_CLIS = {"claude": "claude-code"}


@dataclass
class AgentConfig:
    """Configuration for an agent CLI."""

    cli: str = "claude-code"
    model: str | None = None  # passed through unchanged; None: the CLI's own default
    effort: str | None = None  # passed through (`CLI_EFFORT`)
    timeout: int | None = None  # seconds: the hard kill, and time-left hints where supported
    extra_args: list[str] = field(default_factory=list)
    auto_memory: bool = False  # Disabled by default; set true to enable Claude Code's built-in memory
    # `cli: mock`'s test knobs (`agent.mock:`, accepted with that hidden CLI only).
    mock: dict[str, Any] = field(default_factory=dict)
    # Judge mode (`autohelix ask`), set in code, never from YAML: no edit tools, no
    # project settings/hooks/MCP, no extra_args pass-through.
    read_only: bool = False
    system_prompt: str | None = None  # appended to the CLI's system prompt
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
        cli = cli_value(data, "agent", errors) or "claude-code"
        for key in data:
            if key in RENAMED_AGENT_KEYS:
                errors.append(RENAMED_AGENT_KEYS[key])
            elif key not in AGENT_KEYS and not (key == "mock" and cli == "mock"):
                errors.append(f"Unknown key agent.{key} (known: {', '.join(AGENT_KEYS)})")

        mock = data.get("mock") or {}
        if not isinstance(mock, dict):
            errors.append("agent.mock must be a mapping")
            mock = {}

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

        auto_memory = data.get("auto_memory", False)
        if not isinstance(auto_memory, bool):  # `"false"` would be true
            errors.append(f"agent.auto_memory must be true or false, got {auto_memory!r}")
            auto_memory = False

        return cls(
            cli=cli,
            model=agent_string(data, "model", "agent", errors),
            effort=agent_string(data, "effort", "agent", errors),
            timeout=timeout,
            extra_args=[str(arg) for arg in extra_args],
            auto_memory=auto_memory,
            mock=mock if cli == "mock" else {},
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
        copy_path: Path,
        prompt: str,
        iteration: int,
        log_path: Path,
        event_callback: EventCallback,
        project_path: Path | None = None,
    ) -> AgentRunResult:
        """Execute the agent inside the given copy."""


# Every `agent.cli` (`create_agent`; `mock`, for tests, is never named to users), and
# those an `ask:`/`review:` judge can use (a read-only mode).
CLIS = ("claude-code", "codex", "opencode", "mock")
JUDGE_CLIS = ("claude-code", "codex", "mock")
CLI_NAMES = {"claude-code": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}
# The executable each CLI is found as on PATH.
EXECUTABLES = {"claude-code": "claude", "codex": "codex", "opencode": "opencode"}
# Variables that are gone, with the one to set instead.
RENAMED_VARIABLES = {"AUTOHELIX_CLAUDE_CMD": "AUTOHELIX_CLAUDE_CODE_CMD"}


def command_variable(cli: str) -> str:
    """The variable naming a CLI's executable: AUTOHELIX_CLAUDE_CODE_CMD, ..."""
    return f"AUTOHELIX_{cli.upper().replace('-', '_')}_CMD"


def variable_problem() -> str | None:
    """A set variable that is gone (AUTOHELIX_CLAUDE_CMD), as an error naming its replacement."""
    for old, new in RENAMED_VARIABLES.items():
        if os.environ.get(old):
            return f"{old} is now {new}: set that instead (and unset {old})"
    return None


def command_path(cli: str) -> str:
    """The executable a CLI runs: `$AUTOHELIX_<CLI>_CMD`, else the CLI's name found on
    PATH, else the bare name (which then fails to start). The backends (when created)
    and preflight resolve it here; ConfigError for a variable that is gone."""
    name = executable(cli)
    return name if os.environ.get(command_variable(cli)) else shutil.which(name) or name


def executable(cli: str) -> str:
    """What starts a CLI, unresolved: `$AUTOHELIX_<CLI>_CMD`, else its name (`claude`).
    The one check of a variable that is gone (`variable_problem`): a ConfigError, which
    every command reports as it reports a config error (no traceback)."""
    if problem := variable_problem():
        from autohelix.config import ConfigError  # config imports this module

        raise ConfigError(problem)
    return os.environ.get(command_variable(cli)) or EXECUTABLES.get(cli, cli)


def cli_value(data: dict, where: str, errors: list[str]) -> str | None:
    """`data["cli"]`, lower-cased, None when absent; an old value (`claude`) is an error
    naming the new one (the new one is then used, so later checks see a valid CLI)."""
    value = agent_string(data, "cli", where, errors)
    if value is None:
        return None
    value = value.lower()
    if value in RENAMED_CLIS:
        errors.append(f"`{where}.cli: {value}` is now `{RENAMED_CLIS[value]}`")
        return RENAMED_CLIS[value]
    return value


def agent_string(data: dict, key: str, where: str, errors: list[str]) -> str | None:
    """`data[key]` when it is a non-empty string, None when absent; anything else is an
    error (`where.key must be a string`)."""
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value
    errors.append(f"{where}.{key} must be a non-empty string, got {value!r}")
    return None


def create_agent(config: AgentConfig, heartbeat_seconds: int = 30) -> AgentBackend:
    """Create an agent backend from configuration."""
    cli = config.cli.lower()
    if cli == "claude-code":
        from autohelix.agents.claudecode import ClaudeCodeAgent

        return ClaudeCodeAgent(config=config, heartbeat_seconds=heartbeat_seconds)
    if cli == "codex":
        from autohelix.agents.codex import CodexAgent

        return CodexAgent(config=config, heartbeat_seconds=heartbeat_seconds)
    if cli == "opencode":
        from autohelix.agents.opencode import OpenCodeAgent

        return OpenCodeAgent(config=config, heartbeat_seconds=heartbeat_seconds)
    if cli == "mock":
        from autohelix.agents.mock import MockAgent

        return MockAgent(config=config, heartbeat_seconds=heartbeat_seconds)
    raise ValueError(f"Unsupported agent.cli: {config.cli}")
