# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Configuration loading and validation."""

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import glob as globmod
import json
import math
import os
import re
import shlex
from typing import Any

import yaml

from autohelix.agents import AgentConfig
from autohelix.iteration_time import parse_duration_string


MAX_DURATION = 30 * 24 * 3600  # 30 days


def parse_duration(value: str | int | float) -> int:
    """Parse a duration string like '1h', '30m', '2h30m' into total seconds.

    Also accepts an integer or a string of digits ("90"), as seconds. Unlike the optional variant
    in iteration_time, this requires a positive result of at most 30 days and
    raises on invalid or empty/zero input.
    """
    if isinstance(value, bool):
        raise ValueError(f"Duration must be a string or number, got bool ({value})")

    if isinstance(value, (int, float)):
        if not (value == value):  # NaN check
            raise ValueError("Duration must be a finite number, got NaN")
        if value == float("inf") or value == float("-inf"):
            raise ValueError(f"Duration must be a finite number, got {value}")
        if value > MAX_DURATION:
            raise ValueError(f"Duration must be at most 30 days, got {value}")
        secs = int(value)
        if secs <= 0:
            raise ValueError(f"Duration must be positive, got {value}")
        return secs

    s = str(value).strip()
    if not s:
        raise ValueError("Empty duration string")
    if re.fullmatch(r"\d+", s):  # "90", as a bare number: seconds
        return parse_duration(int(s))

    total = parse_duration_string(s)
    if total <= 0:
        raise ValueError(f"Duration must be positive, got '{s}'")
    if total > MAX_DURATION:
        raise ValueError(f"Duration must be at most 30 days, got '{s}'")
    return total


@dataclass
class ConfigIssue:
    """A structured validation issue with a severity level."""

    level: str  # "error" or "warning"
    message: str


# Fallback timeout (seconds) for gate/observe/stop commands that don't set their own
# `timeout:`. Generous enough not to trip legitimate ML/eval commands, while still
# catching a genuinely hung process. Override per entry in config.
DEFAULT_CHECK_TIMEOUT = 1800

KNOWN_TOP_LEVEL_KEYS = {
    "goal", "agent", "prompt_template", "scope", "gates", "observe", "stop",
}

# Keys of the first config format: each is an error naming its replacement.
RENAMED_TOP_LEVEL_KEYS = {
    "constraints": "`constraints:` is now `gates:`",
    "constraint": "`constraint:` is now `gates:`",
    "metrics": "`metrics:` is now `observe:` (each entry needs a `name:`; `values:` is now `metrics:`)",
    "observables": "`observables:` is now `observe:` (each entry needs a `name:`; `values:` is now `metrics:`)",
    "acceptance": ("`acceptance:` is now a `gates:` entry: `- metric: NAME` with `regress: N%` "
                   "(was max_regression_pct) or `improve: N%`"),
    "reviewer": "`reviewer:` is now an `observe:` entry with `name:` and `review: <prompt>`",
    "name": "`name:` is gone: runs are named after the config file; use autohelix/<name>.yaml",
    "budget": ("`budget:` moved to the command line: `autohelix run -n N --cost USD --time DURATION` "
               "(also `resume`; `agent.timeout` limits one iteration)"),
    "land": ("`land:` moved to the command line: `autohelix run --land auto|manual` (also `resume`; "
             "default auto, manual with --group)"),
    "report": "`report:` moved to the command line: `autohelix run --no-report` (also `resume`)",
}

ENTRY_AGENT_KEYS = ("backend", "model", "reasoning_effort", "timeout", "command")
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_METRIC_NAME = re.compile(r"\w+")
TITLE_LENGTH = 80


@dataclass
class CommandSpec:
    """A gate command (including `ask:` entries) or a stop command."""

    command: str
    timeout: int = DEFAULT_CHECK_TIMEOUT
    label: str | None = None  # display text ("ask: <question>"); None shows the command

    @property
    def text(self) -> str:
        return self.label or self.command


@dataclass
class MetricGate:
    """A comparison of one metric with the last accepted iteration's."""

    metric: str
    mode: str  # "regress" or "improve"
    threshold_pct: float  # e.g. 5.0 for "5%"

    @property
    def text(self) -> str:
        return f"{self.metric} {self.mode} {self.threshold_pct:g}%"


