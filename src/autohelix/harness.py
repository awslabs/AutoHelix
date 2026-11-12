# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Main iteration loop."""

import json
import hashlib
import os
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.status import Status
from rich.table import Table
from rich.text import Text

from autohelix.agents import AgentEvent, AgentEventKind, AgentRunResult, create_agent
from autohelix.config import MetricGate, load_config
from autohelix.costs import summarize_costs
from autohelix.display import LiveDisplay
from autohelix.history import History, IterationResult
from autohelix.runs import (RunPaths, RunLock, RunState, select_run, reconcile, land, git, allocate_call,
                            claim_copies, remove_copies, short_id, write_json, write_text)
from autohelix.iteration import IterationWorkspace
from autohelix.launcher import command_path
from autohelix.checks import (metric_gate_failure, metric_reference, preflight_check, run_gate_commands,
                              run_observe_entry, run_stop_checks)
from autohelix.run_status import RunStatus
from autohelix.usage_log import ENV as USAGE_ENV, read_usage
from autohelix.formatting import format_cost, format_delta, format_duration, format_total_cost
from autohelix.prompt_template import build_prompt_variables, load_template, render_template, uses_old_layout
from autohelix.run_log import RunLog
from autohelix.sandbox import ensure_clean_working_tree, ensure_editable_files_ready
from autohelix.workspace import (SCRUBBED_GIT_ENV, ATTR_SOURCE_LIMITATION, ITERATION_DIR, CandidateRejected,
                                 Repo, Scope)
from autohelix.state import (
    GitExcludeError,
    ensure_runtime_ignored,
)


_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class AutoHelixRunError(RuntimeError):
    """A run could not safely continue because infrastructure failed."""


@dataclass
class _Attempt:
    """What an iteration attempt has spent and checked so far, for its record."""

    started_at: float
    monotonic: float
    agent_usage: dict = field(default_factory=dict)
    agent_attempted: bool = False
    evaluation: "_Evaluation | None" = None


@dataclass
class _Evaluation:
    """What an evaluation (gate commands, observe entries, metric gates) has run and
    checked so far: filled in as it goes, so a rejection or error keeps it."""

    gates: list[dict]  # IterationResult.gates
    constraints: list = field(default_factory=list)  # the gate commands' CommandResults
    observe: list[dict] = field(default_factory=list)  # IterationResult.observe
    metrics: dict[str, float] = field(default_factory=dict)  # from the entries that succeeded
    rejection: str | None = None
    failure_output: str | None = None


def _tail(output: str, lines: int = 15) -> str:
    return "\n".join(output.strip().splitlines()[-lines:])


