# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run constraints and observables."""

import glob as globmod
import logging
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

from autohelix.agents.runner import _kill_process_group
from autohelix.config import DEFAULT_CHECK_TIMEOUT, Config, ObservableCommand
from autohelix.sandbox import build_worktree_env

# Progress callback type: (phase, command, elapsed_seconds | None)
ProgressCallback = Callable[[str, str, float | None], None]


@dataclass
class PreflightIssue:
    """An issue found during preflight checks."""

    level: str  # "error" or "warning"
    message: str


def _find_constraint_targets(constraints, project_path: Path) -> list[str]:
    """Extract file/directory paths from constraint commands that exist on disk.

    Looks for tokens in constraint commands that correspond to actual files or
    directories in the project — these are what the agent could weaken if no
    scope is configured.
    """
    targets: set[str] = set()
    for c in constraints:
        cmd = c.command if hasattr(c, "command") else c
        tokens = cmd.split()
        for token in tokens[1:]:
            if token.startswith("-"):
                continue
            candidate = project_path / token
            if candidate.exists():
                targets.add(token)
    return sorted(targets)


def preflight_check(
    config: Config, raw_config: dict, project_path: Path
) -> list[PreflightIssue]:
    """Run preflight validation checks on a project.

    Checks config validity, agent availability, and scope patterns.
    Returns a list of issues found (empty means all OK).
    """
    issues: list[PreflightIssue] = []

    # 1. Config validation
    config_issues = config.validate(raw_data=raw_config)
    for ci in config_issues:
        issues.append(PreflightIssue(level=ci.level, message=ci.message))

    # 2. Check agent command exists (skip for mock agent which is built-in)
    agent_type = config.agent.type.lower()
    if agent_type != "mock":
        default_cmds = {"claude": "claude", "codex": "codex", "opencode": "opencode"}
        agent_cmd = config.agent.command or default_cmds.get(agent_type, agent_type)
        if not shutil.which(agent_cmd):
            issues.append(PreflightIssue(
                level="error",
                message=f"Agent command '{agent_cmd}' not found in PATH",
            ))

    # 3. Check scope configuration
    if not config.editable and not config.frozen:
        # Warn that agent can modify constraint targets
        exposed = _find_constraint_targets(config.constraints, project_path)
        if exposed:
            targets = ", ".join(sorted(exposed))
            issues.append(PreflightIssue(
                level="warning",
                message=(
                    f"No scope set — agent can modify constraint targets ({targets}). "
                    f"Consider adding scope.editable to protect tests and benchmarks."
                ),
            ))

    for label, patterns in [("editable", config.editable), ("frozen", config.frozen)]:
        for pattern in patterns:
            matches = globmod.glob(str(project_path / pattern), recursive=True)
            if not matches:
                issues.append(PreflightIssue(
                    level="warning",
                    message=f"scope.{label} '{pattern}' matches no files",
                ))

    return issues


@dataclass
class ConstraintResult:
    """Result of running a constraint."""

    command: str
    passed: bool
    output: str
    return_code: int




def run_constraint(
    command: str,
    cwd: Path,
    timeout: int = DEFAULT_CHECK_TIMEOUT,
    project_path: Path | None = None,
    on_progress: ProgressCallback | None = None,
) -> ConstraintResult:
    """Run a single constraint command."""
    env = build_worktree_env(project_path, cwd) if project_path else None

    start = time.monotonic()
    if on_progress:
        on_progress("constraint", command, 0.0)

    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=2.0)
                break
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - start
                if on_progress:
                    on_progress("constraint", command, elapsed)
                if elapsed > timeout:
                    # Kill the whole process group, not just the /bin/sh wrapper,
                    # so a timed-out pytest/benchmark doesn't keep running detached.
                    _kill_process_group(proc)
                    proc.communicate()
                    return ConstraintResult(
                        command=command,
                        passed=False,
                        output=f"Timeout after {timeout}s",
                        return_code=-1,
                    )

        return ConstraintResult(
            command=command,
            passed=proc.returncode == 0,
            output=stdout + stderr,
            return_code=proc.returncode,
        )
    except KeyboardInterrupt:
        if proc is not None:
            _kill_process_group(proc)
        raise
    except Exception as e:
        return ConstraintResult(
            command=command,
            passed=False,
            output=str(e),
            return_code=-1,
        )


def run_constraints(
    config: Config,
    cwd: Path,
    project_path: Path | None = None,
    on_progress: ProgressCallback | None = None,
) -> list[ConstraintResult]:
    """Run all constraints and return results."""
    return [
        run_constraint(
            c.command, cwd, timeout=c.timeout, project_path=project_path, on_progress=on_progress
        )
        for c in config.constraints
    ]