@dataclass
class ObserveEntry:
    """A command whose output the next agent reads, and whose metrics become the numbers."""

    name: str
    command: str  # for review entries: the rendered `autohelix review ...` command
    metrics: dict[str, str] = field(default_factory=dict)  # name -> "higher" or "lower"
    capture: list[str] = field(default_factory=list)  # files to save, relative to the copy
    timeout: int = DEFAULT_CHECK_TIMEOUT
    review: str | None = None  # the review prompt, for review entries


def goal_title(goal: str) -> str:
    """The goal's first line, cut at its first sentence end or about 80 characters."""
    line = goal.strip().split("\n", 1)[0].strip()
    if len(line) <= TITLE_LENGTH:
        return line
    end = re.search(r"[.!?](?=\s|$)", line)
    if end and end.end() <= TITLE_LENGTH:
        return line[:end.end()]
    cut = line[:TITLE_LENGTH - 1]
    if " " in cut[TITLE_LENGTH // 2:]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:") + "…"


@dataclass
class Config:
    """AutoHelix configuration."""

    goal: str
    gates: list[CommandSpec | MetricGate] = field(default_factory=list)
    observe: list[ObserveEntry] = field(default_factory=list)
    # Stop commands: the run ends when one exits 0 (checked on the baseline and
    # after each accepted iteration).
    stop: list[CommandSpec] = field(default_factory=list)
    agent: AgentConfig = field(default_factory=AgentConfig)
    editable: list[str] = field(default_factory=list)
    frozen: list[str] = field(default_factory=list)
    # A Jinja file replacing the built-in agent prompt, resolved against the config file's folder;
    # `prompt_template_text` is its text when the config is a run's stored copy (which inlines it).
    prompt_template: Path | None = None
    prompt_template_text: str | None = None
    # The config file and its template, project-relative, as named and (for symlinks) as
    # resolved: always frozen, whatever `scope:` says (the agent can't edit its own protocol).
    protected: list[str] = field(default_factory=list)
    _parse_errors: list[str] = field(default_factory=list, repr=False)

    @property
    def title(self) -> str:
        """The goal's first line, cut to about 80 characters (`goal_title`)."""
        return goal_title(self.goal)

    def frozen_names(self) -> list[str]:
        """What is frozen, as shown (header, prompt): `scope.frozen`, then the config and
        template files."""
        return self.frozen + [path for path in self.protected if path not in self.frozen]

    def frozen_patterns(self) -> list[str]:
        """What the harness matches as frozen: `frozen_names` with the files' glob
        characters escaped."""
        return self.frozen + [globmod.escape(path) for path in self.protected if path not in self.frozen]

    def gate_commands(self) -> list[CommandSpec]:
        return [gate for gate in self.gates if isinstance(gate, CommandSpec)]

    def metric_gates(self) -> list[MetricGate]:
        return [gate for gate in self.gates if isinstance(gate, MetricGate)]

    def metric_directions(self) -> dict[str, str]:
        """Every declared metric's direction, in declaration order."""
        return {name: direction for entry in self.observe for name, direction in entry.metrics.items()}

    def metric_names(self) -> set[str]:
        """Return the set of all declared metric names."""
        return set(self.metric_directions())

    def metric_source(self, name: str) -> ObserveEntry:
        """The observe entry declaring a metric; KeyError if none does."""
        for entry in self.observe:
            if name in entry.metrics:
                return entry
        raise KeyError(name)

    def validate(self, raw_data: dict[str, Any] | None = None) -> list[ConfigIssue]:
        """Return a list of structured validation issues.

        Args:
            raw_data: If provided, also checks for unknown top-level keys
                      (catches typos like 'gaol' instead of 'goal').
        """
        issues: list[ConfigIssue] = []

        def error(msg: str) -> None:
            issues.append(ConfigIssue("error", msg))

        def warning(msg: str) -> None:
            issues.append(ConfigIssue("warning", msg))

        for message in self._parse_errors:
            error(message)

        # Check for unknown keys (typo detection)
        if raw_data is not None:
            unknown = set(raw_data.keys()) - KNOWN_TOP_LEVEL_KEYS - set(RENAMED_TOP_LEVEL_KEYS)
            for key in sorted(unknown):
                warning(f"Unknown config key '{key}' - possible typo?")

        # Goal must be non-empty and not the default placeholder
        if not isinstance(self.goal, str) or not self.goal.strip():
            error("'goal' is empty - the agent won't know what to optimize")
        elif "describe what you want" in self.goal.lower():
            error(
                "'goal' is still the default placeholder - "
                "replace it with your optimization goal"
            )
        elif len(self.goal.strip().split("\n", 1)[0].strip()) > TITLE_LENGTH:
            warning(f"The goal's first line is its title and is shown cut to \"{self.title}\"; "
                    f"keep it under {TITLE_LENGTH} characters and put details on the following lines")

        if not self.gates and not self.observe:
            warning(
                "No gates or observe entries defined - "
                "iterations will always be accepted with no measurement"
            )

        known_backends = {"claude", "codex", "opencode", "mock"}
        backend = self.agent.backend.lower()
        if backend not in known_backends:
            error(
                f"Unknown agent backend '{self.agent.backend}' "
                f"(must be one of: {', '.join(sorted(known_backends))})"
            )

        # agent.settings is only consumed by codex (-c key=value passthrough)
        # and by the mock agent for test knobs; other backends ignore it.
        if self.agent.settings and backend in {"claude", "opencode"}:
            warning(
                f"agent.settings is ignored by the '{self.agent.backend}' backend "
                f"(only the 'codex' backend consumes it)"
            )

        # require_notes depends on a Claude Code Stop hook.
        if self.agent.require_notes is not None:
            if backend in {"codex", "opencode"}:
                error(
                    f"agent.require_notes is not implemented for agent backend "
                    f"'{backend}'. Currently only 'claude' is supported."
                )
            if self.agent.require_notes.min_chars < 0:
                error("agent.require_notes.min_chars must be >= 0")
            if self.agent.require_notes.max_retries < 1:
                error("agent.require_notes.max_retries must be >= 1")

        if (self.prompt_template is not None and self.prompt_template_text is None
                and not self.prompt_template.is_file()):
            error(f"prompt_template not found: {self.prompt_template}")
        return issues

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        """Create config from dictionary; problems are reported by validate()."""
        errors: list[str] = []
        for key in data:
            if key in RENAMED_TOP_LEVEL_KEYS:
                errors.append(RENAMED_TOP_LEVEL_KEYS[key])

        agent, agent_errors = AgentConfig.parse(data.get("agent"))
        errors.extend(agent_errors)

        observe = _parse_observe(data.get("observe"), agent.backend, errors)
        declared = {name: entry for entry in observe for name in entry.metrics}
        gates = _parse_gates(data.get("gates"), declared, agent.backend, errors)
        stop = _parse_stop(data.get("stop"), errors)

        # Parse scope (`or {}` guards a present-but-empty `scope:` key)
        scope = data.get("scope") or {}
        if not isinstance(scope, dict):
            errors.append("scope must be a mapping with editable: and/or frozen:")
            scope = {}
        editable = _paths(scope.get("editable"), "scope.editable", errors)
        frozen = _paths(scope.get("frozen"), "scope.frozen", errors)
        for key in scope:
            if key not in ("editable", "frozen"):
                errors.append(f"Unknown key scope.{key} (known: editable, frozen)")

        goal = data.get("goal", "")
        return cls(
            goal=goal if isinstance(goal, str) else "" if goal is None else str(goal),
            gates=gates,
            observe=observe,
            stop=stop,
            agent=agent,
            editable=editable,
            frozen=frozen,
            _parse_errors=errors,
        )


def _duration(value: Any, where: str, errors: list[str]) -> int | None:
    """A duration in seconds: a bare number is seconds, a string a duration ("90s", "2h30m")."""
    try:
        return parse_duration(value)
    except (ValueError, OverflowError) as e:
        errors.append(f"{where}: {value!r} is not a valid duration ({e})")
        return None


def _paths(value: Any, where: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        errors.append(f"{where} must be a path or a list of paths")
        return []
    return list(value)


# An observe entry's own output files in `iterations/NNN/observe/<name>/`, and folders a
# capture may never copy (repository and harness state). Compared case-insensitively:
# macOS file systems are.
OUTPUT_FILES = ("stdout.txt", "stderr.txt")
RESERVED_CAPTURE_PARTS = (".git", ".autohelix", ".autohelix-iteration")


def capture_problem(capture: str) -> str | None:
    """Why a `capture:` path is refused (before anything runs), or None."""
    path = PurePosixPath(capture)
    parts = [part for part in path.parts if part not in ("", ".")]
    if path.is_absolute() or not parts or ".." in parts:
        return "not a path inside the copy"
    if reserved := next((part for part in parts if part.casefold() in RESERVED_CAPTURE_PARTS), None):
        return f"{reserved} is repository or harness state"
    if len(parts) == 1 and parts[0].casefold() in OUTPUT_FILES:
        return f"{parts[0]} is the entry's own output"
    return None


def _entries(value: Any, where: str, errors: list[str]) -> list:
    """A list of entries; a single string or mapping counts as a list of one."""
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        return [value]
    if not isinstance(value, list):
        errors.append(f"{where}: expected a list of entries, got {value!r}")
        return []
    return value


def _check_keys(entry: dict, allowed: tuple[str, ...], where: str, errors: list[str]) -> bool:
    unknown = [key for key in entry if key not in allowed]
    for key in unknown:
        errors.append(f"{where}: unknown key `{key}` (known: {', '.join(allowed)})")
    return not unknown


def _timeout(entry: dict, where: str, errors: list[str]) -> int | None:
    """The entry's `timeout:` in seconds, or None when unset or invalid (reported)."""
    if entry.get("timeout") is None:
        return None
    return _duration(entry["timeout"], f"{where} timeout", errors)


def _command_text(entry: dict, key: str, where: str, errors: list[str]) -> str | None:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}: `{key}:` must be a non-empty string")
        return None
    return value


def _entry_agent(entry: dict, timeout: int | None, backend: str, where: str,
                 errors: list[str]) -> tuple[list[str], int]:
    """The flags an `ask:` or `review:` entry's `agent:` mapping becomes, and the entry's
    command timeout. The entry's `timeout:` also bounds its agent, unless `agent.timeout`
    sets that; `agent.timeout` alone makes the command timeout a minute longer.
    `backend` is the top-level one, the entry's default."""
    agent = entry.get("agent")
    if agent is None:
        agent = {}
    if not isinstance(agent, dict):
        errors.append(f"{where}: `agent:` must be a mapping")
        agent = {}
    _check_keys(agent, ENTRY_AGENT_KEYS, f"{where} agent", errors)
    flags: list[str] = []
    own = agent.get("backend")
    if own is not None:
        own = str(own).lower()
        if own not in ("claude", "codex", "mock"):
            errors.append(f"{where}: agent.backend {own!r} can't be used here"
                          + (" (OpenCode has no read-only mode)" if own == "opencode" else
                             " (must be claude or codex)"))
        flags += ["--agent", own]
    elif backend == "opencode":
        errors.append(f"{where}: opencode has no read-only mode; set agent.backend on this entry")
    for key, flag in (("model", "--model"), ("reasoning_effort", "--effort")):
        if agent.get(key) is not None:
            flags += [flag, str(agent[key])]
    agent_timeout = None
    if agent.get("timeout") is not None:
        agent_timeout = _duration(agent["timeout"], f"{where} agent.timeout", errors)
    if agent_timeout is not None and timeout is not None and agent_timeout >= timeout:
        errors.append(f"{where}: agent.timeout ({agent_timeout}s) must be shorter than the entry's "
                      f"timeout ({timeout}s), which stops the whole command")
    if agent_timeout is not None or timeout is not None:
        flags += ["--timeout", str(agent_timeout or timeout)]
    if agent.get("command") is not None:
        flags += ["--command", str(agent["command"])]
    command_timeout = timeout or (agent_timeout + 60 if agent_timeout else DEFAULT_CHECK_TIMEOUT)
    return flags, command_timeout


def _render(command: list[str], flags: list[str], text: str) -> str:
    """`command flags -- text`, each word quoted: a text starting with `-` is not an option."""
    return " ".join(shlex.quote(word) for word in [*command, *flags, "--", text])


def parse_threshold(value: Any) -> float:
    """A metric gate's percentage, written with %: "5%", "0.5%"; must be >= 0."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        raise ValueError(f"write the percentage with %, e.g. {value:g}%, got {value!r}")
    if not isinstance(value, str) or not value.strip().endswith("%"):
        raise ValueError(f"expected a percentage such as 5%, got {value!r}")
    try:
        number = float(value.strip().removesuffix("%").strip())
    except ValueError:
        raise ValueError(f"expected a percentage such as 5%, got {value!r}") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"must be a percentage of 0% or more, got {value!r}")
    return number


def _parse_gates(value: Any, declared: dict[str, ObserveEntry], backend: str,
                 errors: list[str]) -> list[CommandSpec | MetricGate]:
    gates: list[CommandSpec | MetricGate] = []
    for index, entry in enumerate(_entries(value, "gates", errors), start=1):
        where = f"gates entry {index}"
        if isinstance(entry, str):
            if entry.strip():
                gates.append(CommandSpec(command=entry))
            else:
                errors.append(f"{where}: the command is empty")
            continue
        if not isinstance(entry, dict):
            errors.append(f"{where}: expected a command or a mapping, got {entry!r}")
            continue
        kinds = [key for key in ("command", "ask", "metric") if key in entry]
        if len(kinds) != 1:
            errors.append(f"{where}: needs exactly one of `command:`, `ask:` or `metric:`")
            continue
        kind = kinds[0]
        if kind == "command":
            _check_keys(entry, ("command", "timeout"), where, errors)
            command = _command_text(entry, "command", where, errors)
            timeout = _timeout(entry, where, errors)
            if command is not None:
                gates.append(CommandSpec(command=command, timeout=timeout or DEFAULT_CHECK_TIMEOUT))
        elif kind == "ask":
            # Unquoted `yes:` / `no:` keys are YAML booleans.
            entry = {{True: "yes", False: "no"}.get(key, key) if isinstance(key, bool) else key: value
                     for key, value in entry.items()}
            _check_keys(entry, ("ask", "yes", "no", "timeout", "agent"), where, errors)
            question = _command_text(entry, "ask", where, errors)
            timeout = _timeout(entry, where, errors)
            flags = []
            for key in ("yes", "no"):
                if entry.get(key) is not None:
                    if not isinstance(entry[key], str):
                        errors.append(f"{where}: `{key}:` must be a string")
                    flags += [f"--{key}", str(entry[key])]
            agent_flags, command_timeout = _entry_agent(entry, timeout, backend, where, errors)
            if question is not None:
                gates.append(CommandSpec(command=_render(["autohelix", "ask"], flags + agent_flags, question),
                                         timeout=command_timeout, label=f"ask: {' '.join(question.split())}"))
        else:
            _check_keys(entry, ("metric", "regress", "improve"), where, errors)
            metric = entry.get("metric")
            modes = [key for key in ("regress", "improve") if key in entry]
            if len(modes) != 1:
                errors.append(f"{where}: a metric gate needs exactly one of `regress: N%` or `improve: N%`")
                continue
            try:
                threshold = parse_threshold(entry[modes[0]])
            except ValueError as e:
                errors.append(f"{where}: {modes[0]}: {e}")
                continue
            if not isinstance(metric, str) or metric not in declared:
                errors.append(f"{where}: metric {metric!r} is not declared by an observe entry's `metrics:`")
                continue
            gates.append(MetricGate(metric=metric, mode=modes[0], threshold_pct=threshold))
    return gates


def _parse_observe(value: Any, backend: str, errors: list[str]) -> list[ObserveEntry]:
    entries: list[ObserveEntry] = []
    names: set[str] = set()
    metric_owner: dict[str, str] = {}
    for index, entry in enumerate(_entries(value, "observe", errors), start=1):
        where = f"observe entry {index}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: expected a mapping with `name:` and `command:` or `review:`, got {entry!r}")
            continue
        name = entry.get("name")
        if isinstance(name, str) and _NAME.fullmatch(name):
            where = f"observe entry `{name}`"
            if name in names:
                errors.append(f"observe: two entries are named `{name}`; names must be unique")
            names.add(name)
        elif name is None:
            errors.append(f"{where}: needs a `name:` (it names the entry's observations)")
        else:
            errors.append(f"{where}: name {name!r} must be lowercase letters, digits, _ or -, "
                          "starting with a letter or digit, at most 64 characters")
        if "values" in entry:
            errors.append(f"{where}: `values:` is now `metrics:`")
        if "capture_stdout" in entry:
            errors.append(f"{where}: `capture_stdout:` is gone: stdout is always saved; remove it")
        kinds = [key for key in ("command", "review") if key in entry]
        if len(kinds) != 1:
            errors.append(f"{where}: needs exactly one of `command:` or `review:`")
            continue
        review = kinds[0] == "review"
        allowed = (("name", "review", "previous", "metrics", "agent", "timeout") if review
                   else ("name", "command", "metrics", "capture", "timeout"))
        _check_keys({k: v for k, v in entry.items() if k not in ("values", "capture_stdout")},
                    allowed, where, errors)
        text = _command_text(entry, kinds[0], where, errors)
        timeout = _timeout(entry, where, errors)

        metrics: dict[str, str] = {}
        raw_metrics = entry.get("metrics")
        if raw_metrics is not None and not isinstance(raw_metrics, dict):
            errors.append(f"{where}: `metrics:` must map metric names to higher or lower")
            raw_metrics = None
        for metric, direction in (raw_metrics or {}).items():
            if not isinstance(metric, str) or not _METRIC_NAME.fullmatch(metric):
                errors.append(f"{where}: metric name {metric!r} must be letters, digits or _")
                continue
            if direction not in ("higher", "lower"):
                errors.append(f"Metric '{metric}' has invalid direction {direction!r} (must be 'higher' or 'lower')")
                continue
            if metric in metric_owner:
                errors.append(f"Metric '{metric}' is declared twice (by `{metric_owner[metric]}` and "
                              f"`{name}`); metric names must be unique across observe entries")
                continue
            metric_owner[metric] = str(name)
            metrics[metric] = direction

        capture: list[str] = []
        command_timeout = timeout or DEFAULT_CHECK_TIMEOUT
        if review:
            previous = entry.get("previous", False)
            if not isinstance(previous, bool):
                errors.append(f"{where}: `previous:` must be true or false")
            flags = ["--previous"] if previous is True else []
            for metric, direction in metrics.items():
                flags += ["--metric", f"{metric}:{direction}"]
            agent_flags, command_timeout = _entry_agent(entry, timeout, backend, where, errors)
            command = _render(["autohelix", "review"], flags + agent_flags, text or "")
        else:
            command = text or ""
            capture = _paths(entry.get("capture"), f"{where} capture", errors)
            for path in capture:
                if problem := capture_problem(path):
                    errors.append(f"{where} capture {path!r}: {problem}")
        if text is None or not isinstance(name, str):
            continue
        entries.append(ObserveEntry(name=name, command=command, metrics=metrics, capture=capture,
                                    timeout=command_timeout, review=text if review else None))
    return entries


def _parse_stop(value: Any, errors: list[str]) -> list[CommandSpec]:
    commands: list[CommandSpec] = []
    for index, entry in enumerate(_entries(value, "stop", errors), start=1):
        where = f"stop entry {index}"
        if isinstance(entry, str):
            if entry.strip():
                commands.append(CommandSpec(command=entry))
            else:
                errors.append(f"{where}: the command is empty")
        elif isinstance(entry, dict):
            _check_keys(entry, ("command", "timeout"), where, errors)
            command = _command_text(entry, "command", where, errors)
            timeout = _timeout(entry, where, errors)
            if command is not None:
                commands.append(CommandSpec(command=command, timeout=timeout or DEFAULT_CHECK_TIMEOUT))
        else:
            errors.append(f"{where}: expected a command or a mapping, got {entry!r}")
    return commands


CONFIG_FILE = "autohelix.yaml"
CONFIG_DIR = "autohelix"


class ConfigError(ValueError):
    """A config file that can't be read as a config (not YAML, not a mapping)."""


class ConfigNotFound(FileNotFoundError):
    """No config by that name, or no single config for a bare command. The message
    states what was found; `configs` lists the project's configs (project-relative)
    so each caller can add its own advice."""

    def __init__(self, message: str, configs: list[str]):
        super().__init__(message)
        self.configs = configs


def _files(project: Path, pattern: str) -> list[str]:
    """Files matching `pattern` directly in autohelix/, project-relative; dotfiles
    (editor backups, lock files) are not configs."""
    folder = project / CONFIG_DIR
    if not folder.is_dir():
        return []
    return sorted(f"{CONFIG_DIR}/{p.name}" for p in folder.glob(pattern)
                  if p.is_file() and not p.name.startswith("."))


def project_configs(project: Path) -> list[str]:
    """The project's configs, project-relative: autohelix.yaml, then the `.yaml` files
    directly in autohelix/ (its subfolders hold prompt templates and helper scripts)."""
    return ([CONFIG_FILE] if (project / CONFIG_FILE).is_file() else []) + _files(project, "*.yaml")


def run_command(config_path: str) -> str:
    """The command that starts runs of a config (project-relative, as run.json has it)."""
    path = PurePosixPath(config_path)
    if path.parent == PurePosixPath(CONFIG_DIR) and path.suffix == ".yaml":
        return f"autohelix run {path.stem}"
    return "autohelix run"


def find_config(project: Path, name: str | None = None) -> Path:
    """The config `name` names (autohelix/<name>.yaml), else autohelix.yaml, else the
    only config in autohelix/. Raises ConfigNotFound otherwise, and ValueError for a
    name that is a path."""
    configs = project_configs(project)
    listed = ", ".join(configs)
    if name is not None:
        if not re.fullmatch(r"[\w-][\w.-]*", name) or name.endswith((".yaml", ".yml")):
            raise ValueError(f"{name!r} is not a config name: name a config in {CONFIG_DIR}/ "
                             f"without .yaml (`perf` for {CONFIG_DIR}/perf.yaml); a bare `autohelix run` "
                             f"reads {CONFIG_FILE}.")
        relative = f"{CONFIG_DIR}/{name}.yaml"
        if relative in configs:
            return project / relative
        misnamed = f"{CONFIG_DIR}/{name}.yml"
        found = (f" (found {misnamed}; configs end in .yaml)" if (project / misnamed).is_file() else "")
        raise ConfigNotFound(f"No config {relative}{found}." + (f" Configs: {listed}." if configs else ""), configs)
    if (project / CONFIG_FILE).is_file():
        return project / CONFIG_FILE
    if len(configs) == 1:
        return project / configs[0]
    if configs:
        raise ConfigNotFound(f"Several configs: {listed}.", configs)
    misnamed = (["autohelix.yml"] if (project / "autohelix.yml").is_file() else []) + _files(project, "*.yml")
    found = f" (found {', '.join(misnamed)}; configs end in .yaml)" if misnamed else ""
    raise ConfigNotFound(f"No config found in {project}{found}.", configs)


def parse_config(config_path: Path, project_path: Path) -> dict[str, Any]:
    """The config file's YAML mapping; ConfigError if it isn't one (named relative to the project)."""
    try:
        shown = config_path.resolve().relative_to(Path(project_path).resolve())
    except ValueError:
        shown = config_path
    with open(config_path) as f:
        try:
            data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"Cannot parse {shown}: {e}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{shown} must be a YAML mapping")
    return data


