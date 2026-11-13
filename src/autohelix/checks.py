# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run gate, observe and stop commands; compare metric gates."""

import glob as globmod
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
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


def scope_warning(config: Config) -> str | None:
    """The warning for a run with gate or observe commands and no scope: the agent may
    then edit the tests and benchmarks that judge it. None otherwise."""
    if config.editable or config.frozen or not (config.gate_commands() or config.observe):
        return None
    return ("No scope set: the agent may edit the tests and benchmarks that gate it; "
            "set scope.editable (and scope.frozen) to protect them.")


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

    # 3. Scope patterns that match nothing (no scope at all: `scope_warning`, at run start)
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
    stderr: str = ""  # stored apart from stdout (`output` is both); why it couldn't start, if so
    timed_out: bool = False
    duration_s: float | None = None


# A check command may run its own agent (e.g. `autohelix ask`), which needs up to
# the agent runner's grace (3 s) plus 2 s to stop it on SIGTERM; wait longer.
CHECK_KILL_GRACE = 8

# Per-command variables: a context manager given the command, yielding the variables to
# add for that one call (a value of None unsets the variable) and cleaning up after it.
PerCommand = Callable[[str], AbstractContextManager]


def _command_env(project_path: Path | None, cwd: Path, timeout: int,
                 extra_env: dict[str, str | None] | None) -> dict[str, str]:
    """Environment for a check command: sys.path remapped to the copy, harness-provided
    variables (None removes one, so a variable can't leak in from the harness's own
    environment), and the command's deadline (for tools that run their own agents)."""
    env = build_worktree_env(project_path, cwd) if project_path else os.environ.copy()
    for key, value in (extra_env or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    env["AUTOHELIX_COMMAND_DEADLINE"] = str(time.time() + timeout)
    return env


@dataclass
class _Execution:
    return_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None  # could not start
    duration_s: float = 0.0


def _end_group(proc) -> None:
    """SIGKILL whatever is left of a command's process group (its own session, from
    `start_new_session`), also after the shell itself exited: a background child must not
    keep running, and writing files, once the harness has checked them. A child that
    starts a session of its own escapes this (until commands run in a sandbox).

    PID reuse: once the shell is reaped, its PID (the group's id) is free again only
    when no member of the group is left; a new process could then take it as its own
    group id before this kill, a narrow race. Closing it means not reaping the shell
    until after the kill (waitid with WNOWAIT, then reading the pipes ourselves instead
    of `communicate`), which `subprocess` doesn't offer; left as is."""
    if not isinstance(getattr(proc, "pid", None), int):
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:  # nothing left
        pass


def _text(data) -> str:
    """Command output as text: bytes decoded leniently (invalid UTF-8 can't lose it)."""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    return data or ""


def _drain(proc) -> tuple[str, str]:
    """The rest of a killed command's output, bounded: a child that escaped the group
    and holds the pipes can't hang the harness."""
    try:
        stdout, stderr = proc.communicate(timeout=CHECK_KILL_GRACE)
    except subprocess.TimeoutExpired:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        proc.wait()
        return "", ""
    return _text(stdout), _text(stderr)


def _execute(command: str, cwd: Path, timeout: int, env: dict[str, str], phase: str,
             on_progress: ProgressCallback | None) -> _Execution:
    """Run a shell command in its own process group. The whole group is killed when the
    command ends (whatever its background children were doing, and however it ends), at
    the timeout, and on an interrupt. A timed-out command keeps the output it wrote."""
    start = time.monotonic()
    if on_progress:
        on_progress(phase, command, 0.0)

    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
                    on_progress(phase, command, elapsed)
                if elapsed > timeout:
                    # Kill the whole process group, not just the /bin/sh wrapper,
                    # so a timed-out pytest/benchmark doesn't keep running detached.
                    _kill_process_group(proc, grace=CHECK_KILL_GRACE)
                    _end_group(proc)
                    stdout, stderr = _drain(proc)
                    return _Execution(-1, stdout, stderr, timed_out=True, duration_s=time.monotonic() - start)
        return _Execution(proc.returncode, _text(stdout), _text(stderr), duration_s=time.monotonic() - start)
    except KeyboardInterrupt:
        if proc is not None:
            _kill_process_group(proc, grace=CHECK_KILL_GRACE)
        raise
    except Exception as e:
        return _Execution(-1, error=str(e), duration_s=time.monotonic() - start)
    finally:
        if proc is not None:
            _end_group(proc)


def run_command(
    command: str,
    cwd: Path,
    timeout: int = DEFAULT_CHECK_TIMEOUT,
    project_path: Path | None = None,
    on_progress: ProgressCallback | None = None,
    extra_env: dict[str, str | None] | None = None,
) -> CommandResult:
    """Run a single command."""
    env = _command_env(project_path, cwd, timeout, extra_env)
    run = _execute(command, cwd, timeout, env, "gate", on_progress)
    if run.timed_out:
        output = f"Timeout after {timeout}s"
        if run.stdout or run.stderr:
            output += "\n" + run.stdout + run.stderr
        return CommandResult(command=command, passed=False, output=output, return_code=-1,
                             stdout=run.stdout, stderr=run.stderr, timed_out=True, duration_s=run.duration_s)
    if run.error is not None:
        return CommandResult(command=command, passed=False, output=run.error, return_code=-1,
                             stderr=run.error, duration_s=run.duration_s)
    return CommandResult(
        command=command,
        passed=run.return_code == 0,
        output=run.stdout + run.stderr,
        return_code=run.return_code,
        stdout=run.stdout,
        stderr=run.stderr,
        duration_s=run.duration_s,
    )


@contextmanager
def _no_extra(_command: str):
    yield {}


def run_gate_commands(
    commands: list[CommandSpec],
    cwd: Path,
    project_path: Path | None = None,
    on_progress: ProgressCallback | None = None,
    extra_env: dict[str, str | None] | None = None,
    after_command: Callable[[str], None] | None = None,
    stop_on_failure: bool = False,
    per_command: PerCommand | None = None,
    on_result: Callable[[CommandSpec, CommandResult], None] | None = None,
) -> list[CommandResult]:
    """Run gate commands in order and return results; with `stop_on_failure`, the first
    failure ends the phase (later commands don't run). `per_command` adds variables for
    each call (see `PerCommand`); `after_command` runs after each command (e.g. to
    verify the candidate) and may raise to stop the phase; `on_result` sees each
    result as soon as it exists: after the command (and its process group) ended, but
    while its `per_command` context is still open, and before `after_command`."""
    results = []
    for c in commands:
        with (per_command or _no_extra)(c.command) as added:
            result = run_command(
                c.command, cwd, timeout=c.timeout, project_path=project_path, on_progress=on_progress,
                extra_env={**(extra_env or {}), **added},
            )
            results.append(result)
            if on_result:
                on_result(c, result)
        if after_command:
            after_command(c.command)
        if stop_on_failure and not result.passed:
            break
    return results


@dataclass
class StopCheckResult:
    """Outcome of one stop command: exit 0 stops the run, 1 continues, anything
    else (including a timeout) is an error that also continues."""

    command: str
    return_code: int
    output: str
    reason: str  # the last non-empty stdout line (unless just true/false/null), or the command itself

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
    extra_env: dict[str, str | None] | None = None,
    on_progress: ProgressCallback | None = None,
    per_command: PerCommand | None = None,
    on_result: Callable[[CommandSpec, CommandResult], None] | None = None,
) -> list[StopCheckResult]:
    """Run stop commands in order until one exits 0; return the results of those run.
    `per_command` adds variables for each call (see `PerCommand`); `on_result` sees
    each command's result as `run_gate_commands`' does (its context still open)."""
    results: list[StopCheckResult] = []
    for c in config.stop:
        with (per_command or _no_extra)(c.command) as added:
            r = run_command(
                c.command, cwd, timeout=c.timeout, project_path=project_path,
                on_progress=on_progress, extra_env={**(extra_env or {}), **added},
            )
            if on_result:
                on_result(c, r)
        lines = [line.strip() for line in r.stdout.splitlines() if line.strip()]
        # `jq -e` prints the value it tested: true/false/null says nothing, the command does.
        last = lines[-1] if lines else None
        results.append(StopCheckResult(
            command=c.command, return_code=r.return_code, output=r.output,
            reason=last if last and last not in ("true", "false", "null") else c.command,
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
    for match in re.finditer(r"##autohelix\[(\w+)=\s*([0-9.eE+-]+)\s*\]", output):
        try:
            value = float(match.group(2))
        except ValueError:
            continue
        if math.isfinite(value):  # 1e999 is not a measurement; treat it as not produced
            results[match.group(1)] = value
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

    # A structured line for this name that isn't a finite number reports nothing; the
    # name match below would read "1" out of "##autohelix[speed=1e999]".
    if metric_name and f"##autohelix[{metric_name}=" in output:
        return None

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
    """Result of running an observe entry. A failed entry (non-zero exit, timeout, could
    not run; a review that left a declared number out) contributes no values."""

    command: str
    values: dict[str, float]  # parsed metric name -> value
    output: str
    errors: dict[str, str]  # metric name -> error message for values not found
    name: str = ""
    ok: bool = True
    return_code: int = 0
    error: str | None = None  # why the entry failed: "exited 2", "timed out after 30m", ...
    duration_s: float | None = None
    stdout: str = ""  # stored as stdout.txt
    stderr: str = ""  # stored as stderr.txt (when not empty); why it couldn't start, if so

    def record(self) -> dict:
        """The entry as the history record lists it (`IterationResult.observe`)."""
        record = {"name": self.name, "ok": self.ok, "return_code": self.return_code, "error": self.error,
                  "metrics": dict(self.values),
                  "duration_s": None if self.duration_s is None else round(self.duration_s, 3)}
        if self.ok and self.errors:  # it ran, but some declared metric couldn't be read: say how to fix it
            record["metric_errors"] = dict(self.errors)
        return record


def run_observe_entry(
    mc: ObserveEntry,
    cwd: Path,
    timeout: int | None = None,
    project_path: Path | None = None,
    on_progress: ProgressCallback | None = None,
    extra_env: dict[str, str | None] | None = None,
) -> ObserveResult:
    """Run an observe entry and extract its declared metric values from the output.
    A review entry's numbers count only from `##autohelix[name=value]` lines."""
    from autohelix.iteration_time import format_duration

    timeout = timeout if timeout is not None else mc.timeout
    env = _command_env(project_path, cwd, timeout, extra_env)
    run = _execute(mc.command, cwd, timeout, env, "observe", on_progress)
    output = run.stdout + run.stderr

    def failed(error: str, return_code: int) -> ObserveResult:
        return ObserveResult(
            command=mc.command, values={}, output=output if run.error is None else run.error,
            errors={name: f"observe entry {mc.name or mc.command!r} failed ({error})" for name in mc.metrics},
            name=mc.name, ok=False, return_code=return_code, error=error, duration_s=run.duration_s,
            stdout=run.stdout, stderr=run.stderr if run.error is None else run.error,
        )

    if run.timed_out:
        return failed(f"timed out after {format_duration(int(timeout))}", -1)
    if run.error is not None:
        return failed(f"could not run: {run.error}", -1)
    if run.return_code != 0:
        return failed(f"exited {run.return_code}", run.return_code)

    values: dict[str, float] = {}
    errors: dict[str, str] = {}
    # The unnamed-metric fallback is only safe when a single metric is
    # declared; with several, it would copy the lone emitted value into
    # every declared name (see parse_metric_output).
    allow_unnamed_fallback = len(mc.metrics) == 1
    structured = parse_structured_metrics(run.stdout)
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
    if mc.review is not None and errors:  # a review that leaves a declared number out fails
        return failed(f"did not report {', '.join(errors)}", run.return_code)
    return ObserveResult(command=mc.command, values=values, output=output, errors=errors,
                         name=mc.name, return_code=run.return_code, duration_s=run.duration_s,
                         stdout=run.stdout, stderr=run.stderr)


def metric_reference(records, metric: str) -> float | None:
    """The value a metric gate compares with: the metric as measured on the run's tip
    commit, by the accepted iteration that created it or the baseline. An accepted
    no-op doesn't re-measure it. If that record lacks the metric (a gate added later),
    the most recent commit-creating accepted record (or the baseline) that has it."""
    for record in reversed(records):
        value = record.metrics.get(metric)
        if (record.accepted and (record.commit or record.iteration == 0) and isinstance(value, (int, float))
                and math.isfinite(value)):
            return value
    return None


def metric_gate_failure(gate: MetricGate, direction: str, value: float | None,
                        reference: float | None) -> str | None:
    """Why a metric gate rejects `value` against `reference`, or None if it passes.

    Percentages are of |reference|: with a reference of 0, `regress:` means no worse
    than 0 and `improve:` any strict improvement. A missing value fails; a missing
    reference (nothing measured it yet) passes; a non-finite value fails. Non-finite
    references are skipped by `metric_reference`."""
    if value is None:
        return f"metric gate: {gate.metric} was not measured"
    if not math.isfinite(value):
        return f"metric gate: {gate.metric} is not a finite number ({value})"
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