def parse_structured_metrics(output: str) -> dict[str, float]:
    """Parse structured metric lines from command output.

    Looks for lines matching: ##autohelix[key=value]
    This is the recommended way to report metrics from benchmark scripts.
    Multiple metrics can be reported from a single command.

    Returns a dict of metric_name -> value for all structured lines found.
    """
    results = {}
    for match in re.finditer(r"##autohelix\[(\w+)=([0-9.eE+-]+)\]", output):
        try:
            results[match.group(1)] = float(match.group(2))
        except ValueError:
            continue
    return results


def parse_metric_output(
    output: str,
    metric_name: str | None = None,
    allow_unnamed_fallback: bool = True,
) -> float | None:
    """Extract a numeric value from command output.

    Recognized formats (in priority order):
    - ##autohelix[name=1234] (structured protocol, recommended)
    - "<metric_name>: 1234" or "<metric_name>=1234" (if metric_name provided)

    Returns None if no value can be parsed. Metric commands should print
    their result as: ##autohelix[name=value]

    `allow_unnamed_fallback`: when a single structured metric is reported, use it
    regardless of its name. Only safe when exactly one metric is *declared* —
    otherwise the lone emitted value would be fabricated for every declared
    metric (e.g. declaring throughput+latency but emitting only throughput would
    record both as the same number). Callers with multiple declared metrics
    should pass False.
    """
    # First, try the structured ##autohelix[key=value] protocol
    structured = parse_structured_metrics(output)
    if metric_name and metric_name in structured:
        return structured[metric_name]
    # If only one structured metric was reported, use it regardless of name
    if allow_unnamed_fallback and len(structured) == 1:
        return next(iter(structured.values()))

    # Then, try to match the metric name from config (e.g., "throughput: 1234")
    if metric_name:
        pattern = rf"(?:{re.escape(metric_name)})[:\s=]+([0-9.]+)"
        match = re.search(pattern, output, re.MULTILINE | re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass

    return None


@dataclass
class ObservableResult:
    """Result of running an observable command."""

    command: str
    values: dict[str, float]  # parsed metric name -> value
    output: str
    errors: dict[str, str]  # metric name -> error message for values not found


def run_observable(
    mc: ObservableCommand,
    cwd: Path,
    timeout: int | None = None,
    project_path: Path | None = None,
    on_progress: ProgressCallback | None = None,
) -> ObservableResult:
    """Run an observable command and extract all declared metric values from output."""
    timeout = timeout if timeout is not None else mc.timeout

    env = build_worktree_env(project_path, cwd) if project_path else None

    start = time.monotonic()
    if on_progress:
        on_progress("observable", mc.command, 0.0)

    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            mc.command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=2.0)
                break
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - start
                if on_progress:
                    on_progress("observable", mc.command, elapsed)
                if elapsed > timeout:
                    # Kill the whole process group (see run_constraint).
                    _kill_process_group(proc)
                    proc.communicate()
                    errors = {name: f"Timeout after {timeout}s" for name in mc.values}
                    return ObservableResult(
                        command=mc.command, values={}, output="", errors=errors,
                    )

        output = stdout + stderr

        values: dict[str, float] = {}
        errors: dict[str, str] = {}

        # The unnamed-metric fallback is only safe when a single metric is
        # declared; with several, it would copy the lone emitted value into
        # every declared name (see parse_metric_output).
        allow_unnamed_fallback = len(mc.values) == 1
        for name in mc.values:
            value = parse_metric_output(
                output, metric_name=name, allow_unnamed_fallback=allow_unnamed_fallback
            )
            if value is not None:
                values[name] = value
            else:
                errors[name] = (
                    f"Could not parse metric value from output. "
                    f"Recommended: print '##autohelix[{name}=<number>]' from your benchmark script."
                )

        return ObservableResult(
            command=mc.command,
            values=values,
            output=output,
            errors=errors,
        )
    except KeyboardInterrupt:
        if proc is not None:
            _kill_process_group(proc)
        raise
    except Exception as e:
        errors = {name: str(e) for name in mc.values}
        return ObservableResult(
            command=mc.command, values={}, output="", errors=errors,
        )


def run_observables(
    config: Config,
    cwd: Path,
    project_path: Path | None = None,
    on_progress: ProgressCallback | None = None,
) -> list[ObservableResult]:
    """Run all observable commands and return full results (values + output)."""
    results: list[ObservableResult] = []
    for obs in config.observables:
        cmd_result = run_observable(obs, cwd, project_path=project_path, on_progress=on_progress)
        results.append(cmd_result)
        for name, error in cmd_result.errors.items():
            logger.warning("Metric '%s' failed: %s", name, error)
    return results