def template_path(config_path: Path, file: str) -> Path:
    """A `prompt_template:` path, next to the config file, resolved."""
    return (config_path.resolve().parent / Path(file).expanduser()).resolve()


def _project_relative(project: Path, paths) -> list[str]:
    """`paths` relative to the project, each as named and as resolved (a symlink and its
    target), outside ones left out."""
    roots = [Path(os.path.normpath(project.absolute())), project.resolve()]
    found = []
    for path in paths:
        for candidate in (Path(os.path.normpath(path.absolute())), path.resolve()):
            root = next((r for r in roots if candidate.is_relative_to(r)), None)
            if root is not None and (name := candidate.relative_to(root).as_posix()) not in found:
                found.append(name)
    return found


def load_config(project_path: Path, config_file: Path | None = None, *,
                source: Path | None = None) -> tuple[Config, dict[str, Any]]:
    """Load configuration from `config_file`, else the project's config (`find_config`).

    `prompt_template:` is a path, resolved next to the config file, or a run's stored
    form `{file, text}` (`stored_config_text`): the text is the template. For a run's
    stored copy, `source` is the file it was made from: `file` resolves next to it, and
    it is the config file `Config.protected` freezes.

    Returns:
        A tuple of (Config, raw_data) where raw_data is the parsed YAML dict.
        The raw_data is used for unknown-key validation.
    """
    config_path = Path(config_file) if config_file else find_config(project_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")
    data = parse_config(config_path, project_path)

    config = Config.from_dict(data)
    origin = Path(source) if source else config_path
    template = data.get("prompt_template")
    if isinstance(template, dict):
        for key in template:
            if key not in ("file", "text"):
                config._parse_errors.append(f"Unknown key prompt_template.{key} (known: file, text)")
        file, text = template.get("file"), template.get("text")
        if isinstance(file, str) and file.strip() and isinstance(text, str):
            config.prompt_template = template_path(origin, file)
            config.prompt_template_text = text
        else:
            config._parse_errors.append("a stored prompt_template needs file: (its path) and text: (its contents)")
    elif isinstance(template, str) and template.strip():
        config.prompt_template = template_path(origin, template)
    elif template is not None:
        config._parse_errors.append("prompt_template must be a path to a template file")
    named = [origin]
    if config.prompt_template is not None:
        file = template["file"] if isinstance(template, dict) else template
        named.append(origin.resolve().parent / Path(file).expanduser())  # where it resolves from
    config.protected = _project_relative(Path(project_path), named)
    return config, data


STORED_NOTE = "# stored when the run started"


def inlined(config_path: Path, data: dict[str, Any] | None = None, *,
            project: Path | None = None) -> dict[str, Any]:
    """The config in `config_path` (parsed: `data`) as a run stores it: a `prompt_template:`
    path becomes `{file, text}` with the template's current text. Raises ConfigError
    when the file or its template can't be read (naming it relative to `project`)."""
    if data is None:
        data = parse_config(config_path, project or config_path.parent)
    template = data.get("prompt_template")
    if not isinstance(template, str) or not template.strip():
        return data
    try:
        text = template_path(config_path, template).read_text()
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"Cannot read prompt_template {template}: {e}") from None
    return {**data, "prompt_template": {"file": template, "text": text}}


