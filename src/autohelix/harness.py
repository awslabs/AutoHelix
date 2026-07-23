# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Main iteration loop."""

import json
import os
import shutil
import subprocess
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
from autohelix.display import LiveDisplay
from autohelix.history import History, IterationResult
from autohelix.checks import preflight_check, run_constraints, run_observables
from autohelix.dashboard import generate_dashboard
from autohelix.formatting import format_delta
from autohelix.prompt_template import build_prompt_variables, load_template, render_template
from autohelix.run_log import RunLog
from autohelix.sandbox import Sandbox, Worktree
from autohelix.state import (
    archive_state,
    ensure_gitignore_entry,
    read_stored_config_rel,
    store_config_path,
)


_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


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

    def __init__(self, project_path: Path, verbose: bool = False, heartbeat_seconds: int = 30, config_file: Path | None = None):
        self.project_path = Path(project_path).resolve()
        self.config_file = config_file
        self.config, self._raw_config = load_config(self.project_path, config_file=config_file)
        self.history = History(self.project_path)
        self.sandbox = Sandbox(self.project_path)
        self.verbose = verbose
        self.heartbeat_seconds = heartbeat_seconds
        self.console = Console()
        self.log = RunLog(self.project_path)

        # Validate config and report errors (warnings shown later by preflight)
        issues = self.config.validate(raw_data=self._raw_config)
        errors = [i for i in issues if i.level == "error"]
        if errors:
            for error in errors:
                self.console.print(f"[red]Error:[/red] {error.message}")
            raise SystemExit("Config validation failed. Edit autohelix.yaml and try again.")

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
        self.console.print(f"  [bold]AutoHelix[/bold] [dim]v{__version__}[/dim]", highlight=False)
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
        dst_dir = self.project_path / ".autohelix" / "runtime" / "bin"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / name
        try:
            shutil.copy2(src, dst)
            dst.chmod(0o755)
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
        committed: merge_worktree only stages editable files, so it can't reach
        main) and reads $AUTOHELIX_TIME_LEFT_SCRIPT from the agent env.
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
        settings_dir = self.project_path / ".autohelix" / "runtime"
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
        if cost is not None:
            parts.append(f"${cost:.4f}")
        duration = usage.get("duration_ms")
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
        logs_dir = self.project_path / ".autohelix" / "logs" / f"iter-{iteration}"
        logs_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = logs_dir / "prompt.txt"
        log_path = logs_dir / "agent.log"
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
            "AUTOHELIX_PROJECT": str(self.project_path),
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
            if self.config.iteration_time_seconds is not None or require_notes is not None:
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
            if self.config.iteration_time_seconds is not None or require_notes is not None:
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

    def run_reviewer(self, worktree: Worktree, iteration: int) -> bool:
        """Run the reviewer agent in the worktree.

        Returns True if review was successful, False otherwise.
        """
        if not self.config.reviewer:
            return True  # No reviewer configured, nothing to do

        # Delete the transient review.md (materialized from the latest prior
        # review for the main agent) so the reviewer writes fresh and can't read
        # a prior review — it never sees the reviews/ archive at all.
        old_review = worktree.working_dir / ".autohelix" / "review.md"
        if old_review.exists():
            old_review.unlink()

        reviewer_config = self._create_reviewer_agent_config()
        reviewer_agent = create_agent(reviewer_config, heartbeat_seconds=self.heartbeat_seconds)

        logs_dir = self.project_path / ".autohelix" / "logs" / f"iter-{iteration}"
        logs_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = logs_dir / "review.prompt.txt"
        log_path = logs_dir / "review.log"

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

        try:
            with Live(
                display,
                console=self.console,
                refresh_per_second=10,
                transient=not self.verbose,
            ) as live:
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

            # Save review file
            if result.success:
                saved = self.sandbox.save_review(worktree, iteration)
                if saved:
                    # Also copy into logs for this iteration
                    review_src = self.project_path / ".autohelix" / "reviews" / f"iter-{iteration}.md"
                    iter_logs = self.project_path / ".autohelix" / "logs" / f"iter-{iteration}"
                    iter_logs.mkdir(parents=True, exist_ok=True)
                    if review_src.exists():
                        shutil.copy2(review_src, iter_logs / "review.md")
                    if self.verbose:
                        self.console.print(f"  [green]✓[/green] Review saved to reviews/iter-{iteration}.md")
                else:
                    self.console.print(f"  [yellow]![/yellow] Reviewer did not create review.md")
                    return False

            return result.success
        except KeyboardInterrupt:
            self._status("Interrupted - stopping reviewer...")
            raise

    def run_iteration(self, iteration: int) -> IterationResult:
        """Run a single iteration."""
        iter_start = time.monotonic()
        self.log.info(f"iter {iteration} started")

        # Create worktree
        if self.verbose:
            self._status("Creating worktree...")
        worktree = self.sandbox.create_worktree(iteration)
        if self.verbose:
            self.console.print(f"  Working dir: {worktree.working_dir}")

        # Seed the worktree with notes + read-only reference material
        self.sandbox.prepare_worktree(worktree)

        # Ensure notes directory exists (agent creates the file itself)
        notes_dir = worktree.working_dir / ".autohelix" / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)

        agent_usage: dict = {}
        result: IterationResult | None = None
        try:
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
            agent_result = self.run_agent(worktree.working_dir, prompt, iteration)
            agent_usage = agent_result.usage or {}
            duration_s = agent_usage.get("duration_ms", 0) / 1000
            cost = agent_usage.get("cost_usd", 0)
            self.log.info(f"iter {iteration} agent complete — {duration_s:.0f}s, ${cost:.4f}")

            if not agent_result.success and self.verbose:
                self._status("Agent did not complete successfully")

            # Resolve effective editable scope and revert out-of-scope changes
            effective_editable = self.sandbox.resolve_editable(
                self.config.editable, self.config.frozen, cwd=worktree.working_dir,
            )
            if effective_editable is not None:
                reverted = self.sandbox.revert_out_of_scope(worktree, effective_editable)
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
                    self.log.info(f"iter {iteration} observables started")

                    obs_results = run_observables(
                        self.config, worktree.working_dir,
                        project_path=self.project_path,
                        on_progress=_on_obs_progress,
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

            if all_passed and obs_results is not None:
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
                        usage=agent_usage,
                    )
                else:
                    # Run reviewer if configured (before merge, still in worktree)
                    reviewer_failed = False
                    if self.config.reviewer:
                        reviewer_success = self.run_reviewer(worktree, iteration)
                        if not reviewer_success:
                            if self.verbose:
                                self.console.print(f"  [red]✗[/red] Reviewer failed")
                            reviewer_failed = True

                    if reviewer_failed:
                        # Write a placeholder review so this iteration is recorded
                        reviews_dir = self.project_path / ".autohelix" / "reviews"
                        reviews_dir.mkdir(parents=True, exist_ok=True)
                        review_path = reviews_dir / f"iter-{iteration}.md"
                        review_path.write_text("# Review\n\nReviewer failed.\n")

                    # Accept: merge changes (only editable files are committed)
                    if self.verbose:
                        self._status("Merging changes...")
                    commit_msg = self._build_commit_message(iteration, metrics, worktree_path=worktree.working_dir)
                    commit = self.sandbox.merge_worktree(worktree, editable=effective_editable, message=commit_msg)

                    result = IterationResult(
                        iteration=iteration,
                        accepted=True,
                        metrics=metrics,
                        commit=commit,
                        usage=agent_usage,
                    )
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
                    usage=agent_usage,
                )

        except Exception as e:
            self._status(f"Error: {e}")
            traceback.print_exc()
            result = IterationResult(
                iteration=iteration,
                accepted=False,
                metrics={},
                reason=f"error: {e}",
                usage=agent_usage,
            )
        finally:
            self.sandbox.save_notes(worktree)
            if worktree.path.exists():
                self.sandbox.discard_worktree(worktree)

        # Record total wall-clock time for this iteration (includes all overhead)
        result.usage["wall_clock_ms"] = int((time.monotonic() - iter_start) * 1000)
        wall_s = result.usage["wall_clock_ms"] / 1000

        if result.accepted:
            self.log.info(f"iter {iteration} merged — commit {result.commit or 'n/a'} ({wall_s:.0f}s total)")
        else:
            self.log.info(f"iter {iteration} rejected — {result.reason} ({wall_s:.0f}s total)")

        # Log result and update dashboard
        self.history.append(result)
        try:
            generate_dashboard(self.project_path, self.config.metric_directions())
        except Exception:
            pass
        return result

    def _capture_baseline(self) -> None:
        """Capture baseline observables and run baseline review."""
        self.log.info("baseline capture started")
        with Status("Running baseline...", console=self.console, spinner="dots"):
            obs_results = run_observables(self.config, self.project_path)

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

        if metrics:
            metric_str = ", ".join(f"{name}: {value:g}" for name, value in metrics.items())
            self.console.print(f"  baseline │ {metric_str}", highlight=False)
            self.log.info(f"baseline: {metric_str}")

        baseline = IterationResult(
            iteration=0,
            accepted=True,
            metrics=metrics,
            commit=None,
            reason="baseline",
        )
        self.history.append(baseline)
        try:
            generate_dashboard(self.project_path, self.config.metric_directions())
        except Exception:
            pass

        # Run baseline review if reviewer is configured
        if self.config.reviewer:
            self._run_baseline_review()

    def _run_baseline_review(self) -> None:
        """Run reviewer on the baseline state (before any iterations)."""
        if not self.config.reviewer:
            return

        # Create a temporary worktree for baseline review
        worktree = self.sandbox.create_worktree(0)
        self.sandbox.prepare_worktree(worktree)

        # Ensure notes directory exists
        notes_dir = worktree.working_dir / ".autohelix" / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)

        try:
            reviewer_success = self.run_reviewer(worktree, 0)
            if not reviewer_success:
                self.console.print(f"  [yellow]![/yellow] Baseline review failed, continuing anyway")
        except Exception as e:
            self._status(f"Baseline review error: {e}")
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

    def _check_active_config(self) -> bool:
        """Check if the active config matches. Returns True if ok to proceed."""
        current = self._resolve_config_rel_path()
        previous = read_stored_config_rel(self.project_path)
        if previous and previous != current:
            from rich.prompt import Confirm
            proceed = Confirm.ask(
                f"Different config detected (was '{previous}', now '{current}'). "
                f"Archive previous state and continue?",
                console=self.console,
            )
            if proceed:
                archive_state(self.project_path, self.console)
            else:
                return False
        store_config_path(self.project_path, current)
        return True

    def run(self, max_iterations: int | None = None) -> None:
        """Run the optimization loop."""
        max_iter = max_iterations or self.config.max_iterations
        start_iter = self.history.get_last_iteration() + 1

        # Check active config matches
        if not self._check_active_config():
            return

        # Ensure .autohelix/ is gitignored (covers manual config + run path)
        ensure_gitignore_entry(self.project_path, ".autohelix/")
        # Commit .gitignore if we just modified it
        result = subprocess.run(
            ["git", "status", "--porcelain", ".gitignore"],
            cwd=self.project_path, capture_output=True, text=True,
        )
        if result.stdout.strip():
            subprocess.run(
                ["git", "add", ".gitignore"],
                cwd=self.project_path, capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "Add .autohelix to .gitignore"],
                cwd=self.project_path, capture_output=True,
            )

        # Check for any uncommitted changes in the repo
        try:
            self.sandbox.ensure_clean_working_tree()
        except RuntimeError as e:
            self.console.print(f"[red]Error:[/red] {e}")
            return

        # Check editable files exist and are committed
        try:
            self.sandbox.ensure_editable_files_ready(self.config.editable)
        except RuntimeError as e:
            self.console.print(f"[red]Error:[/red] {e}")
            self.console.print("\n[dim]Commit your editable files before running AutoHelix.[/dim]")
            return

        # Show header
        self._print_header(max_iter, start_iter)

        # Run preflight checks on first run (silent on success)
        if start_iter == 1:
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
                return

        # Capture baseline on first run
        if start_iter == 1 and self.history.get_last_iteration() == 0:
            results = self.history.load()
            if not any(r.iteration == 0 for r in results):
                self._capture_baseline()

        # Run baseline review if configured but no review exists yet
        if self.config.reviewer:
            reviews_dir = self.project_path / ".autohelix" / "reviews"
            if not reviews_dir.exists() or not any(reviews_dir.glob("iter-*.md")):
                self._run_baseline_review()

        # Load cumulative cost and time from history so budgets persist across resume
        cumulative_cost = 0.0
        prior_elapsed_seconds = 0.0
        if start_iter > 1:
            prior = self.history.load()
            prior_iterations = [r for r in prior if r.iteration > 0]
            cumulative_cost = sum(r.usage.get("cost_usd", 0) for r in prior_iterations)
            # Prefer wall_clock_ms (total iteration time) over duration_ms (agent-only time)
            prior_elapsed_seconds = sum(
                r.usage.get("wall_clock_ms", r.usage.get("duration_ms", 0))
                for r in prior_iterations
            ) / 1000.0
        loop_start = max(0.0, time.monotonic() - prior_elapsed_seconds)

        config_name = self._resolve_config_rel_path()
        self.log.info(f"run started — config: {config_name}, max_iter: {max_iter}, resuming from iter {start_iter}")

        try:
            for iteration in range(start_iter, max_iter + 1):
                result = self.run_iteration(iteration)
                self._print_iteration_summary(result)

                # Check cost budget
                cumulative_cost += result.usage.get("cost_usd", 0) if result.usage else 0
                if self.config.max_cost_usd is not None and cumulative_cost >= self.config.max_cost_usd:
                    break

                # Check time budget
                elapsed = time.monotonic() - loop_start
                if self.config.max_time_seconds is not None and elapsed >= self.config.max_time_seconds:
                    break
        except KeyboardInterrupt:
            self.console.print(f"\n\n[yellow]Interrupted at iteration {iteration}. Progress saved.[/yellow]")
        self._print_summary()

    def _print_iteration_summary(self, result: IterationResult) -> None:
        """Print a concise one-liner after each iteration."""
        from rich.text import Text as RichText

        iteration = result.iteration
        wall_ms = result.usage.get("wall_clock_ms", result.usage.get("duration_ms", 0))
        wall_secs = int(wall_ms / 1000)
        time_str = _format_time(wall_secs)
        cost = result.usage.get("cost_usd", 0)

        directions = self.config.metric_directions()

        # Build metric text with delta % from baseline
        metric_parts = []
        baseline_results = [r for r in self.history.load() if r.iteration == 0]
        baseline_metrics = baseline_results[0].metrics if baseline_results else {}
        for name, value in result.metrics.items():
            baseline_val = baseline_metrics.get(name)
            delta_str = format_delta(value, baseline_val) if baseline_val is not None else ""
            if delta_str:
                metric_parts.append(f"{name}: {value:g} ({delta_str})")
            else:
                metric_parts.append(f"{name}: {value:g}")

        # Build line: iter N  time │ cost │ metric │ status
        parts = [f"{time_str:>6}"]
        if cost:
            parts.append(f"${cost:.2f}")
        parts.extend(metric_parts)
        if result.reason and not result.accepted:
            parts.append(result.reason)

        line = RichText()
        line.append(f"  iter {iteration:<2} ", style="dim")
        line.append(" │ ".join(parts), style="")
        line.append(" │ ", style="")
        if result.accepted:
            line.append("✓ merged", style="green")
        else:
            line.append("✗ reject", style="red")

        self.console.print(line)

    def _print_summary(self) -> None:
        """Print end-of-run summary as a bordered key-value table."""
        from rich.box import ROUNDED

        results = self.history.load()
        iterations = [r for r in results if r.iteration > 0]
        accepted = sum(1 for r in iterations if r.accepted)
        rejected = len(iterations) - accepted

        if not iterations:
            return

        directions = self.config.metric_directions()
        baseline_results = [r for r in results if r.iteration == 0]
        baseline_metrics = baseline_results[0].metrics if baseline_results else {}
        best = self.history.get_best_metrics(directions)

        # Cumulative stats
        total_cost = sum(r.usage.get("cost_usd", 0) for r in iterations)
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

        table.add_row("Iterations", f"{accepted} merged, {rejected} rejected")
        table.add_row("Duration", total_time_str)
        if total_cost:
            table.add_row("Cost", f"${total_cost:.2f}")

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
        self.console.print()
        self.console.print("[dim]Run [bold]autohelix report[/bold] for analysis (uses one agent call).[/dim]")

        self.log.info(f"run complete — {accepted} merged, {rejected} rejected, ${total_cost:.2f}, {total_time_str}")
