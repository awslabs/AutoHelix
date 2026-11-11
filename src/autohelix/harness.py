# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Main iteration loop."""

import json
import hashlib
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.status import Status
from rich.table import Table
from rich.text import Text

from autohelix.agents import AgentConfig, AgentEvent, AgentEventKind, AgentRunResult, create_agent
from autohelix.config import load_config
from autohelix.costs import summarize_costs
from autohelix.display import LiveDisplay
from autohelix.history import History, IterationResult
from autohelix.checkpoint import CandidateChanged, ContentGuard
from autohelix.runs import RunPaths, RunLock, RunState, select_run, reconcile, land, git, allocate_call
from autohelix.iteration import IterationWorkspace
from autohelix.checks import preflight_check, run_constraints, run_observables, run_stop_checks
from autohelix.run_status import RunStatus
from autohelix.usage_log import ENV as USAGE_ENV, read_usage
from autohelix.formatting import format_cost, format_delta, format_duration, format_total_cost
from autohelix.prompt_template import build_prompt_variables, load_template, render_template
from autohelix.run_log import RunLog
from autohelix.sandbox import Sandbox, Worktree
from autohelix.state import (
    GitExcludeError,
    ensure_runtime_ignored,
)


_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class AutoHelixRunError(RuntimeError):
    """A run could not safely continue because infrastructure failed."""


class _IterationSpinner:
    """A Rich renderable showing iteration progress across all phases.

    Displays: ⠹ iter N (phase) ─ detail ─ time
    Phases: agent → checks → benchmark → review
    """

    def __init__(self, iteration: int):
        self._iteration = iteration
        self._phase = "agent"
        self._detail = ""
        self._start = time.monotonic()

    def set_phase(self, phase: str, detail: str = "") -> None:
        self._phase = phase
        self._detail = detail

    def set_detail(self, detail: str) -> None:
        self._detail = detail

    def __rich_console__(self, console, options):
        frame_idx = int(time.monotonic() * 10) % len(_SPINNER_FRAMES)
        frame = _SPINNER_FRAMES[frame_idx]
        elapsed = time.monotonic() - self._start
        secs = int(elapsed)
        if secs >= 60:
            mins = secs // 60
            time_str = f"{mins}m {secs % 60:02d}s"
        else:
            time_str = f"{secs}s"
        detail = self._detail
        if len(detail) > 50:
            detail = detail[:47] + "..."
        if detail:
            text = Text(f"  {frame} iter {self._iteration} ({self._phase}) ─ {detail} ─ {time_str}", style="dim")
        else:
            text = Text(f"  {frame} iter {self._iteration} ({self._phase}) ─ {time_str}", style="dim")
        yield text


def _format_time(seconds: int) -> str:
    """Format seconds into a human-readable duration, omitting zero units.

    e.g. 7205 -> '2h 5s', 120 -> '2m', 45 -> '45s', 0 -> '0s'.
    """
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    parts = []
    if h > 0:
        parts.append(f"{h}h")
    if m > 0:
        parts.append(f"{m}m")
    if s > 0:
        parts.append(f"{s}s")
    return " ".join(parts) if parts else "0s"