class _IterationSpinner:
    """A Rich renderable showing iteration progress across all phases.

    Displays: ⠹ iter N (phase) ─ detail ─ time
    Phases: agent → checks → benchmark
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
                 land_mode: str | None = None, create: bool = True, group: str | None = None):
        """Own one run: `run_id`, a new one (`new`, as `autohelix run`), or the config's
        most recently active run (as `autohelix resume`; created if there is none,
        unless `create` is false).

        A new run records its landing mode (`land_mode`, else the config's `land:`,
        else auto, or manual with a `group`) and its `group`. On resume `land_mode`
        and `group` change the stored ones (`group=''` clears it) once the segment
        starts; otherwise the run keeps its own."""
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
        # Launched from a git hook or `rebase -x`, inherited GIT_DIR and friends would send
        # every child's git (agent, checks, judges) to the user's repository.
        for key in SCRUBBED_GIT_ENV:
            os.environ.pop(key, None)
        self._preflight_done = False
        self.paths, self.run_info, self._ownership, notices = select_run(
            self.project_path, config_file or self.project_path / "autohelix.yaml",
            new=new, run_id=run_id, name=name if name is not None else self.config.name,
            create=create, ready=self._first_start_ready, group=group or None,
            # A group lands manually unless --land says otherwise: the config's `land:` is
            # the default for runs on their own (auto landing in a group diverges the rest).
            land_mode=land_mode or ("manual" if group else self.config.land or "auto"),
        )
        # Stored on resume when the segment starts (`_run`): the mode and group asked for.
        self._identity_changes: dict = {}
        if land_mode and self.run_info.get("land") != land_mode:
            self._identity_changes["land"] = land_mode
        if group is not None and (self.run_info.get("group") or "") != group:
            self._identity_changes["group"] = group or None
            short = short_id(self.paths.run_id)
            notices.append(("info", f"{short} joins group {group} from its next iteration." if group
                            else f"{short} leaves group {self.run_info['group']}."))
        # Runs from before landing modes were recorded follow the config, as they did.
        self.land_mode = land_mode or self.run_info.get("land") or self.config.land or "auto"
        for level, text in notices:
            self.console.print(f"[yellow]{text}[/yellow]" if level == "warn" else f"[dim]{text}[/dim]", highlight=False)
        try:
            self.history = History(self.project_path, self.paths.run_id)
            tip, warning = reconcile(self.paths, self.run_info, self.history, self.land_mode)
            self.run_state = RunState(self.paths, self.run_info, tip)
            write_text(self.paths.root / "current", self.paths.run_id + "\n")
            self.repo = Repo(self.project_path)
            self.run_status = RunStatus(self.project_path, self.paths.run_id)
            self.log = RunLog(self.project_path, self.paths.run_id)
            self.run_state.copies_root = claim_copies(self.paths, self.log.warn)
            self._land_attempted = False
            self._segment_started = False
            self._usage_starts: set = set()
            self._segment_target_tip: str | None = None
            self._agent_logs = {}
            if warning:
                self.console.print(f"[yellow]{warning}[/yellow]")

            self._time_left_script: Path | None = None
            self._claude_settings_path: Path | None = None
            self._codex_time_left_hook_path: Path | None = None

            # Install optional helper scripts. Claude Code hooks use a shared
            # accumulator, written once to claude_settings.json.
            claude_hooks: dict[str, list] = {}
            if self.config.agent.timeout is not None:
                self._time_left_script = self._install_time_left(claude_hooks)
            if self.config.agent.require_notes is not None:
                self._install_require_notes(claude_hooks)
            self._write_claude_settings(claude_hooks)

            self.agent = create_agent(self.config.agent, heartbeat_seconds=heartbeat_seconds)
            self._check_codex_time_left()

        except BaseException:
            self.close()
            raise

    def _metric_label(self, name: str) -> str:
        """Format metric name with direction arrow."""
        directions = self.config.metric_directions()
        arrow = "↓" if directions.get(name) == "lower" else "↑"
        return f"{name} {arrow}"

    def _build_commit_message(self, iteration: int, metrics: dict[str, float], summary: str | None = None) -> str:
        """Build a commit message with metric deltas and the agent's summary (first line
        of its `.autohelix-iteration/commit_summary.txt`, read when the copy was frozen)."""
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

    def _new_evaluation(self) -> _Evaluation:
        """Every gate in list order, none reached yet."""
        return _Evaluation(gates=[{"text": gate.text, "kind": "metric" if isinstance(gate, MetricGate) else "command",
                                   "passed": None} for gate in self.config.gates])

    def _check_metric_gate(self, gate: MetricGate, item: dict, metrics: dict[str, float], records) -> str | None:
        """Compare one metric gate with the last accepted iteration and record it in
        `item`. Returns the rejection reason, or None if it passes."""
        direction = self.config.metric_directions().get(gate.metric, "higher")
        value, reference = metrics.get(gate.metric), metric_reference(records, gate.metric)
        failure = metric_gate_failure(gate, direction, value, reference)
        item.update(passed=failure is None, value=value, reference=reference)
        if failure:
            item["reason"] = failure
        return failure

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
        agent_type = self.config.agent.backend
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

        # Gates — short list
        if self.config.gates:
            if len(self.config.gates) == 1:
                self.console.print(f"  [dim]Gates[/dim]      {self.config.gates[0].text}", highlight=False)
            else:
                self.console.print(f"  [dim]Gates[/dim]      {len(self.config.gates)} gates", highlight=False)

        # Budget
        budget_parts = [f"{max_iter} iterations"]
        if self.config.agent.timeout is not None:
            budget_parts.append(f"{_format_time(self.config.agent.timeout)}/iter")
        if self.config.max_cost_usd is not None:
            cost = self.config.max_cost_usd
            # Whole dollars show no decimals; sub-dollar caps need cents ($0.10, not $0).
            cost_str = f"${cost:.0f}" if cost == int(cost) else f"${cost:.2f}"
            budget_parts.append(f"{cost_str} max")
        if self.config.max_time_seconds is not None:
            budget_parts.append(f"{_format_time(self.config.max_time_seconds)} total")
        self.console.print(f"  [dim]Budget[/dim]     {', '.join(budget_parts)}", highlight=False)

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

        agent_type = self.config.agent.backend.lower()
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
            # .autohelix-iteration/ and doesn't flag/delete it as an out-of-scope change.
            (worktree_path / ".opencode" / ".gitignore").write_text("*\n")
        except OSError:
            pass

    def _install_require_notes(self, hooks: dict[str, list]) -> None:
        """Install the require-notes Stop hook (claude only).

        Registers a Stop hook that blocks the agent from ending its turn until the
        iteration's notes file has enough content. Adds it to `hooks`. No-op for
        non-claude backends (Stop hooks are a Claude Code feature).
        """
        if self.config.agent.backend.lower() != "claude":
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

    def _check_codex_time_left(self) -> None:
        """With `agent.timeout`, Codex gets time-left hints only if its CLI supports the
        hook; otherwise warn and run without them (the deadline still holds)."""
        if self.config.agent.timeout is None or self.config.agent.backend.lower() != "codex":
            return
        hook = self._codex_time_left_hook_path
        if hook is None:
            message = "Could not install the Codex time-left hook."
        else:
            from autohelix.agents.codex import CodexAgent

            agent = self.agent if isinstance(self.agent, CodexAgent) else CodexAgent(self.config.agent)
            message = agent._validate_time_left_hook_support(str(hook))
        if message:
            self._codex_time_left_hook_path = None
            text = f"{message} Running without time-left hints; agent.timeout still stops the agent."
            self.console.print(f"[yellow]Warning:[/yellow] {text}", highlight=False)
            self.log.warn(text)

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

    def build_prompt(self, iteration: int, worktree_dir: Path, members=()) -> str:
        """Build the prompt for the agent using the template; `members` are the other
        runs of the group, as `group_members` returns them."""
        template = load_template(self.config)
        variables = build_prompt_variables(
            self.config, self.history, iteration, worktree_dir,
            group=self.run_info.get("group"), members=members,
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
            self.config.agent.timeout is not None
            and self.config.agent.backend.lower() == "opencode"
        ):
            self._install_opencode_timer_plugin(worktree_path)

        if self.verbose:
            print(f"  Prompt: {prompt_path}")
            print(f"  Log: {log_path}")
            print(f"  Heartbeat: {self.heartbeat_seconds}s")

        deadline_env = {
            "AUTOHELIX_PROJECT": str(self.project_path),
            "AUTOHELIX_RUN_ID": self.paths.run_id,
            "AUTOHELIX_RUN_DIR": str(self.paths.run_dir),
            "AUTOHELIX_ITERATION_START": str(int(time.time())),
            "AUTOHELIX_ITERATION_BUDGET": (
                str(self.config.agent.timeout)
                if self.config.agent.timeout is not None
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
                worktree_path / ITERATION_DIR / "notes" / f"iter-{iteration}.md"
            )
            # Retry counter lives under runtime/state/ so it never clutters
            # the agent's notes dir. Reset here so each iteration starts fresh.
            retry_file = (
                worktree_path / ITERATION_DIR / "runtime" / "state" / f"iter-{iteration}.retries"
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
            # dict — so gating only on agent.timeout would make require_notes a
            # silent no-op when set on its own.
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

    def _live_call(self, role, log):
        calls = list(self.run_status.data.get("calls", []))
        calls.append({"role": role, "log": self._relative_log(log), "usage": {}})
        self.run_status.note(calls=calls)

    def _rejected(self, iteration: int, attempt: "_Attempt", reason: str, **fields) -> IterationResult:
        return IterationResult(iteration=iteration, accepted=False, reason=reason,
                               calls=[{"role": "agent", "usage": attempt.agent_usage}], **fields)

    def run_iteration(self, iteration: int) -> IterationResult:
        """Run a single iteration and record it, whatever happens after the agent call."""
        attempt = _Attempt(started_at=time.time(), monotonic=time.monotonic())
        self.log.info(f"iter {iteration} started")
        self.run_status.phase("agent", iteration)
        self.run_status.note(calls=[])
        self._start_command_usage(iteration)
        workspace = IterationWorkspace(iteration, self.run_state, self.history, self.repo, warn=self.log.warn)
        result: IterationResult | None = None
        failure: BaseException | None = None
        interrupted_after_accept = False
        try:
            result = self._attempt(workspace, attempt, iteration)
        except CandidateRejected as e:
            self.log.info(f"iter {iteration} rejected — {e}")
            result = self._rejected(iteration, attempt, str(e))
        except KeyboardInterrupt as e:
            if workspace.recorded:
                result, interrupted_after_accept = workspace.result, True
            else:
                failure = e
        except Exception as e:
            failure = e if isinstance(e, AutoHelixRunError) else AutoHelixRunError(
                f"Iteration {iteration} failed unexpectedly: {e}")
            if workspace.recorded:
                result = workspace.result
            self.log.error(str(failure))
        finally:
            if result is None:
                result = self._rejected(iteration, attempt, (str(failure) or "interrupted") if failure else "interrupted")
            try:
                self._complete_record(result, attempt.started_at, attempt.monotonic, attempt.evaluation)
                # Failures before the agent call don't consume the iteration.
                if attempt.agent_attempted:
                    workspace.record(result)
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

    def _attempt(self, workspace: IterationWorkspace, attempt: "_Attempt", iteration: int) -> IterationResult:
        """Agent, freeze, gate commands, observe, metric gates, accept, stop: returns at
        the first rejection."""
        if self.verbose:
            self._status("Creating agent copy...")
        agent_copy = workspace.agent_copy()
        if self.verbose:
            self.console.print(f"  Working dir: {agent_copy.working_dir}")
        self.run_status.note(worktree=str(agent_copy.working_dir))
        prompt = self.build_prompt(iteration, agent_copy.working_dir, workspace.members)
        workspace.write_prompt(prompt)
        if self.verbose:
            self.console.print(Panel(prompt.strip(), title="[bold]Agent Prompt[/bold]", border_style="dim"))
            self.console.print()
            self._status("Running agent...")
        with self._watch_checkout():
            attempt.agent_attempted = True
            agent_result = self.run_agent(agent_copy.working_dir, prompt, iteration)
            attempt.agent_usage = agent_result.usage or {}
        duration_s = attempt.agent_usage.get("duration_ms", 0) / 1000
        self.log.info(f"iter {iteration} agent complete — {duration_s:.0f}s, "
                      f"{format_cost(attempt.agent_usage.get('cost_usd'), precision=4)}")
        if not agent_result.success:
            if self.verbose:
                self._status("Agent did not complete successfully")
            detail = agent_result.error or f"agent exited with status {agent_result.exit_code}"
            raise AutoHelixRunError(f"Iteration {iteration} agent failed: {detail}")

        # Freeze the candidate: out-of-scope changes never reach the tree.
        # The viewer shows the pin from "preparing checks" on (viewer.FROZEN_PHASES).
        discarded = workspace.freeze(Scope(self.repo.prefix, tuple(self.config.editable), tuple(self.config.frozen)),
                                     pinned=lambda: self.run_status.phase("preparing checks"))
        self.run_status.note(worktree=None)
        if discarded and self.verbose:
            reverted = [name.removeprefix(self.repo.prefix) for name in discarded]
            self.console.print(f"  [yellow]![/yellow] Discarded {len(reverted)} change(s) outside editable scope:")
            for f in reverted[:5]:
                self.console.print(f"    [dim]{f}[/dim]")
            if len(reverted) > 5:
                self.console.print(f"    [dim]... and {len(reverted) - 5} more[/dim]")

        evaluation = attempt.evaluation = self._new_evaluation()
        self._evaluate(workspace, iteration, evaluation)
        metrics = evaluation.metrics
        if self.verbose:
            for name, value in metrics.items():
                self.console.print(f"  {self._metric_label(name)} [cyan]{value}[/cyan]")
        if evaluation.rejection:
            return self._rejected(iteration, attempt, evaluation.rejection, metrics=metrics,
                                  failure_output=evaluation.failure_output)

        # Publish the validated tree to this run, independently of landing.
        self.run_status.phase("accept")
        if self.verbose:
            self._status("Accepting changes...")
        result = IterationResult(iteration=iteration, accepted=True, metrics=metrics,
                                 calls=[{"role": "agent", "usage": attempt.agent_usage}])
        self._complete_record(result, attempt.started_at, attempt.monotonic, evaluation)
        workspace.accept(self._build_commit_message(iteration, metrics, summary=workspace.summary), result)

        # Stop commands see accepted content in a fresh copy. Errors (and interrupts,
        # handled by the caller) keep the recorded acceptance.
        if self.config.stop:
            try:
                result.stop, _ = self._check_stop(workspace, iteration, metrics)
            except Exception as e:  # noqa: BLE001 - acceptance already happened
                self.console.print(f"  [yellow]![/yellow] Stop check failed: {e}")
                self.log.info(f"iter {iteration} stop check failed — {e}")
        return result

    def _gate_text(self, command: str) -> str:
        """A gate command's display text (`ask: <question>` for ask entries)."""
        return next((gate.text for gate in self.config.gate_commands() if gate.command == command), command)

    def _candidate_env(self, workspace: IterationWorkspace) -> dict[str, str]:
        """What gate, observe and stop commands are told about the candidate: its tree
        (T; A's tree at the baseline), the commit it is built on (A), and the project."""
        return {
            "AUTOHELIX_CANDIDATE": workspace.candidate(),
            "AUTOHELIX_BASE": workspace.base,
            "AUTOHELIX_PROJECT": str(self.project_path),
        }

    def _evaluate(self, workspace: IterationWorkspace, iteration: int, evaluation: _Evaluation) -> None:
        """Evaluate T in a checks copy (HEAD at A), verifying T's files after every command,
        filling in `evaluation` as it goes:

        1. gate commands in list order; the first failure rejects and skips the rest;
        2. observe entries top to bottom, each output saved under
           `observations/iter-N/<name>/`; a failed entry contributes no metrics and the
           next one runs. After each entry, the metric gates on its metrics (gates list
           order); a failure rejects and skips the remaining entries.

        At the baseline (iteration 0) nothing rejects: every gate command and entry runs
        and metric gates aren't checked. Raises CandidateRejected if a command or judge
        changed the candidate."""
        baseline = iteration == 0
        checks = workspace.eval_copy("checks")
        command_env = {**self._command_env(iteration), **self._candidate_env(workspace),
                       "AUTOHELIX_OBSERVATIONS": None}
        self.history.clear_observations(iteration)  # a baseline retry's earlier attempt
        records = self.history.load()  # the reference for metric gates: the last accepted iteration
        where = "baseline" if baseline else f"iter {iteration}"
        notes: list[str] = []  # printed once the live display is gone

        def verify(command: str) -> None:
            # Checks are read-only: one command must not alter what a later one validates.
            try:
                changed = checks.changed()
            except RuntimeError:
                changed = ["its copy was removed or replaced"]
            if changed:
                raise CandidateRejected(f"checks modified candidate files: {command} ({', '.join(changed[:5])})")
            if violation := self._integrity_violation(iteration):
                raise CandidateRejected(violation)

        @contextmanager
        def gate_call(_command: str):
            with workspace.call_folder() as folders:
                yield {"AUTOHELIX_SCRATCH": str(folders["scratch"]), "AUTOHELIX_PREVIOUS": None,
                       "PATH": command_path(folders["bin"])}

        positions = {id(gate): index for index, gate in enumerate(self.config.gates)}

        def gate_result(spec, result) -> None:
            evaluation.constraints.append(result)
            item = evaluation.gates[positions[id(spec)]]
            item.update(passed=result.passed, return_code=result.return_code,
                        duration_s=None if result.duration_s is None else round(result.duration_s, 3))
            if not result.passed:
                item["output"] = _tail(result.output)
            self.log.info(f"{where} gate: {spec.text} — {'passed' if result.passed else 'FAILED'}")

        spinner = _IterationSpinner(iteration)
        spinner.set_phase("checks")
        progress = None if baseline else (lambda _phase, command, _elapsed: spinner.set_detail(command))
        if not baseline:
            self.log.info(f"iter {iteration} gates started")
            self.run_status.phase("checks")
            if self.verbose:
                self._status("Running gates...")
        display = (Status("Running baseline...", console=self.console, spinner="dots") if baseline
                   else Live(spinner, console=self.console, refresh_per_second=10, transient=True))
        with display:
            run_gate_commands(self.config.gate_commands(), checks.working_dir, project_path=self.project_path,
                              extra_env=command_env, after_command=verify, on_progress=progress,
                              stop_on_failure=not baseline, per_command=gate_call, on_result=gate_result)
            failed = next((r for r in evaluation.constraints if not r.passed), None)
            # At the baseline, gate commands only guard against mutation; failing is fine.
            if failed is not None and not baseline:
                evaluation.rejection = f"gate failed: {self._gate_text(failed.command)}"
                evaluation.failure_output = _tail(failed.output)
            elif self.config.observe:
                spinner.set_phase("benchmark")
                spinner.set_detail("")
                if not baseline:
                    self.run_status.phase("benchmark")
                    self.log.info(f"iter {iteration} observe started")
                self._observe(workspace, iteration, checks, evaluation, command_env, records,
                              verify=verify, progress=progress, notes=notes)

        if not baseline:
            for r in evaluation.constraints:
                if not r.passed:
                    self.console.print(f"  [red]✗[/red] {self._gate_text(r.command)}", highlight=False)
                    if self.verbose:
                        self.console.print(Panel(r.output[:2000], title="Gate output", border_style="red"))
                elif self.verbose:
                    self.console.print(f"  [green]✓[/green] {self._gate_text(r.command)}", highlight=False)
        for note in notes:
            self.console.print(note, highlight=False)
        workspace.remove(checks)
        if evaluation.metrics and not baseline:
            self.log.info(f"iter {iteration} observe: "
                          + ", ".join(f"{k}={v:g}" for k, v in evaluation.metrics.items()))
        if violation := self._integrity_violation(iteration):
            raise CandidateRejected(violation)

    def _observe(self, workspace: IterationWorkspace, iteration: int, checks, evaluation: _Evaluation,
                 command_env: dict, records, *, verify, progress, notes: list[str]) -> None:
        """Observe entries in order (see `_evaluate`), each with its own call folder and,
        as `$AUTOHELIX_PREVIOUS`, a copy of its output from the last accepted iteration."""
        baseline = iteration == 0
        where = "baseline" if baseline else f"iter {iteration}"
        gates = [(index, gate) for index, gate in enumerate(self.config.gates) if isinstance(gate, MetricGate)]
        declared = self.config.metric_names()
        for entry in self.config.observe:
            previous = self.history.previous_observations(entry.name, iteration)
            with workspace.call_folder(previous=previous) as folders:
                env = {**command_env, "AUTOHELIX_SCRATCH": str(folders["scratch"]),
                       "AUTOHELIX_PREVIOUS": str(folders["previous"]) if "previous" in folders else None,
                       "PATH": command_path(folders["bin"])}
                result = run_observe_entry(entry, checks.working_dir, project_path=self.project_path,
                                           on_progress=progress, extra_env=env)
            # Stored before anything can reject: a rejected iteration keeps its output.
            self.history.save_stdout(iteration, entry.name, result.output)
            for capture in entry.capture:
                if problem := self.history.save_capture(iteration, entry.name, checks.working_dir, capture):
                    self.log.warn(f"{where} observe {entry.name}: {problem}")
            evaluation.observe.append(result.record())
            if result.ok:
                evaluation.metrics.update(result.values)
                for name, error in result.errors.items():
                    self.log.info(f"{where} observe {entry.name}: metric '{name}' missing — {error}")
            else:
                notes.append(f"  [yellow]![/yellow] observe {entry.name} failed ({result.error}); "
                             "its metrics are missing")
                self.log.info(f"{where} observe {entry.name} failed — {result.error}")
            verify(entry.command)
            if baseline:
                continue
            # Metric gates waiting for this entry, in gates list order; a gate on a
            # metric nobody declares (not a valid config) is checked after the last entry.
            last = entry is self.config.observe[-1]
            for index, gate in gates:
                if not (gate.metric in entry.metrics or last and gate.metric not in declared):
                    continue
                if failure := self._check_metric_gate(gate, evaluation.gates[index], evaluation.metrics, records):
                    notes.append(f"  [red]✗[/red] {failure}")
                    self.log.info(f"iter {iteration} {failure}")
                    evaluation.rejection = failure
                    return

    @contextmanager
    def _watch_checkout(self):
        """Advisory: warn if the user's checkout changes while the agent runs (an import escape)."""
        try:
            before = self._checkout_probe()
        except Exception as e:  # noqa: BLE001
            before = None
            self.log.warn(f"Could not inspect main checkout for import escapes: {e}")
        try:
            yield
        finally:
            try:
                if before is not None and self._checkout_probe() != before:
                    self.log.warn("Main checkout changed during agent phase; review git diff for an import escape or user edits.")
            except Exception as e:  # noqa: BLE001
                self.log.warn(f"Could not inspect main checkout for import escapes: {e}")

    def _checkout_probe(self):
        """Tracked-only, advisory import-escape inspection of the user's checkout."""
        def run(*args):
            return self.repo.git(*args, work_tree=self.repo.root, cwd=self.project_path, user_checkout=True).stdout

        return hashlib.sha256(run("status", "--porcelain", "--untracked-files=no")
                              + run("diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--")).digest()

    def _relative_log(self, path):
        return self.paths.relative_log(path)

    def _complete_record(self, result, started_at, iter_start, evaluation: _Evaluation | None = None):
        from dataclasses import asdict

        result.started_at = started_at
        result.ended_at = time.time()
        if evaluation is not None:
            result.constraints = [asdict(check) for check in evaluation.constraints]
            result.gates = [dict(item) for item in evaluation.gates]
            result.observe = [dict(item) for item in evaluation.observe]
        else:
            result.constraints, result.gates, result.observe = [], [], []
        agents = [call for call in result.calls if call["role"] == "agent"]
        for call in agents:
            call["usage"]["wall_clock_ms"] = int((time.monotonic() - iter_start) * 1000)
            call["log"] = self._relative_log(self._agent_logs.get(result.iteration))
        commands = []
        for record in read_usage(self._usage_path(result.iteration)):
            usage = dict(record)
            log = usage.pop("log", None)
            commands.append({"role": "command", "usage": usage, "log": self._relative_log(log)})
        result.calls = agents + commands

    def _integrity_violation(self, iteration: int) -> str | None:
        """A judge call in this attempt changed the files it judged (sticky)."""
        for usage in read_usage(self._usage_path(iteration)):
            if usage.get("id") not in self._usage_starts and usage.get("integrity_violation"):
                return f"a judge call changed the files it judged ({usage.get('source', 'judge')})"
        return None

    def _usage_path(self, iteration: int) -> Path:
        return self.paths.runtime / "usage" / f"iter-{iteration}.jsonl"

    def _start_command_usage(self, iteration: int) -> None:
        """Start an empty usage file for the commands this iteration runs. A baseline
        retry keeps the earlier attempts' calls, which still count toward its cost."""
        path = self._usage_path(iteration)
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = next((r for r in self.history.load() if r.iteration == iteration), None)
        if previous is None or previous.accepted:
            path.write_text("")
        self._usage_starts = {r.get("id") for r in read_usage(path)}

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

    def _check_stop(self, workspace: IterationWorkspace, iteration: int,
                    metrics: dict[str, float]) -> tuple[dict[str, str] | None, list]:
        """Run the stop commands in a fresh copy of the accepted tip. Returns (stop,
        errors): stop is {"command", "reason"} if one exited 0; errors are the results
        that exited with anything other than 0 or 1 (or timed out). Changing the
        files or a judge violation is an error.

        Each command also gets `$AUTOHELIX_OBSERVATIONS`, a copy (made for that call)
        of the accepted iteration's `observations/iter-N/` (empty if it observed
        nothing), and its own `$AUTOHELIX_SCRATCH`."""
        self.run_status.phase("stop check", iteration)
        runtime = self.paths.runtime
        runtime.mkdir(parents=True, exist_ok=True)
        metrics_path = runtime / "metrics.json"
        metrics_path.write_text(json.dumps(metrics))
        env = {**self._command_env(iteration), **self._candidate_env(workspace),
               "AUTOHELIX_METRICS": str(metrics_path), "AUTOHELIX_PREVIOUS": None}
        observations = self.paths.observations / f"iter-{iteration}"

        @contextmanager
        def stop_call(_command: str):
            with workspace.call_folder(observations=observations) as folders:
                yield {"AUTOHELIX_SCRATCH": str(folders["scratch"]),
                       "AUTOHELIX_OBSERVATIONS": str(folders["observations"]),
                       "PATH": command_path(folders["bin"])}

        stop = workspace.stop_copy()
        try:
            with Status("Checking stop conditions...", console=self.console, spinner="dots"):
                results = run_stop_checks(self.config, stop.working_dir, project_path=self.project_path, extra_env=env,
                                          per_command=stop_call)
            if changed := stop.changed():
                raise AutoHelixRunError("stop commands modified accepted files: " + ", ".join(changed[:5]))
        finally:
            workspace.remove(stop)
        errors = [r for r in results if r.error]
        for r in errors:
            code = ("timed out" if r.output.startswith("Timeout after")
                    else "could not run" if r.return_code == -1 else f"exited {r.return_code}")
            self.console.print(f"  [yellow]![/yellow] Stop command {code}: {r.command}", highlight=False)
            tail = "\n".join(r.output.strip().splitlines()[-5:])
            if tail:
                self.console.print(f"[dim]{tail}[/dim]", highlight=False)
            self.log.info(f"iter {iteration} stop command error ({code}): {r.command}")
        if violation := self._integrity_violation(iteration):
            raise AutoHelixRunError(violation)
        fired = next((r for r in results if r.stop), None)
        if fired is None:
            return None, errors
        self.log.info(f"iter {iteration} stop condition met — {fired.command}: {fired.reason}")
        return {"command": fired.command, "reason": fired.reason}, errors

    def _baseline_workspace(self) -> IterationWorkspace:
        workspace = IterationWorkspace(0, self.run_state, self.history, self.repo, warn=self.log.warn)
        workspace.unchanged()
        return workspace

    def _write_baseline(self, baseline: IterationResult) -> None:
        if any(r.iteration == 0 for r in self.history.load()):
            self.history.update(baseline)
        else:
            self.history.append(baseline)

    def _capture_baseline(self) -> dict[str, str] | None:
        """Evaluate A in a checks copy and check stop commands.

        Returns the stop condition the baseline already meets, if any."""
        workspace = self._baseline_workspace()
        evaluation = self._new_evaluation()
        try:
            self.log.info("baseline capture started")
            self.run_status.phase("baseline", 0)
            self._start_command_usage(0)
            try:
                self._evaluate(workspace, 0, evaluation)
            except CandidateRejected as e:
                # A mutating check would reject every iteration; refuse before agent spend.
                reason = str(e)
                if reason.startswith("checks modified candidate files: "):
                    reason = ("Baseline command modified candidate files: "
                              + reason.removeprefix("checks modified candidate files: ")
                              + ". Checks must not change the code they check; for example, run a "
                              "formatter in its check-only mode.")
                else:
                    reason = f"Baseline setup error: {reason}"
                self.log.info(f"baseline FAILED — {reason}")
                raise AutoHelixRunError(reason) from e

            # A missing number is a setup error, except a review's that no gate uses: an
            # LLM's answer missing once may be flakiness. A failed entry produces none.
            metrics = evaluation.metrics
            missing = set(self.config.metric_directions()) - set(metrics)
            failures = {item["name"]: item["error"] for item in evaluation.observe if not item["ok"]}

            def why(names) -> str:
                entries = dict.fromkeys(self.config.metric_source(name).name for name in sorted(names))
                failed = [f"{name} {failures[name]}" for name in entries if name in failures]
                return f" ({'; '.join(failed)})" if failed else ""
            gated = {gate.metric for gate in self.config.metric_gates()}
            advisory = {name for name in missing
                        if self.config.metric_source(name).review is not None and name not in gated}
            if advisory:
                text = f"Baseline review did not report metric(s): {', '.join(sorted(advisory))}{why(advisory)}"
                self.console.print(f"  [yellow]![/yellow] {text}", highlight=False)
                self.log.warn(text)
            if missing - advisory:
                names = ", ".join(sorted(missing - advisory))
                self.log.info(f"baseline FAILED — missing metrics: {names}")
                raise RuntimeError(f"Baseline benchmark did not produce required metric(s): {names}"
                                   f"{why(missing - advisory)}")
            if metrics:
                metric_str = ", ".join(f"{name}: {value:g}" for name, value in metrics.items())
                self.console.print(f"  baseline │ {metric_str}", highlight=False)
                self.log.info(f"baseline: {metric_str}")

            baseline = IterationResult(iteration=0, accepted=True, metrics=metrics, commit=None, reason="baseline")
            if self.config.stop:
                # A broken stop command fails here, before anything is recorded, so a
                # typo surfaces before the first iteration is paid for.
                baseline.stop, errors = self._check_stop(workspace, 0, metrics)
                if errors:
                    self.log.info("baseline FAILED — stop command error")
                    raise AutoHelixRunError(
                        f"Stop command failed on the baseline: {errors[0].command} "
                        "(expected exit 0 to stop or 1 to continue)"
                    )
            self._complete_record(baseline, time.time(), time.monotonic(), evaluation)
            self._write_baseline(baseline)
            return baseline.stop
        except BaseException as error:
            baseline = IterationResult(iteration=0, accepted=False, reason=f"baseline failed: {error}")
            self._complete_record(baseline, time.time(), time.monotonic(), evaluation)
            if any(r.iteration == 0 and r.accepted for r in self.history.load()):
                raise
            self._write_baseline(baseline)
            raise
        finally:
            workspace.close()

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
                try:
                    # Only the live view reads the pin, while an iteration is judged.
                    git(self.project_path, "update-ref", "-d", self.paths.pin, check=False)
                    remove_copies(self.run_state.copies_root, self.log.warn)
                finally:
                    self.close()

    def close(self) -> None:
        if hasattr(self, "_ownership"):
            self._ownership.close()

    def __del__(self):
        self.close()

    def _first_start_ready(self) -> None:
        """Readiness checks for a first start, run before any run state exists."""
        ensure_editable_files_ready(Repo(self.project_path), self.project_path, self.config.editable)
        if not self._preflight():
            raise RuntimeError("Preflight checks failed. Fix the issues above before running.")
        self._preflight_done = True

    def _preflight(self) -> bool:
        """Print preflight warnings and errors; False if there are errors."""
        issues = preflight_check(self.config, self._raw_config, self.project_path)
        for w in (i for i in issues if i.level == "warning"):
            self.console.print(f"  [yellow]WARN[/yellow] {w.message}")
        errors = [i for i in issues if i.level == "error"]
        for e in errors:
            self.console.print(f"  [red]FAIL[/red] {e.message}")
        if errors:
            self.console.print(
                "\n[red]Preflight checks failed.[/red] Fix the issues above before running."
            )
        return not errors

    def _target_tip(self) -> str | None:
        target = self.run_info.get("target_branch")
        return git(self.project_path, "rev-parse", "--verify", f"refs/heads/{target}",
                   check=False).stdout.strip() if target else None

    def _land(self) -> None:
        """Auto-land at segment end; in every mode, refuse/warn if the target moved."""
        if not self._segment_started or self._land_attempted:
            return
        self._land_attempted = True
        target = self.run_info.get("target_branch")
        try:
            if target and self._segment_target_tip is not None and self._target_tip() != self._segment_target_tip:
                if self.land_mode == "auto":
                    raise AutoHelixRunError(
                        f"Target {target} changed during this segment; leaving user commits untouched. "
                        f"Review them, then use autohelix land {short_id(self.paths.run_id)} explicitly.")
                message = (f"Target {target} changed during this segment. Review those commits "
                           f"before autohelix land {short_id(self.paths.run_id)}.")
                self.console.print(f"[yellow]{message}[/yellow]", highlight=False)
                self.log.warn(message)
            if self.land_mode == "auto":
                land(self.paths, self.run_info, self.run_state.expected_tip)
                self.console.print(f"[dim]Landed run {short_id(self.paths.run_id)} into {target}.[/dim]")
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
            ensure_clean_working_tree(self.repo, self.project_path)
            if git(self.project_path, "rev-parse", "HEAD").stdout.strip() != self.run_state.expected_tip:
                raise AutoHelixRunError("Baseline checkout moved since this run started. Restore its original commit, "
                                        "or start a new run with `autohelix run` (and `autohelix delete "
                                        f"{short_id(self.paths.run_id)}` this one).")
            ensure_editable_files_ready(self.repo, self.project_path, self.config.editable)

        # Show header
        self._print_header(max_iter, start_iter)
        remove_copies(self.run_state.copies_root, self.log.warn)  # left by a segment that crashed
        if not self.repo.attr_source:
            self.console.print(f"  [yellow]WARN[/yellow] {ATTR_SOURCE_LIMITATION}")
        if (self.project_path / ".autohelix" / "prompt.md").exists():
            self.console.print("  [yellow]WARN[/yellow] .autohelix/prompt.md is no longer read: the built-in "
                               "prompt is used. To keep a custom prompt, move it next to your config, set "
                               "`prompt_template: <file>` there, and update its .autohelix/ paths to "
                               f"{ITERATION_DIR}/. Otherwise delete it.", highlight=False)
        if self.config.prompt_template and uses_old_layout(load_template(self.config)):
            self.console.print(f"  [yellow]WARN[/yellow] {self.config.prompt_template.name} names .autohelix/ paths "
                               f"in the agent's copy; they are now {ITERATION_DIR}/ (notes written to the old "
                               "place are lost).", highlight=False)

        # Run preflight checks before the baseline (silent on success); a first
        # start already ran them before allocating the run.
        if needs_baseline and not self._preflight_done and not self._preflight():
            return False

        self.run_status.start(max_iterations=max_iter, run_id=self.paths.run_id)
        self._segment_started = True
        self._segment_target_tip = self._target_tip()
        flags = [self.run_info.pop("archived", None), self.run_info.pop("lifecycle", None)]
        for key, value in self._identity_changes.items():  # `resume --land` / `--group`
            if value is None:
                self.run_info.pop(key, None)
            else:
                self.run_info[key] = value
        if any(flags) or self._identity_changes:
            write_json(self.paths.identity, self.run_info)  # resuming brings an archived run back

        # Capture baseline on first run
        baseline_stop = None
        if start_iter == 1 and needs_baseline:
            baseline_stop = self._capture_baseline()

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
        loop_start = time.monotonic()  # monotonic counts from boot: never subtract prior time from it

        config_name = self._resolve_config_rel_path()
        self.log.info(f"run started — config: {config_name}, max_iter: {max_iter}, resuming from iter {start_iter}")

        ran = False
        cost_warning_shown = False
        if self.config.max_cost_usd is not None:
            cost_warning_shown = prior_cost.incomplete
            if cost_warning_shown:
                self.console.print(
                    "[yellow]Warning: prior agent or command calls have unavailable cost.\n"
                    "budget.cost can only track known costs.[/yellow]"
                )

        # Why the loop ended; budgets are checked after stop conditions.
        stop_reason: dict | None = None
        if baseline_stop:
            stop_reason = self._condition_stop(baseline_stop, 0)
        cost_reached = {"kind": "budget", "budget": "cost",
                        "detail": f"cost budget reached ({format_cost(self.config.max_cost_usd)})"}
        time_reached = {"kind": "budget", "budget": "time",
                        "detail": f"time budget reached ({_format_time(self.config.max_time_seconds or 0)})"}
        iteration = start_iter
        try:
            for iteration in range(start_iter, max_iter + 1):
                if stop_reason:
                    break
                if self.config.max_cost_usd is not None and cumulative_cost >= self.config.max_cost_usd:
                    stop_reason = cost_reached
                    break
                if (self.config.max_time_seconds is not None
                        and prior_elapsed_seconds + time.monotonic() - loop_start >= self.config.max_time_seconds):
                    stop_reason = time_reached
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
                elapsed = prior_elapsed_seconds + time.monotonic() - loop_start
                if self.config.max_time_seconds is not None and elapsed >= self.config.max_time_seconds:
                    stop_reason = time_reached
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
            self.console.print(f"[dim]Accepted code remains on {self.paths.ref}. Land with autohelix land {short_id(self.paths.run_id)}.[/dim]")
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
        # "+": known part only (some call's cost is unavailable); the run total says "(incomplete)".
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
