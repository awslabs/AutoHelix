# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Configuration loading and validation."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from autohelix.agents import AgentConfig
from autohelix.iteration_time import parse_duration as parse_iteration_duration
from autohelix.iteration_time import parse_duration_string


def parse_duration(value: str | int | float) -> int:
    """Parse a duration string like '1h', '30m', '2h30m' into total seconds.

    Also accepts an integer (treated as seconds). Unlike the optional variant
    in iteration_time, this requires a positive result and raises on invalid
    or empty/zero input.
    """
    if isinstance(value, bool):
        raise ValueError(f"Duration must be a string or number, got bool ({value})")

    if isinstance(value, (int, float)):
        if not (value == value):  # NaN check
            raise ValueError("Duration must be a finite number, got NaN")
        if value == float("inf") or value == float("-inf"):
            raise ValueError(f"Duration must be a finite number, got {value}")
        secs = int(value)
        if secs <= 0:
            raise ValueError(f"Duration must be positive, got {value}")
        return secs

    s = str(value).strip()
    if not s:
        raise ValueError("Empty duration string")

    total = parse_duration_string(s)
    if total <= 0:
        raise ValueError(f"Duration must be positive, got '{s}'")
    return total


@dataclass
class ConfigIssue:
    """A structured validation issue with a severity level."""

    level: str  # "error" or "warning"
    message: str


# Fallback timeout (seconds) for constraint/observable commands that don't set
# their own `timeout:`. Generous enough not to trip legitimate ML/eval commands,
# while still catching a genuinely hung process. Override per-command in config.
DEFAULT_CHECK_TIMEOUT = 1800

KNOWN_TOP_LEVEL_KEYS = {
    "goal", "constraints", "constraint", "metrics", "observables",
    "scope", "agent", "acceptance", "budget", "reviewer",
}


@dataclass
class ConstraintCommand:
    """A constraint command that must pass for an iteration to be accepted."""

    command: str
    timeout: int = DEFAULT_CHECK_TIMEOUT


@dataclass
class ObservableCommand:
    """An observable command whose output is captured for the agent.

    Optionally extracts scalar values for terminal display and gating.
    """

    command: str
    values: dict[str, str] = field(default_factory=dict)  # metric name -> direction ("higher" or "lower")
    capture: list[str] = field(default_factory=list)  # extra files to save
    capture_stdout: bool = True
    timeout: int = DEFAULT_CHECK_TIMEOUT


@dataclass
class MetricGate:
    """Gate that rejects iterations where a metric regresses too much."""

    metric: str
    max_regression_pct: float = 10  # percentage, e.g. 10 means 10%


@dataclass
class AcceptanceConfig:
    """Optional acceptance policy beyond constraints."""

    metric_gates: list[MetricGate] = field(default_factory=list)


DEFAULT_REVIEWER_PROMPT = """\
Review the current state of this codebase.
What's working well? What needs improvement?
What specific change would have the most impact for the next iteration?
"""


@dataclass
class ReviewerConfig:
    """Configuration for the reviewer agent."""

    prompt: str = DEFAULT_REVIEWER_PROMPT
    model: str | None = None  # Override main agent model
    timeout_seconds: int | None = None  # Override main agent timeout
    auto_memory: bool = False  # Disabled by default for independent reviews


