# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run gate, observe and stop commands; compare metric gates."""

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

from autohelix.agents.runner import _kill_process_group, end_group
from autohelix.agents import command_path
from autohelix.config import DEFAULT_CHECK_TIMEOUT, CommandSpec, Config, MetricGate, ObserveEntry
from autohelix.sandbox import build_worktree_env
from autohelix.workspace import git_env, path_in_scope, run_git

# Progress callback type: (phase, command, elapsed_seconds | None)


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


def preflight_check(config: Config, project_path: Path) -> list[PreflightIssue]:
    """Run preflight validation checks on a project.

    Checks config validity, agent availability, and scope patterns.
    Returns a list of issues found (empty means all OK).
    """
    issues: list[PreflightIssue] = []

    # 1. Config validation
    config_issues = config.validate()
    for ci in config_issues:
        issues.append(PreflightIssue(level=ci.level, message=ci.message))

    # 2. Every agent the config runs (main agent, `ask:` and `review:` judges) can start:
    # its command resolved as the backend resolves it (mock is built in).
    missing: dict[str, list[str]] = {}
    for who, backend, command in config.agent_commands():
        if backend != "mock" and not shutil.which(path := command_path(backend, command)):
            missing.setdefault(f"{backend}\0{path}", []).append(who)
    for key, whos in missing.items():
        backend, path = key.split("\0")
        users = "" if whos == ["agent"] else " (used by " + ", ".join(
            "the agent" if who == "agent" else f"`{who}`" for who in whos) + ")"
        issues.append(PreflightIssue(
            level="error",
            message=(f"Agent command '{path}' not found in PATH{users}; install {backend}, or set "
                     f"agent.command or AUTOHELIX_{backend.upper()}_CMD"),
        ))

    # 3. Scope patterns that match no committed file, with the matcher the harness uses
    # (no scope at all: `scope_warning`, at run start). Editable ones may name files the
    # agent is to create, so warnings only.
    if config.editable or config.frozen:
        listed = run_git(["git", "ls-files", "-z"], cwd=project_path, capture_output=True, env=git_env())
        files = os.fsdecode(listed.stdout).split("\0") if listed.returncode == 0 else []
        for label, patterns in [("editable", config.editable), ("frozen", config.frozen)]:
            for pattern in patterns:
                if not any(path_in_scope(path, [pattern]) for path in files if path):
                    issues.append(PreflightIssue(
                        level="warning",
                        message=f"scope.{label} '{pattern}' matches no committed file",
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


def _execute(command: str, cwd: Path, timeout: int, env: dict[str, str]) -> _Execution:
    """Run a shell command in its own process group, with no input (stdin is /dev/null,
    also when the harness runs in a terminal). The whole group is killed when the
    command ends (whatever its background children were doing, and however it ends), at
    the timeout, and on an interrupt. A timed-out command keeps the output it wrote."""
    start = time.monotonic()
    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,  # never the user's terminal: no prompts, no TTY size
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
                if elapsed > timeout:
                    # Kill the whole process group, not just the /bin/sh wrapper,
                    # so a timed-out pytest/benchmark doesn't keep running detached.
                    _kill_process_group(proc, grace=CHECK_KILL_GRACE)
                    end_group(proc)
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
            end_group(proc)


def run_command(
    command: str,
    cwd: Path,
    timeout: int = DEFAULT_CHECK_TIMEOUT,
    project_path: Path | None = None,
    extra_env: dict[str, str | None] | None = None,
) -> CommandResult:
    """Run a single command."""
    env = _command_env(project_path, cwd, timeout, extra_env)
    run = _execute(command, cwd, timeout, env)
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
                c.command, cwd, timeout=c.timeout, project_path=project_path,
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
    timed_out: bool = False
    duration_s: float | None = None

    @classmethod
    def of(cls, spec: CommandSpec, r: "CommandResult") -> "StopCheckResult":
        lines = [line.strip() for line in r.stdout.splitlines() if line.strip()]
        # `jq -e` prints the value it tested: true/false/null says nothing, the command does.
        last = lines[-1] if lines else None
        return cls(command=spec.command, return_code=r.return_code, output=r.output,
                   reason=last if last and last not in ("true", "false", "null") else spec.command,
                   timed_out=r.timed_out, duration_s=r.duration_s)

    @property
    def stop(self) -> bool:
        return self.return_code == 0 and not self.timed_out

    @property
    def error(self) -> bool:
        return self.timed_out or self.return_code not in (0, 1)

    @property
    def outcome(self) -> str:
        """"stop", "continue" or "error", as the iteration record has it."""
        return "stop" if self.stop else "error" if self.error else "continue"

    def problem(self) -> str:
        """An error's kind, for messages: `timed out`, `could not run`, `exited 2`."""
        return ("timed out" if self.timed_out else "could not run" if self.return_code == -1
                else f"exited {self.return_code}")

    def record(self, text: str) -> dict:
        """The record's item for this command (`IterationResult.stops`), like a gate's."""
        item = {"text": text, "outcome": self.outcome, "return_code": self.return_code,
                "duration_s": None if self.duration_s is None else round(self.duration_s, 3)}
        if self.timed_out:
            item["timed_out"] = True
        return item


def run_stop_checks(
    config: Config,
    cwd: Path,
    project_path: Path | None = None,
    extra_env: dict[str, str | None] | None = None,
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
                extra_env={**(extra_env or {}), **added},
            )
            if on_result:
                on_result(c, r)
        results.append(StopCheckResult.of(c, r))
        if results[-1].stop:
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
    extra_env: dict[str, str | None] | None = None,
) -> ObserveResult:
    """Run an observe entry and extract its declared metric values from the output.
    A review entry's numbers count only from `##autohelix[name=value]` lines."""
    from autohelix.iteration_time import format_limit

    timeout = timeout if timeout is not None else mc.timeout
    env = _command_env(project_path, cwd, timeout, extra_env)
    run = _execute(mc.command, cwd, timeout, env)
    output = run.stdout + run.stderr

    def failed(error: str, return_code: int) -> ObserveResult:
        return ObserveResult(
            command=mc.command, values={}, output=output if run.error is None else run.error,
            errors={name: f"observe entry {mc.name or mc.command!r} failed ({error})" for name in mc.metrics},
            name=mc.name, ok=False, return_code=return_code, error=error, duration_s=run.duration_s,
            stdout=run.stdout, stderr=run.stderr if run.error is None else run.error,
        )

    if run.timed_out:
        return failed(f"timed out after {format_limit(int(timeout))}", -1)
    if run.error is not None:
        return failed(f"could not run: {run.error}", -1)
    if run.return_code != 0:
        return failed(f"exited {run.return_code}", run.return_code)

    # Only `##autohelix[name=value]` lines on stdout count, matched by name: a
    # `name: value` line or a structured line under another name is not the metric.
    values: dict[str, float] = {}
    errors: dict[str, str] = {}
    structured = parse_structured_metrics(run.stdout)
    others = sorted(set(structured) - set(mc.metrics))
    for name in mc.metrics:
        if name in structured:
            values[name] = structured[name]
        else:
            errors[name] = (
                f"Could not read metric {name!r}: print '##autohelix[{name}=<number>]' on stdout"
                + (f" (it reported {', '.join(others)})" if others else "")
                + "."
            )
    if mc.review is not None and errors:  # a review that leaves a declared number out fails
        return failed(f"did not report {', '.join(errors)}", run.return_code)
    return ObserveResult(command=mc.command, values=values, output=output, errors=errors,
                         name=mc.name, return_code=run.return_code, duration_s=run.duration_s,
                         stdout=run.stdout, stderr=run.stderr)


def metric_reference(records, metric: str) -> float | None:
    """The value a metric gate compares with: the metric as measured on the run's tip
    commit, by the accepted iteration that created it or the baseline (an accepted
    no-op doesn't re-measure it); None if that record has no finite value for it."""
    for record in reversed(records):
        if record.accepted and (record.commit or record.iteration == 0):
            value = record.metrics.get(metric)
            return value if isinstance(value, (int, float)) and math.isfinite(value) else None
    return None


def metric_gate_failure(gate: MetricGate, direction: str, value: float | None,
                        reference: float | None) -> str | None:
    """Why a metric gate rejects `value` against `reference`, or None if it passes.

    Percentages are of |reference|: with a reference of 0, `regress:` means no worse
    than 0 and `improve:` any strict improvement. A missing value fails; a missing
    reference (nothing measured it yet) passes; a non-finite value fails. Non-finite
    references are None (`metric_reference`)."""
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