class _StoredDumper(yaml.SafeDumper):
    """Multi-line strings as literal blocks where YAML allows it (the dump fallback)."""


_StoredDumper.add_representer(str, lambda dumper, value: dumper.represent_scalar(
    "tag:yaml.org,2002:str", value, style="|" if "\n" in value else None))


def _template_block(file: str, text: str) -> str:
    """`prompt_template:` in its stored form, as YAML lines: `text` a literal block with
    the chomping its trailing newlines need, and an indentation indicator when its
    first line starts with spaces. `stored_config_text` checks the result parses back."""
    plain = yaml.safe_load(f"k: {file}") == {"k": file} if file.strip() == file and file else False
    chomp = "+" if text.endswith("\n\n") else "" if text.endswith("\n") else "-"
    first = next((line for line in text.split("\n") if line.strip()), "")
    indicator = "2" if first.startswith(" ") else ""
    body = text if text.endswith("\n") else text + "\n"
    lines = "".join(("    " + line if line else "") + "\n" for line in body.split("\n")[:-1])
    return (f"prompt_template:  {STORED_NOTE}\n"
            + f"  file: {file if plain else json.dumps(file)}\n  text: |{indicator}{chomp}\n{lines}")


def stored_config_text(config_path: Path) -> str:
    """What a run stores as `runs/<id>/config.yaml`: the config file's text verbatim
    (comments kept), with a `prompt_template: x.md` line replaced by the stored form
    `{file, text}`. When that line can't be replaced textually (flow style, a value
    on the next line), a YAML dump of the stored form. Raises ConfigError as `inlined`."""
    text = config_path.read_text()
    data = parse_config(config_path, config_path.parent)
    stored = inlined(config_path, data)
    if stored is data:
        return text
    lines = text.splitlines(keepends=True)
    found = [i for i, line in enumerate(lines) if re.match(r"prompt_template\s*:", line)]
    if len(found) == 1:
        i = found[0]
        template = stored["prompt_template"]
        candidate = ("".join(lines[:i]) + _template_block(template["file"], template["text"])
                     + "".join(lines[i + 1:]))
        try:
            if yaml.safe_load(candidate) == stored:
                return candidate
        except yaml.YAMLError:
            pass
    return (f"{STORED_NOTE} from {config_path.name} (its prompt_template inlined)\n"
            + yaml.dump(stored, Dumper=_StoredDumper, sort_keys=False, allow_unicode=True))


def _normalized(value: Any) -> Any:
    """Mappings without None values or empty containers, at any depth (`gates: []`,
    `scope: {editable: null}` and no key are the same)."""
    if isinstance(value, dict):
        items = ((key, _normalized(item)) for key, item in value.items())
        return {key: item for key, item in items if item is not None and item != {} and item != []}
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    return value


def config_changes(stored: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """The top-level keys whose parsed values differ: comments and formatting don't count,
    nor empty values (`_normalized`) or how `prompt_template.file` spells the path
    (`./p.md`). Defaults written out (`agent: {backend: claude}`) and other spellings (`10m`, `600`)
    do: the refusal names the keys."""
    def normalized(data: dict[str, Any]) -> dict[str, Any]:
        data = _normalized(data)
        template = data.get("prompt_template")
        if isinstance(template, dict) and isinstance(template.get("file"), str):
            data["prompt_template"] = {**template, "file": os.path.normpath(template["file"])}
        return data

    stored, current = normalized(stored), normalized(current)
    return [key for key in dict.fromkeys([*stored, *current]) if stored.get(key) != current.get(key)]