def _format_tokens(n: int) -> str:
    """Format token count compactly: 1234 -> '1.2k', 1234567 -> '1.2M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M "
    if n >= 1_000:
        return f"{n / 1_000:.1f}k "
    return f"{n} "



class Harness:
    """Main harness for running the optimization loop."""

    def __init__(self, project_path: Path, verbose: bool = False, heartbeat_seconds: int = 30,
                 config_file: Path | None = None, *, new: bool = False,
                 run_id: str | None = None, name: str | None = None,
                 land_mode: str | None = None):
        self.project_path = Path(project_path).resolve()
        if run_id and config_file is None:
            selected = RunPaths(self.project_path, run_id).read_identity()
            config_file = self.project_path / selected["config_path"]
        self.config_file = config_file
        self.config, self._raw_config = load_config(self.project_path, config_file=config_file)
        self.verbose = verbose
        self.heartbeat_seconds = heartbeat_seconds
        self.console = Console()
        errors = [issue for issue in self.config.validate(self._raw_config) if issue.level == "error"]
        if errors:
            for error in errors:
                self.console.print(f"[red]Error:[/red] {error.message}")
            raise SystemExit("Config validation failed. Edit autohelix.yaml and try again.")
        ensure_runtime_ignored(self.project_path)
        self.land_mode = land_mode or self.config.land
        self.paths, self.run_info, self._ownership = select_run(
            self.project_path, config_file or self.project_path / "autohelix.yaml",
            new=new, run_id=run_id, name=name if name is not None else self.config.name,
        )
        try:
            self.history = History(self.project_path, self.paths.run_id)
            tip, warning = reconcile(self.paths, self.run_info, self.history, self.land_mode)
            self.run_state = RunState(self.paths, self.run_info, tip)
            current = self.paths.root / f"current.{os.getpid()}.tmp"
            current.write_text(self.paths.run_id + "\n")
            current.replace(self.paths.root / "current")
            self.sandbox = Sandbox(self.project_path, self.paths)
            self.run_status = RunStatus(self.project_path, self.paths.run_id)
            self.log = RunLog(self.project_path, self.paths.run_id)
            self._land_attempted = False
            self._segment_started = False
            self._usage_starts = {}
            self._review_logs = {}
            self._agent_logs = {}
            self._reviewer_attempt_usage = {}
            if warning:
                self.console.print(f"[yellow]{warning}[/yellow]")

            self._time_left_script: Path | None = None
            self._claude_settings_path: Path | None = None
            self._codex_time_left_hook_path: Path | None = None
            if self.config.iteration_time_seconds is not None:
                existing = self.config.agent.timeout_seconds
                iteration_budget = self.config.iteration_time_seconds
                self.config.agent.timeout_seconds = (
                    iteration_budget if existing is None else min(existing, iteration_budget)
                )

            # Install optional helper scripts. Claude Code hooks use a shared
            # accumulator, written once to claude_settings.json.
            claude_hooks: dict[str, list] = {}
            if self.config.iteration_time_seconds is not None:
                self._time_left_script = self._install_time_left(claude_hooks)
            if self.config.agent.require_notes is not None:
                self._install_require_notes(claude_hooks)
            self._write_claude_settings(claude_hooks)

            self.agent = create_agent(self.config.agent, heartbeat_seconds=heartbeat_seconds)
            self._validate_codex_iteration_time_ready()

        except BaseException:
            self.close()
            raise

    def _metric_label(self, name: str) -> str:
        """Format metric name with direction arrow."""
        directions = self.config.metric_directions()
        arrow = "↓" if directions.get(name) == "lower" else "↑"
        return f"{name} {arrow}"

    def _build_commit_message(self, iteration: int, metrics: dict[str, float], worktree_path: Path | None = None) -> str:
        """Build a commit message with metric deltas and agent summary."""
        # Read agent's commit summary if available
        summary = None
        if worktree_path:
            summary_file = worktree_path / ".autohelix" / "commit_summary.txt"
            if summary_file.exists():
                summary = summary_file.read_text().strip().split("\n")[0]  # first line only

        if summary:
            lines = [summary, "", f"AutoHelix iteration {iteration}"]
        else:
            lines = [f"AutoHelix iteration {iteration}"]
        if not metrics:
            return lines[0] if not summary else "\n".join(lines)

        best = self.history.get_best_metrics(self.config.metric_directions())
        directions = self.config.metric_directions()
        metric_parts = []
        for name, value in metrics.items():
            arrow = "lower" if directions.get(name) == "lower" else "higher"
            best_entry = best.get(name)
            if best_entry is not None:
                prev_val, _ = best_entry
                if prev_val != 0:
                    if arrow == "lower":
                        pct = (prev_val - value) / prev_val * 100
                    else:
                        pct = (value - prev_val) / prev_val * 100
                    sign = "+" if pct >= 0 else ""
                    metric_parts.append(f"{name}: {prev_val:g} -> {value:g} ({sign}{pct:.1f}%)")
                else:
                    metric_parts.append(f"{name}: {value:g}")
            else:
                metric_parts.append(f"{name}: {value:g}")

        if metric_parts:
            lines.append("")
            lines.extend(metric_parts)

        return "\n".join(lines)

    def _check_metric_gates(self, metrics: dict[str, float]) -> str | None:
        """Check metric gates. Returns rejection reason or None if all pass."""
        gates = self.config.acceptance.metric_gates
        if not gates:
            return None
        directions = self.config.metric_directions()
        best = self.history.get_best_metrics(directions)
        for gate in gates:
            best_entry = best.get(gate.metric)
            new_val = metrics.get(gate.metric)
            if new_val is None:
                return f"metric gate: {gate.metric} not produced by benchmark"
            if best_entry is None:
                continue
            best_val, _ = best_entry
            if best_val == 0:
                continue
            if directions.get(gate.metric) == "lower":
                regression = (new_val - best_val) / best_val
            else:
                regression = (best_val - new_val) / best_val
            if regression * 100 > gate.max_regression_pct:
                pct = regression * 100
                return f"metric gate: {gate.metric} regressed {pct:.1f}% (max allowed {gate.max_regression_pct:.0f}%)"
        return None

    def _print_header(self, max_iter: int, start_iter: int = 1) -> None:
        """Print a compact run header summarizing config."""
        self.console.print()
        from autohelix import __version__
        self.console.print(f"  [bold]AutoHelix[/bold] [dim]v{__version__} · run {self.paths.run_id}[/dim]", highlight=False)
        self.console.print(Rule(style="white"))
        self.console.print()

        # Goal — shown in full, with hanging indent for wrapped lines
        label_width = 11  # "  Goal       " = 13 chars total, label is 11 after "  "
        indent = " " * (label_width + 2)
        goal_text = self.config.goal.strip().replace("\n", " ")
        # Wrap manually: Rich will wrap, but we want the indent prefix on continuation
        max_width = (self.console.width or 100) - len(indent)
        if len(goal_text) <= max_width:
            self.console.print(f"  [dim]Goal[/dim]       {goal_text}", highlight=False)
        else:
            words = goal_text.split()
            lines: list[str] = []
            current = ""
            for word in words:
                if current and len(current) + 1 + len(word) > max_width:
                    lines.append(current)
                    current = word
                else:
                    current = f"{current} {word}" if current else word
            if current:
                lines.append(current)
            self.console.print(f"  [dim]Goal[/dim]       {lines[0]}", highlight=False)
            for line in lines[1:]:
                self.console.print(f"{indent}{line}", highlight=False)

        # Agent
        agent_type = self.config.agent.type
        model = self.config.agent.model or ""
        agent_str = f"{agent_type}" + (f" ({model})" if model else "")
        self.console.print(f"  [dim]Agent[/dim]      {agent_str}", highlight=False)

        # Scope — show editable and frozen if configured
        scope_parts = []
        if self.config.editable:
            scope_parts.append(f"editable: {', '.join(self.config.editable)}")
        if self.config.frozen:
            scope_parts.append(f"frozen: {', '.join(self.config.frozen)}")
        if scope_parts:
            self.console.print(f"  [dim]Scope[/dim]      {' │ '.join(scope_parts)}", highlight=False)

        # Metrics with direction arrows
        directions = self.config.metric_directions()
        if directions:
            metric_parts = []
            for name, direction in directions.items():
                arrow = "↑" if direction == "higher" else "↓"
                metric_parts.append(f"{name} {arrow}")
            self.console.print(f"  [dim]Metrics[/dim]    {', '.join(metric_parts)}", highlight=False)

        # Constraints — short list
        if self.config.constraints:
            if len(self.config.constraints) == 1:
                self.console.print(f"  [dim]Checks[/dim]     {self.config.constraints[0].command}", highlight=False)
            else:
                self.console.print(f"  [dim]Checks[/dim]     {len(self.config.constraints)} constraints", highlight=False)

        # Budget
        budget_parts = [f"{max_iter} iterations"]
        if self.config.iteration_time_seconds is not None:
            budget_parts.append(f"{_format_time(self.config.iteration_time_seconds)}/iter")
        if self.config.max_cost_usd is not None:
            cost = self.config.max_cost_usd
            # Whole dollars show no decimals; sub-dollar caps need cents ($0.10, not $0).
            cost_str = f"${cost:.0f}" if cost == int(cost) else f"${cost:.2f}"
            budget_parts.append(f"{cost_str} max")
        if self.config.max_time_seconds is not None:
            budget_parts.append(f"{_format_time(self.config.max_time_seconds)} total")
        self.console.print(f"  [dim]Budget[/dim]     {', '.join(budget_parts)}", highlight=False)

        # Reviewer — only if configured
        if self.config.reviewer:
            reviewer_model = self.config.reviewer.model or "same as agent"
            self.console.print(f"  [dim]Reviewer[/dim]   {reviewer_model}", highlight=False)

        self.console.print()
        self.console.print(Rule(style="dim"))
        self.console.print()

        # On resume, show prior results so user has context
        if start_iter > 1:
            prior = self.history.load()
            baseline = next((r for r in prior if r.iteration == 0), None)
            if baseline and baseline.metrics:
                metric_str = ", ".join(f"{n}: {v:g}" for n, v in baseline.metrics.items())
                self.console.print(f"  baseline │ {metric_str}", highlight=False)
            for r in sorted((r for r in prior if r.iteration > 0), key=lambda r: r.iteration):
                self._print_iteration_summary(r)
            self.console.print()

    def _status(self, message: str) -> None:
        """Print a status message."""
        self.console.print(f"[dim]{message}[/dim]")

    def _copy_bin(self, name: str) -> Path | None:
        """Copy a helper script from the package bin/ into .autohelix/runtime/bin/.

        Returns the installed (executable) path, or None if the source is missing
        or the copy fails.
        """
        src = Path(__file__).parent / "bin" / name
        if not src.exists():
            return None
        dst_dir = self.paths.project_runtime / "bin"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / name
        try:
            temporary = dst.with_name(f"{name}.{os.getpid()}.tmp")
            shutil.copy2(src, temporary)
            temporary.chmod(0o755)
            temporary.replace(dst)
        except OSError:
            return None
        return dst

    def _install_time_left(self, hooks: dict[str, list]) -> Path | None:
        """Install the iteration-time helper and agent-specific time-left hooks.

        Copies time_left.sh for ALL backends — agents can run it on demand via
        $AUTOHELIX_TIME_LEFT_SCRIPT. Claude registers PostToolUse/UserPromptSubmit
        hooks via claude_settings.json. Codex reuses the same hook script via
        inline Codex CLI hook config. Returns the time_left.sh path, or None if
        unavailable.
        """
        time_left = self._copy_bin("time_left.sh")
        if time_left is None:
            return None

        agent_type = self.config.agent.type.lower()
        if agent_type == "claude":
            time_left_hook = self._copy_bin("claude_hook_time_left.sh")
            if time_left_hook is not None:
                entry = {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": str(time_left_hook)}],
                }
                hooks["PostToolUse"] = [entry]
                hooks["UserPromptSubmit"] = [entry]
        elif agent_type == "codex":
            self._codex_time_left_hook_path = self._copy_bin("claude_hook_time_left.sh")

        return time_left

    def _install_opencode_timer_plugin(self, worktree_path: Path) -> None:
        """Drop the opencode time-left plugin into the worktree's .opencode/plugin/.

        opencode auto-loads project-local plugins from .opencode/plugin/*.js. The
        plugin appends time_left.sh output to each tool result — the opencode
        analog of Claude's PostToolUse time hook. It lives in the worktree (never
        installed for the invocation) and reads $AUTOHELIX_TIME_LEFT_SCRIPT from the agent env.
        """
        src = Path(__file__).parent / "bin" / "opencode_timer_pertool.js"
        if not src.exists():
            return
        plugin_dir = worktree_path / ".opencode" / "plugin"
        try:
            plugin_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, plugin_dir / "opencode_timer_pertool.js")
            # Mark the whole .opencode/ tree as gitignored within the worktree so
            # scope enforcement (revert_out_of_scope, which keys off
            # --exclude-standard) treats this harness-managed tooling like
            # .autohelix/ and doesn't flag/delete it as an out-of-scope change.
            (worktree_path / ".opencode" / ".gitignore").write_text("*\n")
        except OSError:
            pass

    def _install_require_notes(self, hooks: dict[str, list]) -> None:
        """Install the require-notes Stop hook (claude only).

        Registers a Stop hook that blocks the agent from ending its turn until the
        iteration's notes file has enough content. Adds it to `hooks`. No-op for
        non-claude backends (Stop hooks are a Claude Code feature).
        """
        if self.config.agent.type.lower() != "claude":
            return
        notes_hook = self._copy_bin("check_notes.sh")
        if notes_hook is not None:
            hooks["Stop"] = [
                {"hooks": [{"type": "command", "command": str(notes_hook)}]}
            ]

    def _write_claude_settings(self, hooks: dict[str, list]) -> None:
        """Write the accumulated Claude Code hooks to claude_settings.json (if any)."""
        if not hooks:
            return
        settings_dir = self.paths.runtime
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings_path = settings_dir / "claude_settings.json"
        settings_path.write_text(json.dumps({"hooks": hooks}, indent=2))
        self._claude_settings_path = settings_path

    def _validate_codex_iteration_time_ready(self) -> None:
        """Fail before the iteration loop if Codex cannot support time-left hooks."""
        if (
            self.config.iteration_time_seconds is None
            or self.config.agent.type.lower() != "codex"
        ):
            return

        if self._codex_time_left_hook_path is None:
            message = "budget.iteration_time for Codex could not install the time-left hook."
        else:
            from autohelix.agents.codex import CodexAgent

            agent = self.agent if isinstance(self.agent, CodexAgent) else CodexAgent(self.config.agent)
            message = agent._validate_time_left_hook_support(str(self._codex_time_left_hook_path))

        if message:
            self.console.print(f"[red]Error:[/red] {message}")
            raise SystemExit("Codex iteration_time setup failed.")

    @staticmethod
    def _format_usage(usage: dict) -> str:
        """Format usage dict into a compact human-readable string."""
        parts = []
        input_t = usage.get("input_tokens")
        output_t = usage.get("output_tokens")
        if input_t or output_t:
            tokens = f"{_format_tokens(input_t or 0)}in / {_format_tokens(output_t or 0)}out"
            parts.append(tokens)
        cost = usage.get("cost_usd")
        if usage:
            parts.append(format_cost(cost, precision=4))
        duration = usage.get("duration_ms", usage.get("wall_clock_ms"))
        if duration is not None:
            secs = duration / 1000
            if secs >= 60:
                mins = int(secs // 60)
                remaining = int(secs % 60)
                parts.append(f"{mins}m {remaining}s")
            else:
                parts.append(f"{secs:.1f}s")
        return " | ".join(parts)

    def build_prompt(self, iteration: int, worktree_dir: Path) -> str:
        """Build the prompt for the agent using the template."""
        template = load_template(self.project_path)
        variables = build_prompt_variables(
            self.config, self.history, iteration, worktree_dir
        )
        return render_template(template, variables)

    def run_agent(self, worktree_path: Path, prompt: str, iteration: int) -> AgentRunResult:
        """Run the configured agent in the worktree."""
        logs_dir = allocate_call(self.paths.logs / f"iter-{iteration}", "agent")
        prompt_path = logs_dir / "prompt.txt"
        log_path = logs_dir / "agent.log"
        self._agent_logs[iteration] = log_path
        self._live_call("agent", log_path)
        self.run_status.note(log=str(log_path))
        prompt_path.write_text(prompt)

        # opencode time-budget injection: drop the per-tool timer plugin into the
        # worktree so opencode auto-loads it (the analog of Claude's PostToolUse
        # time hook). Only when an iteration deadline is set.
        if (
            self.config.iteration_time_seconds is not None
            and self.config.agent.type.lower() == "opencode"
        ):
            self._install_opencode_timer_plugin(worktree_path)

        if self.verbose:
            print(f"  Prompt: {prompt_path}")
            print(f"  Log: {log_path}")
            print(f"  Heartbeat: {self.heartbeat_seconds}s")

        deadline_env = {
            **self.sandbox.context(worktree_path).env(),
            "AUTOHELIX_CANDIDATE_TIP": self.run_state.expected_tip,
            "AUTOHELIX_PROJECT": str(self.project_path),
            "AUTOHELIX_RUN_ID": self.paths.run_id,
            "AUTOHELIX_RUN_DIR": str(self.paths.run_dir),
            "AUTOHELIX_ITERATION_START": str(int(time.time())),
            "AUTOHELIX_ITERATION_BUDGET": (
                str(self.config.iteration_time_seconds)
                if self.config.iteration_time_seconds is not None
                else None
            ),
            "AUTOHELIX_TIME_LEFT_SCRIPT": (
                str(self._time_left_script)
                if self._time_left_script is not None
                else None
            ),
            "AUTOHELIX_CLAUDE_SETTINGS": (
                str(self._claude_settings_path)
                if self._claude_settings_path is not None
                else None
            ),
            "AUTOHELIX_CODEX_TIME_LEFT_HOOK": (
                str(self._codex_time_left_hook_path)
                if self._codex_time_left_hook_path is not None
                else None
            ),
        }

        # require-notes (Stop hook) env: which file to check, the floor, the cap,
        # and a per-iteration retry counter (reset here so each iteration is fresh).
        require_notes = self.config.agent.require_notes
        if require_notes is not None:
            notes_file = (
                worktree_path / ".autohelix" / "notes" / f"iter-{iteration}.md"
            )
            # Retry counter lives under runtime/state/ so it never clutters
            # the agent's notes dir. Reset here so each iteration starts fresh.
            retry_file = (
                worktree_path / ".autohelix" / "runtime" / "state" / f"iter-{iteration}.retries"
            )
            try:
                retry_file.parent.mkdir(parents=True, exist_ok=True)
                retry_file.write_text("0")
            except OSError:
                pass
            deadline_env["AUTOHELIX_NOTES_FILE"] = str(notes_file)
            deadline_env["AUTOHELIX_NOTES_MIN_CHARS"] = str(require_notes.min_chars)
            deadline_env["AUTOHELIX_NOTES_MAX_RETRIES"] = str(require_notes.max_retries)
            deadline_env["AUTOHELIX_NOTES_RETRY_FILE"] = str(retry_file)

        previous_env: dict[str, str | None] = {}

        display = LiveDisplay(
            console=self.console,
            title=self.agent.display_name,
            border_style="blue",
            verbose=self.verbose,
            iteration=iteration,
        )

        def on_event(event: AgentEvent) -> None:
            display.handle_event(event)

        try:
            # Export the hook env whenever anything needs it. require_notes wires
            # a Claude Stop hook via AUTOHELIX_CLAUDE_SETTINGS, which lives in this
            # dict — so gating only on iteration_time made require_notes a silent
            # no-op when set on its own.
            if deadline_env:
                for key, value in deadline_env.items():
                    previous_env[key] = os.environ.get(key)
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

            with Live(
                display,
                console=self.console,
                refresh_per_second=10,
                transient=not self.verbose,
            ) as live:
                result = self.agent.run(
                    worktree_path=worktree_path,
                    prompt=prompt,
                    iteration=iteration,
                    log_path=log_path,
                    event_callback=on_event,
                    project_path=self.project_path,
                )
                if result.error:
                    display.handle_event(
                        AgentEvent(kind=AgentEventKind.ERROR, text=result.error),
                    )
                display.flush()

            if result.error:
                self.console.print(f"[red]Error:[/red] {result.error}")
            if self.verbose:
                self.console.print()
            return result
        except KeyboardInterrupt:
            self._status("Interrupted - stopping agent...")
            raise
        finally:
            # Mirror the export guard above so the env is always restored.
            if deadline_env:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def _create_reviewer_agent_config(self) -> AgentConfig:
        """Create agent config for reviewer, inheriting from main agent with overrides."""
        if not self.config.reviewer:
            raise RuntimeError("Reviewer not configured")

        return AgentConfig(
            type=self.config.agent.type,
            command=self.config.agent.command,
            model=self.config.reviewer.model or self.config.agent.model,
            reasoning_effort=self.config.agent.reasoning_effort,
            timeout_seconds=self.config.reviewer.timeout_seconds or self.config.agent.timeout_seconds,
            settings=self.config.agent.settings,
            extra_args=self.config.agent.extra_args,
            auto_memory=self.config.reviewer.auto_memory,
            system_prompt="Read the candidate without editing it. Only .autohelix/review.md may be written.",
        )

    def _build_reviewer_prompt(self, worktree_path: Path) -> str:
        """Build the reviewer prompt with absolute path for review file."""
        if not self.config.reviewer:
            return ""

        prompt = self.config.reviewer.prompt.strip()
        # Use absolute path - Claude Code resolves relative paths against project root,
        # not cwd, so we must be explicit about the worktree location
        review_path = worktree_path / ".autohelix" / "review.md"
        return f"{prompt}\n\nWrite your review to {review_path}"

    def _begin_review(self, iteration: int) -> Path:
        directory = allocate_call(self.paths.logs / f"iter-{iteration}", "reviewer")
        log = directory / "review.log"
        self._review_logs.setdefault(iteration, []).append(log)
        self._reviewer_attempt_usage = {}
        self._live_call("reviewer", log)
        return log

    def _live_call(self, role, log):
        calls = list(self.run_status.data.get("calls", []))
        calls.append({"role": role, "log": self._relative_log(log), "usage": {}})
        self.run_status.note(calls=calls)

    def run_reviewer(self, worktree: Worktree, iteration: int) -> AgentRunResult:
        """Run the reviewer, preserving usage even if the review fails."""
        if not self.config.reviewer:
            return AgentRunResult(success=True)
        self.run_status.phase("review", iteration)

        # Delete the transient prior review from the intended reference view
        # so this attempt writes fresh feedback. Shared state remains accessible.
        old_review = worktree.working_dir / ".autohelix" / "review.md"
        if old_review.exists():
            old_review.unlink()

        reviewer_config = self._create_reviewer_agent_config()
        reviewer_agent = create_agent(reviewer_config, heartbeat_seconds=self.heartbeat_seconds)

        log_path = self._review_logs.get(iteration, [None])[-1]
        if log_path is None:
            log_path = self._begin_review(iteration)
        logs_dir = log_path.parent
        prompt_path = logs_dir / "review.prompt.txt"
        self.run_status.note(log=str(log_path))

        prompt = self._build_reviewer_prompt(worktree.working_dir)
        prompt_path.write_text(prompt)

        display = LiveDisplay(
            console=self.console,
            title="review",
            border_style="magenta",
            verbose=self.verbose,
            iteration=iteration,
        )

        def on_event(event: AgentEvent) -> None:
            display.handle_event(event)

        result: AgentRunResult | None = None
        try:
            with Live(
                display,
                console=self.console,
                refresh_per_second=10,
                transient=not self.verbose,
            ) as live:
                with ContentGuard(worktree.working_dir, allowed=(log_path,),
                                  git=worktree.git, tip=self.run_state.expected_tip):
                    result = reviewer_agent.run(
                        worktree_path=worktree.working_dir,
                        prompt=prompt,
                        iteration=iteration,
                        log_path=log_path,
                        event_callback=on_event,
                        project_path=self.project_path,
                    )
                display.flush()

            if result.error:
                self.console.print(f"[red]Reviewer error:[/red] {result.error}")
            if self.verbose:
                self.console.print()

            if not result.success:
                return result

            # Save review file
            saved = self.sandbox.save_review(worktree, iteration)
            if not saved:
                self.console.print(
                    f"  [yellow]![/yellow] Reviewer did not create review.md"
                )
                result.success = False
                result.error = "Reviewer did not create review.md"
                return result

            # Also copy into logs for this iteration
            review_src = (
                self.paths.reviews
                / f"iter-{iteration}.md"
            )
            iter_logs = logs_dir
            iter_logs.mkdir(parents=True, exist_ok=True)
            if review_src.exists():
                shutil.copy2(review_src, iter_logs / "review.md")
            if self.verbose:
                self.console.print(
                    f"  [green]✓[/green] Review saved to reviews/iter-{iteration}.md"
                )

            return result
        except KeyboardInterrupt:
            self._status("Interrupted - stopping reviewer...")
            raise
        except Exception as e:
            self._status(f"Reviewer error: {e}")
            self.log.info(f"iter {iteration} reviewer error — {e}")
            return AgentRunResult(
                success=False, error=str(e),
                usage={**(result.usage if result is not None else {}),
                       **({"candidate_changed": True} if isinstance(e, CandidateChanged) else {})},
            )
        finally:
            if result is not None:
                self._reviewer_attempt_usage = dict(result.usage or {})
            _, restored = self.sandbox.normalize(worktree, self.run_state.expected_tip)
            if restored:
                self.log.warn(f"reviewer changed {self.paths.ref}; restored expected tip")

    def run_iteration(self, iteration: int) -> IterationResult:
        """Run a single iteration."""
        iter_start = time.monotonic()
        started_at = time.time()
        self.log.info(f"iter {iteration} started")
        self.run_status.phase("agent", iteration)
        self.run_status.note(calls=[])

        self._start_command_usage(iteration)
        command_env = self._command_env(iteration)

        # Create worktree
        if self.verbose:
            self._status("Creating worktree...")
        workspace = IterationWorkspace(self.sandbox, iteration, self.run_state, self.history)
        worktree = workspace.worktree
        command_env.update(worktree.git.env())
        command_env["AUTOHELIX_CANDIDATE_TIP"] = self.run_state.expected_tip
        if self.verbose:
            self.console.print(f"  Working dir: {worktree.working_dir}")

        workspace.prepare()
        self.run_status.note(worktree=str(worktree.working_dir))
        checkout_before = None

        agent_usage: dict = {}
        result: IterationResult | None = None
        interrupted_after_accept = False
        failure: BaseException | None = None
        agent_attempted = False
        reviewer_usage = []
        constraint_results = []
        try:
            try:
                checkout_before = self._checkout_probe()
            except Exception as e:
                self.log.warn(f"Could not inspect main checkout for import escapes: {e}")
            # Build and run agent
            prompt = self.build_prompt(iteration, worktree.working_dir)
            if self.verbose:
                self.console.print(Panel(
                    prompt.strip(),
                    title="[bold]Agent Prompt[/bold]",
                    border_style="dim",
                ))
                self.console.print()
                self._status("Running agent...")
            try:
                agent_attempted = True
                agent_result = self.run_agent(worktree.working_dir, prompt, iteration)
                agent_usage = agent_result.usage or {}
            finally:
                agent_commits, restored = workspace.normalize()
                if restored:
                    self.log.warn(f"agent changed {self.paths.ref}; restored expected tip")
                try:
                    if checkout_before is not None and self._checkout_probe() != checkout_before:
                        self.log.warn("Main checkout changed during agent phase; review git diff for an import escape or user edits.")
                except Exception as e:
                    self.log.warn(f"Could not inspect main checkout for import escapes: {e}")
            duration_s = agent_usage.get("duration_ms", 0) / 1000
            cost_text = format_cost(agent_usage.get("cost_usd"), precision=4)
            self.log.info(f"iter {iteration} agent complete — {duration_s:.0f}s, {cost_text}")

            if not agent_result.success and self.verbose:
                self._status("Agent did not complete successfully")
            if not agent_result.success:
                detail = agent_result.error or (
                    f"agent exited with status {agent_result.exit_code}"
                )
                raise AutoHelixRunError(f"Iteration {iteration} agent failed: {detail}")

            if agent_commits:
                self.log.info(
                    f"iter {iteration} normalized {agent_commits} agent-created commit(s)"
                )
                if self.verbose:
                    self.console.print(
                        f"  [yellow]![/yellow] Reopened {agent_commits} "
                        "agent-created commit(s) for validation"
                    )

            # Resolve effective editable scope and revert out-of-scope changes
            reverted = workspace.enforce_scope(
                self.config.editable, self.config.frozen,
            )
            workspace.prepare_checks([capture for command in self.config.observables
                                      for capture in command.capture])
            if reverted and self.verbose:
                self.console.print(
                    f"  [yellow]![/yellow] Reverted {len(reverted)} file(s) outside editable scope:"
                )
                for f in reverted[:5]:
                    self.console.print(f"    [dim]{f}[/dim]")
                if len(reverted) > 5:
                    self.console.print(f"    [dim]... and {len(reverted) - 5} more[/dim]")

            # --- Validation phases (checks → benchmark → review) ---
            # In compact mode, a single spinner persists across all phases.
            self.log.info(f"iter {iteration} constraints started")
            if self.verbose:
                self._status("Checking constraints...")

            val_spinner = _IterationSpinner(iteration)
            val_spinner.set_phase("checks")
            self.run_status.phase("checks")

            def _on_check_progress(phase: str, command: str, elapsed: float | None) -> None:
                val_spinner.set_detail(command)

            def _on_obs_progress(phase: str, command: str, elapsed: float | None) -> None:
                val_spinner.set_detail(command)

            with Live(val_spinner, console=self.console, refresh_per_second=10, transient=True):
                # Constraints
                constraint_results = run_constraints(
                    self.config, worktree.working_dir,
                    project_path=self.project_path,
                    on_progress=_on_check_progress,
                    extra_env=command_env,
                )

                for r in constraint_results:
                    status = "passed" if r.passed else "FAILED"
                    self.log.info(f"iter {iteration} constraint: {r.command} — {status}")

                all_passed = all(r.passed for r in constraint_results)

                obs_results = None
                metrics: dict[str, float] = {}
                if all_passed:
                    # Observables
                    val_spinner.set_phase("benchmark")
                    val_spinner.set_detail("")
                    self.run_status.phase("benchmark")
                    self.log.info(f"iter {iteration} observables started")

                    obs_results = run_observables(
                        self.config, worktree.working_dir,
                        project_path=self.project_path,
                        on_progress=_on_obs_progress,
                        extra_env=command_env,
                    )

                    for obs in obs_results:
                        metrics.update(obs.values)

                    if metrics:
                        metric_str = ", ".join(f"{k}={v:g}" for k, v in metrics.items())
                        self.log.info(f"iter {iteration} observables: {metric_str}")

            # --- Post-spinner verbose output and result building ---
            if self.verbose:
                for r in constraint_results:
                    if r.passed:
                        self.console.print(f"  [green]✓[/green] {r.command}")
                    else:
                        self.console.print(f"  [red]✗[/red] {r.command}")
                        output = r.output[:2000] if len(r.output) > 2000 else r.output
                        self.console.print(Panel(output, title="Constraint output", border_style="red"))
            elif not all_passed:
                for r in constraint_results:
                    if not r.passed:
                        self.console.print(f"  [red]✗[/red] {r.command}")

            judge_error = self._judge_violation(iteration)
            if judge_error:
                result = IterationResult(iteration=iteration, accepted=False, metrics=metrics,
                                         reason=judge_error, calls=[{"role": "agent", "usage": agent_usage}])
            elif all_passed and obs_results is not None:
                # Save stdout and capture files
                use_index = len(obs_results) > 1
                for i, obs in enumerate(obs_results):
                    mc = self.config.observables[i]
                    if mc.capture_stdout and obs.output:
                        self.history.save_stdout(
                            iteration, obs.output,
                            index=i if use_index else None,
                        )
                    for cap_path in mc.capture:
                        self.history.save_capture(
                            iteration, worktree.working_dir / cap_path,
                        )

                if self.verbose:
                    for name, value in metrics.items():
                        self.console.print(f"  {self._metric_label(name)} [cyan]{value}[/cyan]")

                # Check metric gates
                gate_failed = self._check_metric_gates(metrics)
                if gate_failed:
                    if self.verbose:
                        self.console.print(f"  [red]✗[/red] {gate_failed}")
                    result = IterationResult(
                        iteration=iteration,
                        accepted=False,
                        metrics=metrics,
                        reason=gate_failed,
                        calls=[{"role": "agent", "usage": agent_usage}],
                    )
                else:
                    workspace.validate()
                    # Run reviewer if configured (before acceptance, still in worktree)
                    reviewer_failed = False
                    reviewer_usage = []
                    if self.config.reviewer:
                        self._begin_review(iteration)
                        reviewer_usage.append({})
                        try:
                            reviewer_result = self.run_reviewer(worktree, iteration)
                            self._reviewer_attempt_usage = reviewer_result.usage or {}
                        finally:
                            reviewer_usage[-1] = self._reviewer_attempt_usage
                        if not reviewer_result.success:
                            if self.verbose:
                                self.console.print(f"  [red]✗[/red] Reviewer failed")
                            reviewer_failed = True

                    if reviewer_failed:
                        # Keep reviewer failures advisory so validated agent work
                        # is not discarded after an infrastructure failure.
                        reviews_dir = self.paths.reviews
                        reviews_dir.mkdir(parents=True, exist_ok=True)
                        review_path = reviews_dir / f"iter-{iteration}.md"
                        review_path.write_text("# Review\n\nReviewer failed.\n")
                        # TODO: After preserving and merging this candidate, halt
                        # the run so persistent reviewer failures do not silently
                        # affect subsequent iterations.

                    # Publish the validated content to this run, independently of landing.
                    self.run_status.phase("accept")
                    if self.verbose:
                        self._status("Accepting changes...")
                    commit_msg = self._build_commit_message(iteration, metrics, worktree_path=worktree.working_dir)
                    changed = any(usage.get("candidate_changed") for usage in reviewer_usage)
                    result = IterationResult(
                        iteration=iteration, accepted=not changed, metrics=metrics,
                        reason="Reviewer changed candidate files" if changed else None,
                        calls=[{"role": "agent", "usage": agent_usage}],
                    )
                    if not changed:
                        self._complete_record(result, started_at, iter_start, constraint_results, reviewer_usage)
                        workspace.accept(commit_msg, result)

                    # Stop commands see accepted content in the disposable worktree.
                    # An interrupt still records the accepted iteration.
                    if result.accepted and self.config.stop:
                        try:
                            workspace.normalize()
                            result.stop, _ = self._check_stop(
                                worktree.working_dir, iteration, metrics, self.project_path,
                            )
                        except KeyboardInterrupt:
                            interrupted_after_accept = True
                        except Exception as e:  # noqa: BLE001 - acceptance already happened
                            self.console.print(f"  [yellow]![/yellow] Stop check failed: {e}")
                            self.log.info(f"iter {iteration} stop check failed — {e}")
                        finally:
                            workspace.normalize()
            elif not all_passed:
                # Reject: discard worktree
                failed_results = [r for r in constraint_results if not r.passed]
                reason = f"constraint failed: {failed_results[0].command}"
                failure_output = "\n".join(failed_results[0].output.strip().splitlines()[-15:])

                result = IterationResult(
                    iteration=iteration,
                    accepted=False,
                    metrics={},
                    reason=reason,
                    failure_output=failure_output,
                    calls=[{"role": "agent", "usage": agent_usage}],
                )

        except KeyboardInterrupt as e:
            if result is not None and result.accepted and workspace.recorded:
                interrupted_after_accept = True
            else:
                failure = e
                if result is not None and not workspace.published:
                    result.accepted = False
                    result.reason = "interrupted"
        except Exception as e:
            failure = e if isinstance(e, AutoHelixRunError) else AutoHelixRunError(
                f"Iteration {iteration} failed unexpectedly: {e}")
            if result is not None and not workspace.published and not workspace.recorded:
                result.accepted = False
                result.reason = str(failure)
            self.log.error(str(failure))
        finally:
            if result is None:
                result = IterationResult(iteration=iteration, accepted=False, calls=[{"role": "agent", "usage": agent_usage}],
                                         reason=(str(failure) or "interrupted") if failure else "interrupted")
            try:
                self._complete_record(result, started_at, iter_start, constraint_results, reviewer_usage)
                # A failed transaction append may have torn its tail. Do not
                # append blindly; the next owner repairs and reconciles it.
                if agent_attempted and not (workspace.record_write_attempted and not workspace.recorded):
                    if workspace.recorded:
                        self.history.update(result)
                    else:
                        self.history.append(result)
            except BaseException as e:
                if failure is None:
                    failure = e
                else:
                    self.log.warn(f"Could not record failed iteration: {e}")
            finally:
                try:
                    workspace.close()
                finally:
                    self.run_status.note(worktree=None)

        if failure:
            raise failure
        wall_s = result.usage.get("wall_clock_ms", 0) / 1000
        outcome = f"accepted — commit {result.commit or 'n/a'}" if result.accepted else f"rejected — {result.reason}"
        self.log.info(f"iter {iteration} {outcome} ({wall_s:.0f}s total)")
        if interrupted_after_accept:
            raise KeyboardInterrupt
        self.run_status.phase("between iterations")
        return result

    def _checkout_probe(self):
        """Tracked-only, advisory import-escape inspection of the user's checkout."""
        context = self.sandbox.git
        status = context.run("status", "--porcelain", "--untracked-files=no").stdout
        diff = context.run("diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--").stdout
        return hashlib.sha256((status + diff).encode()).digest()

    def _relative_log(self, path):
        return self.paths.relative_log(path)

    def _complete_record(self, result, started_at, iter_start, constraints=(), reviews=None):
        from dataclasses import asdict

        result.started_at = started_at
        result.ended_at = time.time()
        result.constraints = [asdict(check) for check in constraints]
        agents = [call for call in result.calls if call["role"] == "agent"]
        for call in agents:
            call["usage"]["wall_clock_ms"] = int((time.monotonic() - iter_start) * 1000)
            call["log"] = self._relative_log(self._agent_logs.get(result.iteration))
        reviewer_calls = [call for call in result.calls if call["role"] == "reviewer"]
        if reviews is not None:
            logs = self._review_logs.get(result.iteration, [])
            reviewer_calls = [{"role": "reviewer", "usage": usage,
                               "log": self._relative_log(logs[index]) if index < len(logs) else None}
                              for index, usage in enumerate(reviews)]
        commands = []
        for record in read_usage(self._usage_path(result.iteration)):
            usage = dict(record)
            log = usage.pop("log", None)
            commands.append({"role": "command", "usage": usage, "log": self._relative_log(log)})
        result.calls = agents + reviewer_calls + commands

    def _judge_violation(self, iteration: int) -> str | None:
        for usage in read_usage(self._usage_path(iteration)):
            if usage.get("id") in self._usage_starts.get(iteration, set()):
                continue
            if usage.get("candidate_changed"):
                return "Judge changed candidate files or candidate verification failed"
            if usage.get("source") in {"ask", "score", "review"} and usage.get("status") == "started":
                return "Judge did not finish verifying the candidate"
        return None

    def _usage_path(self, iteration: int) -> Path:
        return self.paths.runtime / "usage" / f"iter-{iteration}.jsonl"

    def _start_command_usage(self, iteration: int) -> None:
        """Start an empty usage file for the commands this iteration runs."""
        path = self._usage_path(iteration)
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = next((r for r in self.history.load() if r.iteration == iteration), None)
        if previous is None or previous.accepted:
            path.write_text("")
        self._usage_starts[iteration] = {r.get("id") for r in read_usage(path)}

    def _command_env(self, iteration: int) -> dict[str, str]:
        """Variables for every command the harness runs (checks, metrics, stop):
        where to record model usage and logs, and which config to read."""
        config = self.config_file or self.project_path / "autohelix.yaml"
        log_dir = self.paths.logs / f"iter-{iteration}"
        return {
            USAGE_ENV: str(self._usage_path(iteration)),
            "AUTOHELIX_CONFIG": str(Path(config).resolve()),
            "AUTOHELIX_LOG_DIR": str(log_dir),
            "AUTOHELIX_RUN_ID": self.paths.run_id,
            "AUTOHELIX_RUN_DIR": str(self.paths.run_dir),
            "AUTOHELIX_ITERATION": str(iteration),
        }

    def _check_stop(self, cwd: Path, iteration: int, metrics: dict[str, float],
                    project_path: Path | None) -> tuple[dict[str, str] | None, list]:
        """Run the stop commands on the current head. Returns (stop, errors): stop is
        {"command", "reason"} if one exited 0; errors are the results that exited
        with anything other than 0 or 1 (or timed out)."""
        self.run_status.phase("stop check", iteration)
        runtime = self.paths.runtime
        runtime.mkdir(parents=True, exist_ok=True)
        metrics_path = runtime / "metrics.json"
        metrics_path.write_text(json.dumps(metrics))
        env = {**self._command_env(iteration), **self.sandbox.context(cwd).env(),
               "AUTOHELIX_CANDIDATE_TIP": self.run_state.expected_tip,
               "AUTOHELIX_METRICS": str(metrics_path), "AUTOHELIX_ITERATION": str(iteration)}
        with Status("Checking stop conditions...", console=self.console, spinner="dots"):
            results = run_stop_checks(self.config, cwd, project_path=project_path, extra_env=env)
        errors = [r for r in results if r.error]
        for r in errors:
            code = ("timed out" if r.output.startswith("Timeout after")
                    else "could not run" if r.return_code == -1 else f"exited {r.return_code}")
            self.console.print(f"  [yellow]![/yellow] Stop command {code}: {r.command}", highlight=False)
            tail = "\n".join(r.output.strip().splitlines()[-5:])
            if tail:
                self.console.print(f"[dim]{tail}[/dim]", highlight=False)
            self.log.info(f"iter {iteration} stop command error ({code}): {r.command}")
        if violation := self._judge_violation(iteration):
            raise AutoHelixRunError(violation)
        fired = next((r for r in results if r.stop), None)
        if fired is None:
            return None, errors
        self.log.info(f"iter {iteration} stop condition met — {fired.command}: {fired.reason}")
        return {"command": fired.command, "reason": fired.reason}, errors

    def _capture_baseline(self) -> dict[str, str] | None:
        """Capture baseline observables, check stop commands, run baseline review.

        Returns the stop condition the baseline already meets, if any."""
        try:
            self.log.info("baseline capture started")
            self.run_status.phase("baseline", 0)
            self._start_command_usage(0)
            with Status("Running baseline...", console=self.console, spinner="dots"):
                obs_results = run_observables(self.config, self.project_path,
                                              extra_env=self._command_env(0))

            # Collect scalar values
            metrics: dict[str, float] = {}
            for obs in obs_results:
                metrics.update(obs.values)

            # Save stdout and capture files for baseline (iter-0)
            use_index = len(obs_results) > 1
            for i, obs in enumerate(obs_results):
                mc = self.config.observables[i]
                if mc.capture_stdout and obs.output:
                    self.history.save_stdout(0, obs.output, index=i if use_index else None)
                for cap_path in mc.capture:
                    self.history.save_capture(0, self.project_path / cap_path)

            missing_metrics = set(self.config.metric_directions()) - set(metrics)
            if missing_metrics:
                names = ", ".join(sorted(missing_metrics))
                self.log.info(f"baseline FAILED — missing metrics: {names}")
                raise RuntimeError(
                    f"Baseline benchmark did not produce required metric(s): {names}"
                )

            if metrics:
                metric_str = ", ".join(f"{name}: {value:g}" for name, value in metrics.items())
                self.console.print(f"  baseline │ {metric_str}", highlight=False)
                self.log.info(f"baseline: {metric_str}")

            if violation := self._judge_violation(0):
                raise AutoHelixRunError(violation)
            baseline = IterationResult(
                iteration=0,
                accepted=True,
                metrics=metrics,
                commit=None,
                reason="baseline",
            )
            if self.config.stop:
                # Like the baseline observables, these run in the project checkout. A
                # broken stop command fails here, before anything is recorded, so a
                # typo surfaces before the first iteration is paid for.
                stop_tree = self.sandbox.create_worktree(0, self.run_state.expected_tip)
                self.sandbox.prepare_worktree(stop_tree)
                try:
                    baseline.stop, errors = self._check_stop(stop_tree.working_dir, 0, metrics, self.project_path)
                finally:
                    self.sandbox.normalize(stop_tree, self.run_state.expected_tip)
                    self.sandbox.discard_worktree(stop_tree)
                if errors:
                    self.log.info("baseline FAILED — stop command error")
                    raise AutoHelixRunError(
                        f"Stop command failed on the baseline: {errors[0].command} "
                        "(expected exit 0 to stop or 1 to continue)"
                    )
            self._complete_record(baseline, time.time(), time.monotonic())
            if any(r.iteration == 0 for r in self.history.load()):
                self.history.update(baseline)
            else:
                self.history.append(baseline)
            if self.config.reviewer and not baseline.stop:
                self._run_baseline_review()
            return baseline.stop

        except BaseException as error:
            baseline = IterationResult(iteration=0, accepted=False, reason=f"baseline failed: {error}")
            self._complete_record(baseline, time.time(), time.monotonic())
            if any(r.iteration == 0 for r in self.history.load()):
                previous = self.history.load()[0]
                baseline.calls.extend(call for call in previous.calls if call["role"] == "reviewer")
                if previous.accepted:
                    raise
                self.history.update(baseline)
            else:
                self.history.append(baseline)
            raise

    def _run_baseline_review(self) -> None:
        """Run reviewer on the baseline state (before any iterations)."""
        if not self.config.reviewer:
            return

        # Create a temporary worktree for baseline review
        worktree = self.sandbox.create_worktree(0, self.run_state.expected_tip)
        self.sandbox.prepare_worktree(worktree)

        # Ensure notes directory exists
        notes_dir = worktree.working_dir / ".autohelix" / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Preserve an unknown charge if interrupted before usage is returned.
            log = self._begin_review(0)
            attempt = self.history.record_reviewer_usage(0, {}, log=self._relative_log(log))
            try:
                reviewer_result = self.run_reviewer(worktree, 0)
                self._reviewer_attempt_usage = reviewer_result.usage or {}
            finally:
                self.history.record_reviewer_usage(0, self._reviewer_attempt_usage, attempt)
            review_cost = (reviewer_result.usage or {}).get("cost_usd")
            self.console.print(f"  baseline review │ {format_cost(review_cost)}", highlight=False)
            if not reviewer_result.success:
                self.console.print(
                    f"  [yellow]![/yellow] Baseline review failed, continuing anyway"
                )
        finally:
            self.sandbox.discard_worktree(worktree)

    def _resolve_config_rel_path(self) -> str:
        """Return the config file path relative to the project root."""
        if self.config_file is None:
            return "autohelix.yaml"
        try:
            return str(self.config_file.resolve().relative_to(self.project_path))
        except ValueError:
            return str(self.config_file)

    def run(self, max_iterations: int | None = None, report: bool = True) -> None:
        """Run the optimization loop, recording live status in runtime/status.json.

        When the loop finishes normally (not interrupted) after running at least one
        iteration, writes the run summary unless `report: false` or `report=False`.
        However a started run stops, it then saves output/report.html.
        """
        outcome = "finished"
        self._segment_started = False
        self.run_status.data = {}
        try:
            if self._ownership._file.closed:
                self._ownership = RunLock(self.paths.lock)
                self.run_info = self.paths.read_identity()
                if self.run_info["lifecycle"] != "open":
                    raise AutoHelixRunError(f"Run {self.paths.run_id} is abandoned; use --new.")
                tip, warning = reconcile(self.paths, self.run_info, self.history, self.land_mode)
                self.run_state.data = self.run_info
                self.run_state.expected_tip = tip
            self._land_attempted = False
            if self._run(max_iterations) and report and self.config.report:
                from autohelix.report import write_report

                self.run_status.phase("report")
                self.console.print()
                write_report(self.config, self.project_path, self.console, self.config_file,
                             run_id=self.paths.run_id, _owned=True)
        except KeyboardInterrupt:
            outcome = "interrupted"
            raise
        except BaseException:
            outcome = "failed"
            raise
        finally:
            try:
                original = sys.exc_info()[1]
                try:
                    self._land()
                except BaseException as e:
                    if original is None:
                        outcome = "interrupted" if isinstance(e, KeyboardInterrupt) else "failed"
                        raise
                    self.log.warn(f"Auto-land failed while preserving original failure: {e}")
                finally:
                    self.run_status.stop(outcome)
                    if self._segment_started:
                        self._write_static()
            finally:
                self.close()

    def close(self) -> None:
        if hasattr(self, "_ownership"):
            self._ownership.close()

    def __del__(self):
        self.close()

    def _land(self) -> None:
        if self.land_mode != "auto" or not self._segment_started or self._land_attempted:
            return
        self._land_attempted = True
        try:
            target = self.run_info.get("target_branch")
            if target and getattr(self, "_segment_target_tip", None) is not None:
                current = self.sandbox._run_git("rev-parse", "--verify", f"refs/heads/{target}").stdout.strip()
                if current != self._segment_target_tip:
                    raise AutoHelixRunError(
                        f"Target {target} changed during this segment; leaving user commits untouched. "
                        f"Review them, then use autohelix land {self.paths.run_id} explicitly."
                    )
            land(self.paths, self.run_info, self.run_state.expected_tip)
            self.console.print(f"[dim]Landed run {self.paths.run_id} into {target}.[/dim]")
        except Exception as e:
            self.console.print(f"[yellow]{e}[/yellow]", highlight=False)
            self.log.warn(f"auto-land: {e}")

    def _write_static(self) -> None:
        from autohelix.viewer import write_static

        try:
            path = write_static(self.project_path, self.config_file, run_id=self.paths.run_id)
            self.console.print(f"[dim]Run page saved to {path.relative_to(self.project_path)}[/dim]")
        except Exception as e:  # noqa: BLE001 - never hide the run's own outcome
            self.console.print(f"[yellow]Could not save report.html: {e}[/yellow]")

    def _run(self, max_iterations: int | None = None) -> bool:
        """Returns True if the loop ran at least one iteration and was not interrupted."""
        max_iter = max_iterations or self.config.max_iterations
        start_iter = self.history.get_last_iteration() + 1

        # Covers programmatic/repeated runs as well as the CLI's early check.
        try:
            ensure_runtime_ignored(self.project_path)
        except GitExcludeError as e:
            raise AutoHelixRunError(str(e)) from e

        needs_baseline = not any(r.iteration == 0 and r.accepted for r in self.history.load())
        if needs_baseline:
            self.sandbox.ensure_clean_working_tree()
            if self.sandbox._run_git_checked("rev-parse", "HEAD").stdout.strip() != self.run_state.expected_tip:
                raise AutoHelixRunError("Baseline checkout moved since this run started. Restore its original commit or abandon this run and start --new.")
            self.sandbox.ensure_editable_files_ready(self.config.editable)

        # Show header
        self._print_header(max_iter, start_iter)

        # Run preflight checks on first run (silent on success)
        if needs_baseline:
            issues = preflight_check(self.config, self._raw_config, self.project_path)
            errors = [i for i in issues if i.level == "error"]
            warnings = [i for i in issues if i.level == "warning"]
            for w in warnings:
                self.console.print(f"  [yellow]WARN[/yellow] {w.message}")
            if errors:
                for e in errors:
                    self.console.print(f"  [red]FAIL[/red] {e.message}")
                self.console.print(
                    "\n[red]Preflight checks failed.[/red] Fix the issues above before running."
                )
                return False

        self.run_status.start(max_iterations=max_iter, run_id=self.paths.run_id)
        self._segment_started = True
        target = self.run_info.get("target_branch")
        self._segment_target_tip = (
            self.sandbox._run_git("rev-parse", "--verify", f"refs/heads/{target}").stdout.strip()
            if target else None)

        # Capture baseline on first run
        baseline_captured = False
        baseline_stop = None
        if start_iter == 1 and self.history.get_last_iteration() == 0:
            results = self.history.load()
            if needs_baseline:
                baseline_stop = self._capture_baseline()
                baseline_captured = True

        # Run baseline review if configured but no review exists yet (and budget is left)
        reviews_dir = self.paths.reviews
        if (self.config.reviewer and not baseline_captured
                and not (reviews_dir.exists() and any(reviews_dir.glob("iter-*.md")))
                and (self.config.max_cost_usd is None
                     or (summarize_costs(self.history.cost_usages).cost_usd or 0) < self.config.max_cost_usd)):
            self._run_baseline_review()

        # Load cumulative cost and time from history so budgets persist across resume
        prior_cost = summarize_costs(self.history.cost_usages)
        cumulative_cost = prior_cost.cost_usd or 0
        prior_elapsed_seconds = 0.0
        if start_iter > 1:
            prior = self.history.load()
            prior_iterations = [r for r in prior if r.iteration > 0]
            # Prefer wall_clock_ms (total iteration time) over duration_ms (agent-only time)
            prior_elapsed_seconds = sum(
                r.usage.get("wall_clock_ms", r.usage.get("duration_ms", 0))
                for r in prior_iterations
            ) / 1000.0
        loop_start = max(0.0, time.monotonic() - prior_elapsed_seconds)

        config_name = self._resolve_config_rel_path()
        self.log.info(f"run started — config: {config_name}, max_iter: {max_iter}, resuming from iter {start_iter}")

        ran = False
        cost_warning_shown = False
        if self.config.max_cost_usd is not None:
            cost_warning_shown = prior_cost.incomplete
            if cost_warning_shown:
                self.console.print(
                    "[yellow]Warning: prior agent or reviewer calls have unavailable cost.\n"
                    "budget.cost can only track known costs.[/yellow]"
                )

        # Why the loop ended; budgets are checked after stop conditions.
        stop_reason: dict | None = None
        if baseline_stop:
            stop_reason = self._condition_stop(baseline_stop, 0)
        cost_reached = {"kind": "budget", "budget": "cost",
                        "detail": f"cost budget reached ({format_cost(self.config.max_cost_usd)})"}
        iteration = start_iter
        try:
            for iteration in range(start_iter, max_iter + 1):
                if stop_reason:
                    break
                if self.config.max_cost_usd is not None and cumulative_cost >= self.config.max_cost_usd:
                    stop_reason = cost_reached
                    break
                result = self.run_iteration(iteration)
                ran = True
                self._print_iteration_summary(result)

                if result.stop:
                    stop_reason = self._condition_stop(result.stop, iteration)
                    break

                # Check cost budget
                if (
                    self.config.max_cost_usd is not None
                    and result.cost.incomplete
                    and not cost_warning_shown
                ):
                    self.console.print(
                        "[yellow]Warning: iteration cost is unavailable; "
                        "budget.cost can only track known costs.[/yellow]"
                    )
                    cost_warning_shown = True
                cumulative_cost += result.cost.cost_usd or 0
                if self.config.max_cost_usd is not None and cumulative_cost >= self.config.max_cost_usd:
                    stop_reason = cost_reached
                    break

                # Check time budget
                elapsed = time.monotonic() - loop_start
                if self.config.max_time_seconds is not None and elapsed >= self.config.max_time_seconds:
                    stop_reason = {"kind": "budget", "budget": "time",
                                   "detail": f"time budget reached ({_format_time(self.config.max_time_seconds)})"}
                    break
            else:
                stop_reason = stop_reason or {
                    "kind": "budget", "budget": "iterations",
                    "detail": f"iteration budget reached ({max_iter})"}
            self.run_status.note(stop_reason=stop_reason)
            self.console.print(f"\n[dim]Stopped:[/dim] {stop_reason['detail']}", highlight=False)
            self.log.info(f"run stopped — {stop_reason['detail']}")
        except KeyboardInterrupt:
            self.run_status.stop("interrupted")
            self.console.print(f"\n\n[yellow]Interrupted at iteration {iteration}. Progress saved.[/yellow] "
                               "[dim]Run [bold]autohelix report[/bold] for a summary.[/dim]")
            ran = False
        self._land()
        if self.land_mode == "manual":
            self.console.print(f"[dim]Accepted code remains on {self.paths.ref}. Land with autohelix land {self.paths.run_id}.[/dim]")
        self._print_summary()
        return ran

    @staticmethod
    def _condition_stop(stop: dict[str, str], iteration: int) -> dict:
        where = "baseline" if iteration == 0 else f"iteration {iteration}"
        return {"kind": "condition", "iteration": iteration, **stop,
                "detail": f"stop condition met at {where}: {stop['reason']}"}

    def _print_iteration_summary(self, result: IterationResult) -> None:
        """Print a concise one-liner after each iteration."""
        from rich.text import Text as RichText

        iteration = result.iteration
        wall_ms = result.usage.get("wall_clock_ms", result.usage.get("duration_ms", 0))
        cost = result.cost.cost_usd
        # "+": known part only (some agent or reviewer cost is unavailable); the run total says "(incomplete)".
        cost_text = "—" if cost is None else format_cost(cost) + ("+" if result.cost.incomplete else "")

        directions = self.config.metric_directions()

        # Fixed-width columns so successive lines line up:
        # iter N │ time │ cost │ metric value (delta) … │ outcome │ reason
        baseline_results = [r for r in self.history.load() if r.iteration == 0]
        baseline_metrics = baseline_results[0].metrics if baseline_results else {}
        parts = [f"{format_duration(wall_ms / 1000):>7}", f"{cost_text:>7}"]
        for name in [*directions, *(n for n in result.metrics if n not in directions)]:
            value = result.metrics.get(name)
            if value is None:  # e.g. rejected before measuring
                parts.append(f"{name}: {'—':>9} {'':9}")
                continue
            baseline_val = baseline_metrics.get(name)
            delta = format_delta(value, baseline_val) if baseline_val is not None else ""
            parts.append(f"{name}: {value:>9g} {f'({delta})' if delta else '':9}")

        line = RichText()
        line.append(f"  iter {iteration:<2} ", style="dim")
        line.append(" │ ".join(parts) + " │ ")
        if result.accepted:
            line.append("✓ accepted", style="green")
        else:
            line.append("✗ reject", style="red")
        if result.reason and not result.accepted:
            line.append(f" │ {result.reason}")

        self.console.print(line)

    def _print_summary(self) -> None:
        """Print end-of-run summary as a bordered key-value table."""
        from rich.box import ROUNDED

        results = self.history.load()
        iterations = [r for r in results if r.iteration > 0]
        accepted = sum(1 for r in iterations if r.accepted)
        rejected = len(iterations) - accepted

        if not iterations and not self.history.cost_usages:
            return

        directions = self.config.metric_directions()
        baseline_results = [r for r in results if r.iteration == 0]
        baseline_metrics = baseline_results[0].metrics if baseline_results else {}
        best = self.history.get_best_metrics(directions)

        # Cumulative stats
        total_cost_text = format_total_cost(self.history.cost_usages)
        total_wall_ms = sum(
            r.usage.get("wall_clock_ms", r.usage.get("duration_ms", 0))
            for r in iterations
        )
        total_time_str = _format_time(int(total_wall_ms / 1000))

        self.console.print()

        table = Table(
            box=ROUNDED,
            show_header=False,
            border_style="dim",
            padding=(0, 1),
        )
        table.add_column("", style="dim", no_wrap=True)
        table.add_column("", no_wrap=True)

        table.add_row("Iterations", f"{accepted} accepted, {rejected} rejected")
        table.add_row("Duration", total_time_str)
        table.add_row("Cost", total_cost_text)

        for name, (best_val, best_iter) in best.items():
            baseline_val = baseline_metrics.get(name)
            direction = directions.get(name, "lower")
            val_text = Text(f"{best_val:g}")
            delta = format_delta(best_val, baseline_val) if baseline_val is not None else ""
            if delta:
                pct = (best_val - baseline_val) / baseline_val * 100
                improved = (pct < 0) if direction == "lower" else (pct > 0)
                val_text.append(f" ({delta})", style="green" if improved else "red")
            val_text.append(f"  iter {best_iter}", style="dim")
            table.add_row(f"Best {name}", val_text)

        self.console.print(table)

        self.log.info(f"run complete — {accepted} accepted, {rejected} rejected, {total_cost_text}, {total_time_str}")