@dataclass
class Config:
    """AutoHelix configuration."""

    goal: str
    constraints: list[ConstraintCommand]
    observables: list[ObservableCommand]
    agent: AgentConfig = field(default_factory=AgentConfig)
    acceptance: AcceptanceConfig = field(default_factory=AcceptanceConfig)
    reviewer: ReviewerConfig | None = None
    editable: list[str] = field(default_factory=list)
    frozen: list[str] = field(default_factory=list)
    max_iterations: int = 5
    max_cost_usd: float | None = None
    max_time_seconds: int | None = None
    iteration_time_seconds: int | None = None
    _parse_errors: dict[str, str] = field(default_factory=dict, repr=False)

    def metric_directions(self) -> dict[str, str]:
        """Return a flat dict of metric_name -> direction for all declared metrics."""
        result: dict[str, str] = {}
        for obs in self.observables:
            for name, direction in obs.values.items():
                result[name] = direction
        return result

    def metric_names(self) -> set[str]:
        """Return the set of all declared metric names."""
        names: set[str] = set()
        for obs in self.observables:
            names.update(obs.values.keys())
        return names

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

        # Check for unknown keys (typo detection)
        if raw_data is not None:
            unknown = set(raw_data.keys()) - KNOWN_TOP_LEVEL_KEYS
            for key in sorted(unknown):
                warning(f"Unknown config key '{key}' - possible typo?")

        # Goal must be non-empty and not the default placeholder
        if not self.goal.strip():
            error("'goal' is empty - the agent won't know what to optimize")
        elif "describe what you want" in self.goal.lower():
            error(
                "'goal' is still the default placeholder - "
                "edit autohelix.yaml with your optimization goal"
            )

        # Warn if no constraints and no observables
        if not self.constraints and not self.observables:
            warning(
                "No constraints or observables defined - "
                "iterations will always be accepted with no measurement"
            )

        # Validate observables
        for obs in self.observables:
            if not obs.command.strip():
                error(f"Observable command is empty")
            for name, direction in obs.values.items():
                if direction not in ("higher", "lower"):
                    error(
                        f"Metric '{name}' has invalid direction '{direction}' "
                        f"(must be 'higher' or 'lower')"
                    )

        # Validate agent type
        known_agents = {"claude", "codex", "opencode", "mock"}
        if self.agent.type.lower() not in known_agents:
            error(
                f"Unknown agent type '{self.agent.type}' "
                f"(must be one of: {', '.join(sorted(known_agents))})"
            )

        # agent.settings is only consumed by codex (-c key=value passthrough)
        # and by the mock agent for test knobs; other backends ignore it.
        if self.agent.settings and self.agent.type.lower() in {"claude", "opencode"}:
            warning(
                f"agent.settings is ignored by the '{self.agent.type}' backend "
                f"(only the 'codex' backend consumes it)"
            )

        # iteration_time injects remaining time into the agent's context.
        # claude and codex use hooks; opencode uses a tool.execute.after plugin
        # (see harness._install_opencode_timer_plugin).

        # require_notes depends on a Claude Code Stop hook.
        if self.agent.require_notes is not None:
            agent_type = self.agent.type.lower()
            if agent_type in {"codex", "opencode"}:
                error(
                    f"agent.require_notes is not implemented for agent type "
                    f"'{agent_type}'. Currently only 'claude' is supported."
                )
            if self.agent.require_notes.min_chars < 0:
                error("agent.require_notes.min_chars must be >= 0")
            if self.agent.require_notes.max_retries < 1:
                error("agent.require_notes.max_retries must be >= 1")

        # Validate timeout
        if self.agent.timeout_seconds is not None:
            try:
                val = int(self.agent.timeout_seconds)
                if val <= 0:
                    error("agent.timeout_seconds must be a positive integer")
            except (TypeError, ValueError):
                error(
                    f"agent.timeout_seconds must be a positive integer, "
                    f"got '{self.agent.timeout_seconds}'"
                )

        # Validate metric gates reference existing metrics
        known_metric_names = self.metric_names()
        for gate in self.acceptance.metric_gates:
            if gate.metric not in known_metric_names:
                error(
                    f"Metric gate references unknown metric '{gate.metric}'"
                )

        # Validate budget cost
        if self.max_cost_usd is not None and self.max_cost_usd <= 0:
            error(self._parse_errors.get("cost", "budget.cost must be a positive number"))

        # Validate budget time
        if self.max_time_seconds is not None and self.max_time_seconds <= 0:
            error(self._parse_errors.get("time", "budget.time must be a positive duration"))

        # Validate per-iteration budget time
        if self.iteration_time_seconds is not None and self.iteration_time_seconds <= 0:
            error(self._parse_errors.get(
                "iteration_time",
                "budget.iteration_time must be a positive duration",
            ))

        # Editable and frozen are mutually exclusive
        if self.editable and self.frozen:
            error(
                "Cannot set both scope.editable and scope.frozen — "
                "use editable (whitelist) or frozen (blacklist), not both"
            )

        return issues

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        """Create config from dictionary."""
        goal = data.get("goal", "")

        # Handle single constraint or list; each entry can be a string or dict.
        # `or []` guards a present-but-empty `constraints:` key (YAML -> None).
        raw_constraints = data.get("constraints") or []
        if isinstance(raw_constraints, str):
            raw_constraints = [raw_constraints]
        if "constraint" in data:
            c = data["constraint"]
            raw_constraints = [c] if isinstance(c, str) else c

        constraints: list[ConstraintCommand] = []
        for entry in raw_constraints:
            if isinstance(entry, str):
                constraints.append(ConstraintCommand(command=entry))
            elif isinstance(entry, dict):
                constraints.append(ConstraintCommand(
                    command=entry.get("command", ""),
                    timeout=int(entry.get("timeout", DEFAULT_CHECK_TIMEOUT)),
                ))

        # Parse metrics (canonical key; `observables:` is an accepted alias for
        # the same thing — see the config docs for the two-level model).
        observables: list[ObservableCommand] = []
        raw_observables = data.get("metrics")
        if raw_observables is None:
            raw_observables = data.get("observables") or []
        if isinstance(raw_observables, list):
            for entry in raw_observables:
                if isinstance(entry, dict):
                    command = entry.get("command", "")
                    raw_values = entry.get("values", {})
                    values: dict[str, str] = {}
                    if isinstance(raw_values, dict):
                        for name, direction in raw_values.items():
                            values[name] = direction if isinstance(direction, str) else "higher"
                    capture = entry.get("capture", [])
                    if not isinstance(capture, list):
                        capture = []
                    capture_stdout = entry.get("capture_stdout", True)
                    timeout = int(entry.get("timeout", DEFAULT_CHECK_TIMEOUT))
                    observables.append(ObservableCommand(
                        command=command,
                        values=values,
                        capture=capture,
                        capture_stdout=bool(capture_stdout),
                        timeout=timeout,
                    ))

        # Parse scope (`or {}` guards a present-but-empty `scope:` key)
        scope = data.get("scope") or {}
        editable = scope.get("editable", [])
        frozen = scope.get("frozen", [])

        agent = AgentConfig.from_dict(data.get("agent"))

        # Parse acceptance (`or {}` guards a present-but-empty `acceptance:` key)
        raw_acceptance = data.get("acceptance") or {}
        metric_gates = []
        for gate in raw_acceptance.get("metric_gates", []):
            if isinstance(gate, dict) and "metric" in gate:
                metric_gates.append(MetricGate(
                    metric=gate["metric"],
                    max_regression_pct=float(gate.get("max_regression_pct", 10)),
                ))
        acceptance = AcceptanceConfig(metric_gates=metric_gates)

        # Parse budget (`or {}` guards a present-but-empty `budget:` key)
        budget = data.get("budget") or {}
        max_iterations = budget.get("iterations", 5)

        iteration_time_seconds = None
        iteration_time_parse_error: str | None = None
        raw_iteration_time = budget.get("iteration_time")
        if raw_iteration_time is not None:
            try:
                iteration_time_seconds = parse_iteration_duration(raw_iteration_time)
            except (ValueError, OverflowError) as e:
                iteration_time_seconds = -1  # will fail validation
                iteration_time_parse_error = (
                    f"budget.iteration_time: '{raw_iteration_time}' "
                    f"is not a valid duration ({e})"
                )

        max_cost_usd = None
        cost_parse_error: str | None = None
        raw_cost = budget.get("cost")
        if raw_cost is not None:
            if isinstance(raw_cost, bool):
                max_cost_usd = -1
                cost_parse_error = f"budget.cost: expected a number, got bool ({raw_cost})"
            else:
                try:
                    max_cost_usd = float(raw_cost)
                except (TypeError, ValueError):
                    max_cost_usd = -1  # will fail validation
                    cost_parse_error = f"budget.cost: '{raw_cost}' is not a valid number"

        max_time_seconds = None
        time_parse_error: str | None = None
        raw_time = budget.get("time")
        if raw_time is not None:
            try:
                max_time_seconds = parse_duration(raw_time)
            except (ValueError, OverflowError) as e:
                max_time_seconds = -1  # will fail validation
                time_parse_error = f"budget.time: '{raw_time}' is not a valid duration ({e})"

        parse_errors: dict[str, str] = {}
        if cost_parse_error:
            parse_errors["cost"] = cost_parse_error
        if time_parse_error:
            parse_errors["time"] = time_parse_error
        if iteration_time_parse_error:
            parse_errors["iteration_time"] = iteration_time_parse_error

        # Parse reviewer
        reviewer = None
        raw_reviewer = data.get("reviewer")
        if raw_reviewer is True:
            # Just "reviewer: true" - use defaults
            reviewer = ReviewerConfig()
        elif isinstance(raw_reviewer, dict):
            auto_memory = raw_reviewer.get("auto_memory", False)
            reviewer = ReviewerConfig(
                prompt=raw_reviewer.get("prompt", DEFAULT_REVIEWER_PROMPT),
                model=raw_reviewer.get("model"),
                timeout_seconds=raw_reviewer.get("timeout_seconds"),
                auto_memory=bool(auto_memory),
            )

        return cls(
            goal=goal,
            constraints=constraints,
            observables=observables,
            agent=agent,
            acceptance=acceptance,
            reviewer=reviewer,
            editable=editable,
            frozen=frozen,
            max_iterations=max_iterations,
            max_cost_usd=max_cost_usd,
            max_time_seconds=max_time_seconds,
            iteration_time_seconds=iteration_time_seconds,
            _parse_errors=parse_errors,
        )


def load_config(project_path: Path, config_file: Path | None = None) -> tuple[Config, dict[str, Any]]:
    """Load configuration from project's autohelix.yaml.

    Args:
        project_path: The project directory.
        config_file: Optional path to a config file. Defaults to autohelix.yaml
                     in project_path.

    Returns:
        A tuple of (Config, raw_data) where raw_data is the parsed YAML dict.
        The raw_data is used for unknown-key validation.
    """
    config_path = Path(config_file) if config_file else project_path / "autohelix.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(config_path) as f:
        data = yaml.safe_load(f) or {}

    return Config.from_dict(data), data
