# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run gate, observe and stop commands; compare metric gates."""

import glob as globmod
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

from autohelix.agents.runner import _kill_process_group
from autohelix.config import DEFAULT_CHECK_TIMEOUT, CommandSpec, Config, MetricGate, ObserveEntry
from autohelix.sandbox import build_worktree_env

# Progress callback type: (phase, command, elapsed_seconds | None)
ProgressCallback = Callable[[str, str, float | None], None]


@dataclass
class PreflightIssue:
    """An issue found during preflight checks."""

    level: str  # "error" or "warning"
    message: str


def _find_gate_targets(commands, project_path: Path) -> list[str]:
    """Extract file/directory paths from gate commands that exist on disk.

    Looks for tokens in gate commands that correspond to actual files or
    directories in the project — these are what the agent could weaken if no
    scope is configured.
    """
    targets: set[str] = set()
    for c in commands:
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
    agent_type = config.agent.backend.lower()
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
        # Warn that agent can modify gate targets
        exposed = _find_gate_targets(config.gate_commands(), project_path)
        if exposed:
            targets = ", ".join(sorted(exposed))
            issues.append(PreflightIssue(
                level="warning",
                message=(
                    f"No scope set — agent can modify gate targets ({targets}). "
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
class CommandResult:
    """Result of running a gate or stop command."""

    command: str
    passed: bool
    output: str
    return_code: int
    stdout: str = ""


# A check command may run its own agent (e.g. `autohelix ask`), which needs up to
# the agent runner's grace (3 s) plus 2 s to stop it on SIGTERM; wait longer.
CHECK_KILL_GRACE = 8


def _command_env(project_path: Path | None, cwd: Path, timeout: int,
                 extra_env: dict[str, str] | None) -> dict[str, str]:
    """Environment for a check command: sys.path remapped to the copy, harness-provided
    variables, and the command's deadline (for tools that run their own agents)."""
    env = build_worktree_env(project_path, cwd) if project_path else os.environ.copy()
    env.update(extra_env or {})
    env["AUTOHELIX_COMMAND_DEADLINE"] = str(time.time() + timeout)
    return env


def run_command(
    command: str,
    cwd: Path,
    timeout: int = DEFAULT_CHECK_TIMEOUT,
    project_path: Path | None = None,
    on_progress: ProgressCallback | None = None,
    extra_env: dict[str, str] | None = None,
) -> CommandResult:
    """Run a single command."""
    env = _command_env(project_path, cwd, timeout, extra_env)

    start = time.monotonic()
    if on_progress:
        on_progress("gate", command, 0.0)

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
                    on_progress("gate", command, elapsed)
                if elapsed > timeout:
                    # Kill the whole process group, not just the /bin/sh wrapper,
                    # so a timed-out pytest/benchmark doesn't keep running detached.
                    _kill_process_group(proc, grace=CHECK_KILL_GRACE)
                    proc.communicate()
                    return CommandResult(
                        command=command,
                        passed=False,
                        output=f"Timeout after {timeout}s",
                        return_code=-1,
                    )

        return CommandResult(
            command=command,
            passed=proc.returncode == 0,
            output=stdout + stderr,
            return_code=proc.returncode,
            stdout=stdout,
        )
    except KeyboardInterrupt:
        if proc is not None:
            _kill_process_group(proc, grace=CHECK_KILL_GRACE)
        raise
    except Exception as e:
        return CommandResult(
            command=command,
            passed=False,
            output=str(e),
            return_code=-1,
        )


def run_gate_commands(
    commands: list[CommandSpec],
    cwd: Path,
    project_path: Path | None = None,
    on_progress: ProgressCallback | None = None,
    extra_env: dict[str, str] | None = None,
    after_command: Callable[[str], None] | None = None,
) -> list[CommandResult]:
    """Run gate commands in order and return results. `after_command` runs after each
    command (e.g. to verify the candidate) and may raise to stop the phase."""
    results = []
    for c in commands:
        results.append(run_command(
            c.command, cwd, timeout=c.timeout, project_path=project_path, on_progress=on_progress,
            extra_env=extra_env,
        ))
        if after_command:
            after_command(c.command)
    return results


@dataclass
class StopCheckResult:
    """Outcome of one stop command: exit 0 stops the run, 1 continues, anything
    else (including a timeout) is an error that also continues."""

    command: str
    return_code: int
    output: str
    reason: str  # the last non-empty stdout line, or the command itself

    @property
    def stop(self) -> bool:
        return self.return_code == 0

    @property
    def error(self) -> bool:
        return self.return_code not in (0, 1)


def run_stop_checks(
    config: Config,
    cwd: Path,
    project_path: Path | None = None,
    extra_env: dict[str, str] | None = None,
    on_progress: ProgressCallback | None = None,
) -> list[StopCheckResult]:
    """Run stop commands in order until one exits 0; return the results of those run."""
    results: list[StopCheckResult] = []
    for c in config.stop:
        r = run_command(
            c.command, cwd, timeout=c.timeout, project_path=project_path,
            on_progress=on_progress, extra_env=extra_env,
        )
        lines = [line.strip() for line in r.stdout.splitlines() if line.strip()]
        results.append(StopCheckResult(
            command=c.command, return_code=r.return_code, output=r.output,
            reason=lines[-1] if lines else c.command,
        ))
        if r.return_code == 0:
            break
    return results


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
class ObserveResult:
    """Result of running an observe entry."""

    command: str
    values: dict[str, float]  # parsed metric name -> value
    output: str
    errors: dict[str, str]  # metric name -> error message for values not found


def run_observe_entry(
    mc: ObserveEntry,
    cwd: Path,
    timeout: int | None = None,
    project_path: Path | None = None,
    on_progress: ProgressCallback | None = None,
    extra_env: dict[str, str] | None = None,
) -> ObserveResult:
    """Run an observe entry and extract its declared metric values from the output.
    A review entry's numbers count only from `##autohelix[name=value]` lines."""
    timeout = timeout if timeout is not None else mc.timeout

    env = _command_env(project_path, cwd, timeout, extra_env)

    start = time.monotonic()
    if on_progress:
        on_progress("observe", mc.command, 0.0)

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
                    on_progress("observe", mc.command, elapsed)
                if elapsed > timeout:
                    # Kill the whole process group (see run_command).
                    _kill_process_group(proc, grace=CHECK_KILL_GRACE)
                    proc.communicate()
                    errors = {name: f"Timeout after {timeout}s" for name in mc.metrics}
                    return ObserveResult(
                        command=mc.command, values={}, output="", errors=errors,
                    )

        output = stdout + stderr

        values: dict[str, float] = {}
        errors: dict[str, str] = {}

        # The unnamed-metric fallback is only safe when a single metric is
        # declared; with several, it would copy the lone emitted value into
        # every declared name (see parse_metric_output).
        allow_unnamed_fallback = len(mc.metrics) == 1
        structured = parse_structured_metrics(stdout)
        for name in mc.metrics:
            if mc.review is not None:  # the review's prose can't carry a number
                value = structured.get(name)
            else:
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

        return ObserveResult(
            command=mc.command,
            values=values,
            output=output,
            errors=errors,
        )
    except KeyboardInterrupt:
        if proc is not None:
            _kill_process_group(proc, grace=CHECK_KILL_GRACE)
        raise
    except Exception as e:
        errors = {name: str(e) for name in mc.metrics}
        return ObserveResult(
            command=mc.command, values={}, output="", errors=errors,
        )


def run_observe(
    entries: list[ObserveEntry],
    cwd: Path,
    project_path: Path | None = None,
    on_progress: ProgressCallback | None = None,
    extra_env: dict[str, str] | None = None,
    after_command: Callable[[str], None] | None = None,
) -> list[ObserveResult]:
    """Run observe entries in order and return full results (values + output)."""
    results: list[ObserveResult] = []
    for obs in entries:
        cmd_result = run_observe_entry(obs, cwd, project_path=project_path, on_progress=on_progress,
                                    extra_env=extra_env)
        results.append(cmd_result)
        if after_command:
            after_command(obs.command)
        for name, error in cmd_result.errors.items():
            logger.warning("Metric '%s' failed: %s", name, error)
    return results


def metric_reference(records, metric: str) -> float | None:
    """The value a metric gate compares with: the metric as measured on the run's tip
    commit, by the accepted iteration that created it or the baseline. An accepted
    no-op doesn't re-measure it. If that record lacks the metric (a gate added later),
    the most recent commit-creating accepted record (or the baseline) that has it."""
    for record in reversed(records):
        if record.accepted and (record.commit or record.iteration == 0) and metric in record.metrics:
            return record.metrics[metric]
    return None


def metric_gate_failure(gate: MetricGate, direction: str, value: float | None,
                        reference: float | None) -> str | None:
    """Why a metric gate rejects `value` against `reference`, or None if it passes.

    Percentages are of |reference|: with a reference of 0, `regress:` means no worse
    than 0 and `improve:` any strict improvement. A missing value fails; a missing
    reference (nothing measured it yet) passes."""
    if value is None:
        return f"metric gate: {gate.metric} was not measured"
    if reference is None:
        return None
    gain = value - reference if direction == "higher" else reference - value  # > 0: better
    margin = abs(reference) * gate.threshold_pct / 100
    slack = 1e-9 * max(abs(reference), abs(value))  # float noise at exactly the threshold
    pct = f"{abs(gain) / abs(reference) * 100:.1f}%" if reference else f"{abs(gain):g}"
    if gate.mode == "regress":
        if -gain > margin + slack:
            return (f"metric gate: {gate.metric} {value:g} is {pct} worse than {reference:g} "
                    f"(regress: {gate.threshold_pct:g}% allowed)")
        return None
    if gain <= 0:
        return f"metric gate: {gate.metric} {value:g} does not improve on {reference:g}"
    if gain < margin - slack:
        return (f"metric gate: {gate.metric} {value:g} improves on {reference:g} by only {pct} "
                f"(improve: {gate.threshold_pct:g}% needed)")
    return None
