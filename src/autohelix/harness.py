# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Main iteration loop."""

import json
import hashlib
import itertools
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from autohelix.agents import AgentEvent, AgentEventKind, AgentRunResult, create_agent
from autohelix.config import ConfigError, MetricGate, config_changes, find_config, load_config, run_command
from autohelix.costs import summarize_costs
from autohelix import terminal
from autohelix.display import LiveDisplay, clean
from autohelix.terminal import LiveRow, Row, budget_text
from autohelix.history import History, IterationResult
from autohelix.runs import (RunPaths, RunState, select_run, reconcile, land, git,
                            claim_copies, config_relative, critical_section, new_run_name,
                            iteration_folder, load_run_config, planned_run, remove_copies, require_clean, short_id, landing,
                            Landing, LandingWords, landing_words, WHY_LIMIT,
                            imported_run, is_live, group_landing_note, has_baseline, land_first, target_ahead_refusal,
                            write_json)
from autohelix.iteration import IterationWorkspace
from autohelix.launcher import command_path
from autohelix.checks import (metric_gate_failure, metric_reference, preflight_check, run_gate_commands,
                              run_observe_entry, run_stop_checks, scope_warning, StopCheckResult)
from autohelix.events import iteration_fields
from autohelix.run_status import RunStatus, SegmentLog, clear_stop_request, read_segments, read_stop_request
from autohelix.usage_log import ENV as USAGE_ENV, append_usage, read_usage, superseded_marker
from autohelix.formatting import clean_block, format_cap, format_cost, format_duration, format_total_cost
from autohelix.iteration_time import format_limit
from autohelix.prompt_template import (build_prompt_variables, load_template, render_template, unknown_variables,
                                       uses_old_layout)
from autohelix.run_log import RunLog
from autohelix.workspace import (path_in_scope, SCRUBBED_GIT_ENV, ATTR_SOURCE_LIMITATION, ITERATION_DIR, OPENCODE_PLUGIN,
                                 CandidateRejected, GitExcludeError, Repo, Scope, ensure_git_exclude_entry,
                                 ensure_runtime_ignored)


class AutoHelixRunError(RuntimeError):
    """A run could not safely continue because infrastructure failed."""


# A failed or timed-out agent call is a rejected iteration and the loop goes on; this
# many in a row (within a segment) stop the run: the agent is unlikely to recover.
MAX_CONSECUTIVE_AGENT_FAILURES = 3

# Iterations a segment runs when no budget flag is given (`run`/`resume` without
# -n, --until, --cost or --time).
DEFAULT_ITERATIONS = 1
RESUME_ROWS = 5  # rows a resume shows of the iterations before it


@dataclass(frozen=True)
class Budget:
    """How long one segment (`run`, `resume`) goes on: the config says what an
    iteration does, the command line how long.

    `iterations` more iterations, or up to iteration `until` in total (the one figure
    that counts a total: safe to repeat); up to `cost_usd` more dollars charged by this
    segment (agent calls, reviews, asks, the baseline: known costs only) and
    `time_seconds` of its wall clock, baseline included. With nothing set,
    `DEFAULT_ITERATIONS` more; with only cost or time, no iteration cap."""

    iterations: int | None = None
    until: int | None = None
    cost_usd: float | None = None
    time_seconds: int | None = None

    def __post_init__(self):
        if self.iterations is not None and self.until is not None:
            raise ValueError("Use either -n (more iterations) or --until (up to an iteration), not both.")
        for flag, value in (("-n", self.iterations), ("--until", self.until), ("--cost", self.cost_usd),
                            ("--time", self.time_seconds)):
            if value is not None and (isinstance(value, bool) or not math.isfinite(value) or not value > 0):
                raise ValueError(f"{flag} must be a positive finite number, got {value!r}")

    @property
    def default(self) -> bool:
        """No figure given: `DEFAULT_ITERATIONS` more."""
        return self == Budget()

    def last_iteration(self, done: int) -> int | None:
        """The last iteration this segment may run after iteration `done`; None: no cap."""
        if self.until is not None:
            return self.until
        if self.iterations is not None:
            return done + self.iterations
        return None if self.cost_usd is not None or self.time_seconds is not None else done + DEFAULT_ITERATIONS


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


@contextmanager
def _terminate_interrupts():
    """While a segment runs, SIGTERM (`autohelix stop --now`, a plain `kill`) raises
    KeyboardInterrupt, so it ends as Ctrl-C does: the iteration recorded, its agent's
    and commands' process groups (their own sessions) killed, the run landed. Unlike
    SIGINT it is not ignored in a run a script started in the background. Only in the
    main thread, and only over the default action: a caller's own handler stays."""
    if threading.current_thread() is not threading.main_thread() or signal.getsignal(signal.SIGTERM) != signal.SIG_DFL:
        yield
        return

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)


def _call_env(folders: dict) -> dict[str, str]:
    """The variables of one command call (`IterationWorkspace.call_folder`): its scratch
    and transcript folders, its call name and the `autohelix` launcher first on PATH."""
    return {"AUTOHELIX_SCRATCH": str(folders["scratch"]), "AUTOHELIX_LOG_DIR": str(folders["logs"]),
            "AUTOHELIX_CALL": folders["call"], "PATH": command_path(folders["bin"])}


def _tail(output: str, lines: int = 15) -> str:
    return "\n".join(output.strip().splitlines()[-lines:])


class Harness:
    """Main harness for running the optimization loop."""

    def __init__(self, project_path: Path, verbose: bool = False, heartbeat_seconds: int = 30,
                 config_file: Path | None = None, *, new: bool = False,
                 run_id: str | None = None, name: str | None = None,
                 land_mode: str | None = None, create: bool = True, group: str | None = None,
                 import_run: str | None = None, why: str | None = None, check_only: bool = False):
        """Own one run: `run_id`, a new one (`new`, as `autohelix run`), or the config's
        most recently active run (as `autohelix resume`; created if there is none,
        unless `create` is false).

        A new run records its landing mode (`land_mode`, else manual with a
        `group`, else auto) and its `group`. On resume `land_mode`
        and `group` change the stored ones (`group=''` clears it) once the segment
        starts; otherwise the run keeps its own. A new run is named after the config
        file (`name` replaces that, for library callers and tests).

        A new run may import another (`import_run`, a full id: `run --import`): every
        agent copy shows that run's state read-only, and the baseline's observe entries
        get its last outputs as `$AUTOHELIX_PREVIOUS`. `why` is the new run's note
        (`run --why`). A resume refuses, before writing anything, when its target branch
        moved past its tip and it lands automatically (`runs.landing`: `behind`).

        `check_only` (`run --check`): make every check up to the first write (config,
        template, clean checkout, preflight) and stop there; the object is good for
        nothing else."""
        # Launched from a git hook or `rebase -x`, inherited GIT_DIR and friends would send
        # every git call (ours, and the agent's, checks' and judges') to another repository.
        # The CLI does the same in `cli.main`; this covers the Harness as a library.
        for key in SCRUBBED_GIT_ENV:
            os.environ.pop(key, None)
        self._ran = False  # one segment per Harness (`run`)
        self.project_path = Path(project_path).resolve()
        # `config_file` is the source file, as run.json names it (naming, messages); the run
        # loads its stored copy (`RunPaths.config`), which judges and the report read too.
        if run_id:
            stored = RunPaths(self.project_path, run_id).read_identity()["config_path"]
            if config_file is None:
                config_file = self.project_path / stored
            elif config_relative(self.project_path, Path(config_file)) != stored:
                raise RuntimeError(f"Run {short_id(run_id)} was started with {stored}, not "
                                   f"{config_relative(self.project_path, Path(config_file))}; a run keeps its config.")
        elif config_file is None:
            config_file = find_config(self.project_path)
        self.config_file = config_file = Path(config_file)
        self.verbose = verbose
        self.heartbeat_seconds = heartbeat_seconds
        self.console = Console()
        # Every refusal comes before the first write (the excludes, then .autohelix/): the
        # run to own, read-only (no run to resume, a run without its ref), the config (for
        # an existing run, its stored copy, which the file must still match), and for a new
        # run its name, a clean tree and readiness. select_run decides again under its lock.
        if import_run and not new:
            raise ValueError("import_run applies to a new run")
        planned = planned_run(self.project_path, config_file, new=new, run_id=run_id, create=create)
        creating = planned is None
        if import_run:
            source = RunPaths(self.project_path, import_run).read_identity().get("config_path")
            if not isinstance(source, str):
                raise RuntimeError(f"Run {short_id(import_run)}'s run.json names no config file; it can't be imported.")
            if source != (used := config_relative(self.project_path, config_file)):
                self._warn(f"Run {short_id(import_run)} used {source}; this run uses {used}.")
        if creating:
            self.config = load_config(self.project_path, config_file=config_file)
        else:
            self.config = self._run_config(planned)
        errors = [issue for issue in self.config.validate() if issue.level == "error"]
        if errors:
            for error in errors:
                terminal.show(self.console, error.message, Text("Error: ", style="red"))
            if creating:
                raise SystemExit(f"Config validation failed. Edit {config_relative(self.project_path, config_file)} "
                                 "and try again.")
            short = short_id(planned.run_id)
            raise SystemExit(f"Run {short}'s stored config is no longer valid; continue it in a new run: "
                             f"`{run_command(config_relative(self.project_path, config_file))} --import {short}`.")
        self._preflight_done = False
        if creating:
            name = new_run_name(config_file, name)
            self._new_run_ready()
        else:  # a run continues its own tip: refused before anything is written
            data = planned.read_identity()
            if (land_mode or data.get("land") or "auto") == "auto" and (
                    found := landing(planned, data)).relation in ("behind", "landed") and has_baseline(planned):
                if is_live(planned):  # said first: the advice (`--land manual`) would lead straight to it
                    raise RuntimeError(f"Run {short_id(planned.run_id)} is running.")
                raise RuntimeError(target_ahead_refusal(planned, data, found))
        if check_only:  # nothing written; the scope advice a run prints at its start too
            if warning := scope_warning(self.config):
                self._warn(warning)
            return
        # The first writes: the local excludes, before .autohelix/ exists.
        ensure_runtime_ignored(self.project_path)
        try:  # best effort: only runtime state must be ignored
            ensure_git_exclude_entry(self.project_path, "__pycache__/")
        except (OSError, subprocess.CalledProcessError):
            pass
        existing = {p.name for p in RunPaths(self.project_path).runs.glob("*")}
        self.paths, self.run_info, self._ownership, notices = select_run(
            self.project_path, config_file,
            new=new, run_id=run_id, name=name,
            create=create, ready=None if creating else self._new_run_ready, group=group or None,
            # A group lands manually unless --land says otherwise (auto landing in a group
            # diverges the rest); a run on its own lands automatically.
            land_mode=land_mode or ("manual" if group else "auto"),
            imported=import_run, why=why,
        )
        if self.paths.run_id != (planned and planned.run_id):
            try:  # a new run (or, raced, another than planned): from now on, its stored copy
                self.config = self._run_config(
                    self.paths, check=self.paths.run_id in existing)  # one just created matches its file
            except BaseException:
                self._ownership.close()
                raise
        # Stored on resume when the segment starts (`_run`): the mode and group asked for.
        self._identity_changes: dict = {}
        if land_mode and self.run_info.get("land") != land_mode:
            self._identity_changes["land"] = land_mode
        if group is not None and (self.run_info.get("group") or "") != group:
            self._identity_changes["group"] = group or None
            short = short_id(self.paths.run_id)
            notices.append(("info", f"{short} joins group {group} from its next iteration." if group
                            else f"{short} leaves group {self.run_info['group']}."))
        if self.paths.run_id in existing and (note := group_landing_note(self.paths, self.run_info, group, land_mode)):
            notices.append(("warn", note))
        self.land_mode = land_mode or self.run_info.get("land") or "auto"
        if self.paths.run_id not in existing and self.land_mode == "auto":
            notices.extend(("warn", text) for text in self._uncommitted_config())
        for level, text in notices:
            if level == "warn":
                self._warn(text)
            else:
                terminal.show(self.console, text, style="dim")
        try:
            self.history = History(self.project_path, self.paths.run_id)
            tip, warning = reconcile(self.paths, self.run_info, self.history, self.land_mode)
            self.run_state = RunState(self.paths, self.run_info, tip)
            self.repo = Repo(self.project_path)
            self.run_status = RunStatus(self.project_path, self.paths.run_id)
            self.segment_log = SegmentLog(self.paths.segments)
            self.log = RunLog(self.project_path, self.paths.run_id)
            self.run_state.copies_root = claim_copies(self.paths, self.log.warn)
            self._land_attempted = False
            self._segment_started = False
            self._segment: int | None = None  # this segment's number (segments.jsonl, status, stop requests)
            self._agent_logs = {}
            if warning:
                self._warn(warning)

            self._time_left_script: Path | None = None
            self._claude_settings_path: Path | None = None
            self._codex_time_left_hook_path: Path | None = None

            # Install optional helper scripts. Claude Code hooks use a shared
            # accumulator, written once to claude_settings.json.
            claude_hooks: dict[str, list] = {}
            if self.config.agent.timeout is not None:
                self._time_left_script = self._install_time_left(claude_hooks)
            self._write_claude_settings(claude_hooks)

            self.agent = create_agent(self.config.agent, heartbeat_seconds=heartbeat_seconds)
            self._check_codex_time_left()

        except BaseException:
            self.close()
            raise

    def _uncommitted_config(self) -> list[str]:
        """A new auto-landing run's warning when its config file or prompt template is
        untracked or modified (not ignored): committing it after the run lands puts the
        branch past the run's tip, and `resume` then refuses."""
        branch = self.run_info.get("target_branch")
        files = [f for f in (self.config_file, self.config.prompt_template) if f is not None]
        if not branch or not files:
            return []

        def uncommitted(path: Path) -> bool:
            def fails(*args: str) -> bool:
                return git(self.project_path, "--no-optional-locks", *args, "--", str(path), check=False).returncode != 0
            if not fails("check-ignore", "-q"):
                return False  # ignored: committing it never moves the branch
            return fails("ls-files", "--error-unmatch") or fails("diff", "--quiet", "HEAD")
        return [f"commit {config_relative(self.project_path, f)} first: a commit on {branch} after this run lands "
                "makes `resume` refuse." for f in files if uncommitted(f)]

    def _run_config(self, paths: RunPaths, check: bool = True):
        """A run's stored config (`load_run_config`). With `check`, refuses, before
        anything is spent, when the file it came from parses differently now, or its
        template's text differs (`config_changes`); a deleted file, or a deleted prompt
        template (the other settings still compared), continues from the stored copy."""
        data = paths.read_identity()
        config = load_run_config(paths, data)
        source = self.project_path / data["config_path"]
        short = short_id(paths.run_id)
        if not check:
            return config
        if not source.is_file():
            terminal.show(self.console, f"{data['config_path']} is gone; run {short} continues "
                          "with the config it stored when it started.", style="dim")
            return config
        try:
            current = load_config(self.project_path, source)
            if (current.prompt_template is not None and current.prompt_template == config.prompt_template
                    and not current.prompt_template.exists()):
                terminal.show(self.console, f"{config_relative(self.project_path, current.prompt_template)} is gone; "
                              f"run {short} continues with the prompt template it stored when it started.",
                              style="dim")
                current.prompt_template_text = config.prompt_template_text
            if current._parse_errors and current._parse_errors != config._parse_errors:
                changed = f"it no longer reads: {current._parse_errors[0]}"
            else:
                keys = config_changes(config, current)
                changed = f"changed: {', '.join(keys)}" if keys else None
        except ConfigError as e:
            changed = f"it no longer reads: {e}"
        if changed:
            said = f"{data['config_path']} changed since run {short} started ({changed}); "
            if not has_baseline(paths):  # a failed first attempt: nothing to continue or import
                raise RuntimeError(f"{said}run {short} has no accepted baseline: start a new run, "
                                   f"`{run_command(data['config_path'])}` (`autohelix delete {short}` removes "
                                   "this attempt).")
            first = land_first(paths, data, data.get("target_branch"))
            raise RuntimeError(
                f"{said}the run keeps the config it started with. To apply the new config, start a new run "
                f"that continues from this one: `{run_command(data['config_path'])} --import {short}`"
                f"{'. ' + first if first else '.'} To continue this run, restore the file.")
        return config

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
        direction = self.config.metric_directions()[gate.metric]
        value, reference = metrics.get(gate.metric), metric_reference(records, gate.metric)
        failure = metric_gate_failure(gate, direction, value, reference)
        item.update(passed=failure is None, value=value, reference=reference)
        if failure:
            item["reason"] = failure
        return failure

    def _print_header(self, budget: Budget, start_iter: int = 1) -> None:
        """The run's header (`terminal.header`); on resume, the baseline and the last
        rows so far, then a `resumed HH:MM` line."""
        width = self.console.width or 100
        name = self.run_info.get("name") or self.project_path.name
        self.console.print()
        title = terminal.title(f"run {short_id(self.paths.run_id)}", name, terminal.clock(), version=terminal.version())
        agent = terminal.agent_text(self.config.agent.cli, self.config.agent.model,
                                    getattr(self.agent, "executable", None))
        fields = terminal.run_fields(self.config, budget_text(budget, start_iter - 1), agent)
        if not self.console.is_terminal:  # no live row to show them under
            fields.append(("Steer", terminal.steer_text(short_id(self.paths.run_id), interrupt=False)))
        terminal.print_lines(self.console, terminal.header(title, fields, width, terminal.shows_mark(self.console)))
        self._table = terminal.Table.of(self.config, width)
        if start_iter > 1:  # resuming: the rows so far, for context
            rows = [Row.of(r) for r in sorted(self.history.load(), key=lambda r: r.iteration)
                    if r.iteration > 0 or r.accepted]
            self._print_lines(self._table.earlier(rows, short_id(self.paths.run_id), shown=RESUME_ROWS))

    def _rows(self) -> "terminal.Table":
        if getattr(self, "_table", None) is None:
            self._table = terminal.Table.of(self.config, self.console.width or 100)
        return self._table

    def _print_lines(self, lines) -> None:
        terminal.print_lines(self.console, lines)

    def _print_row(self, record: IterationResult) -> None:
        """A record's row and the lines under it (`terminal.Table.add`)."""
        self._print_lines(self._rows().add(Row.of(record)))

    def _live_row(self, iteration: int | None = None) -> LiveRow:
        """The live row of the iteration under way (restarted for `iteration`)."""
        if getattr(self, "_live", None) is None:
            self._live = LiveRow(self._rows(), iteration or 0)
        elif iteration is not None:
            self._live.restart(iteration)
        return self._live

    @contextmanager
    def _showing(self, renderable):
        """Show `renderable` live, transient, with a blank line under it: in the Live
        already showing (the whole iteration's, so its row stays up between phases), else
        in a new one."""
        # Not on the terminal's last line; under it, the commands that steer the run.
        steer = terminal.steer_line(short_id(self.paths.run_id), self.console.width or 100)
        renderable = terminal.with_room(renderable, steer if self.console.is_terminal else None)
        outer = getattr(self, "_live_display", None)
        if outer is not None:
            previous = outer.renderable
            outer.update(renderable)
            try:
                yield
            finally:
                outer.update(previous)
            return
        with Live(renderable, console=self.console, refresh_per_second=10, transient=True) as live:
            self._live_display = live
            try:
                yield
            finally:
                self._live_display = None

    def _warn(self, message: str) -> None:
        """A warning (`terminal.warning`: `  ! message`, wrapped under itself)."""
        terminal.warn(self.console, message)

    def _status(self, message: str) -> None:
        """Print a status message."""
        terminal.show(self.console, message, style="dim")

    def _copy_bin(self, name: str) -> Path | None:
        """Copy a helper script from the package bin/ into .autohelix/.internal/bin/.

        Returns the installed (executable) path, or None if the source is missing
        or the copy fails.
        """
        src = Path(__file__).parent / "bin" / name
        if not src.exists():
            return None
        dst_dir = self.paths.bin
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

        agent_type = self.config.agent.cli.lower()
        if agent_type == "claude-code":
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

    def _install_opencode_timer_plugin(self, copy_path: Path) -> None:
        """Drop the opencode time-left plugin into the copy as `workspace.OPENCODE_PLUGIN`.

        opencode auto-loads project-local plugins from .opencode/plugin/*.js. The
        plugin appends time_left.sh output to each tool result — the opencode
        analog of Claude's PostToolUse time hook — and reads $AUTOHELIX_TIME_LEFT_SCRIPT
        from the agent env. The snapshot leaves that one file out by pathspec, as it
        does `.autohelix-iteration/`; the project's own `.opencode/` files are untouched.
        """
        src = Path(__file__).parent / "bin" / "opencode_timer_pertool.js"
        if not src.exists():
            return
        plugin = copy_path / OPENCODE_PLUGIN
        try:
            plugin.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, plugin)
        except OSError:
            pass

    def _write_claude_settings(self, hooks: dict[str, list]) -> None:
        """Write the accumulated Claude Code hooks to claude_settings.json (if any)."""
        if not hooks:
            return
        settings_path = self.paths.claude_settings
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps({"hooks": hooks}, indent=2))
        self._claude_settings_path = settings_path

    def _check_codex_time_left(self) -> None:
        """With `agent.timeout`, Codex gets time-left hints only if its CLI supports the
        hook; otherwise warn and run without them (the deadline still holds)."""
        if self.config.agent.timeout is None or self.config.agent.cli.lower() != "codex":
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
            self._warn(text)
            self.log.warn(text)

    def build_prompt(self, iteration: int, copy_dir: Path, members=(), rejected_diff: str = "",
                     imported=None, head: str | None = None) -> str:
        """Build the prompt for the agent using the template; `members` are the other
        runs of the group, as `group_members` returns them, `imported` the run this one
        imports (`runs.imported_run`), if any; `head` the commit the copy's code is."""
        template = load_template(self.config)
        variables = build_prompt_variables(
            self.config, self.history, iteration, copy_dir,
            group=self.run_info.get("group"), members=members, rejected_diff=rejected_diff, imported=imported,
            head=head,
        )
        template_file = self.config.prompt_template
        return render_template(template, variables, *([template_file.name] if template_file else []))

    def run_agent(self, copy_path: Path, prompt: str, iteration: int) -> AgentRunResult:
        """Run the configured agent in its copy."""
        iteration_folder(self.paths, iteration)
        prompt_path = self.paths.prompt(iteration)
        log_path = self.paths.agent_log(iteration)
        for path in (prompt_path, log_path):  # planted: never written through
            if path.is_symlink() or path.exists() and not path.is_file():
                path.unlink()
        self._agent_logs[iteration] = log_path
        self._live_call("agent", log_path)
        self.run_status.note(log=str(log_path))
        prompt_path.write_text(prompt)

        # opencode time-budget injection: drop the per-tool timer plugin into the
        # copy so opencode auto-loads it (the analog of Claude's PostToolUse
        # time hook). Only when an iteration deadline is set.
        if (
            self.config.agent.timeout is not None
            and self.config.agent.cli.lower() == "opencode"
        ):
            self._install_opencode_timer_plugin(copy_path)

        if self.verbose:
            print(f"  Prompt: {prompt_path}")
            print(f"  Log: {log_path}")
            print(f"  Heartbeat: {self.heartbeat_seconds}s")

        # The agent is told its run and iteration, not where the project or the run's
        # state is (hygiene, not a boundary: gate, observe and stop commands get those).
        deadline_env = {
            "AUTOHELIX_PROJECT": None,
            "AUTOHELIX_RUN_DIR": None,
            "AUTOHELIX_RUN_ID": self.paths.run_id,
            "AUTOHELIX_ITERATION": str(iteration),
            "AUTOHELIX_ITERATION_START": str(int(time.time())),
            "AUTOHELIX_ITERATION_BUDGET": (
                str(self.config.agent.timeout)
                if self.config.agent.timeout is not None
                else None
            ),
            # The budget as the time-left hint says it (1h30m, as the config spells it).
            "AUTOHELIX_ITERATION_BUDGET_TEXT": (
                format_limit(self.config.agent.timeout) if self.config.agent.timeout is not None else None
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

        previous_env: dict[str, str | None] = {}

        display = LiveDisplay(
            console=self.console,
            title=self.agent.display_name,
            border_style="blue",
            verbose=self.verbose,
        )
        row = self._live_row()
        row.set("agent")

        def on_event(event: AgentEvent) -> None:
            display.handle_event(event)

        try:
            # The backends read their hook settings from the environment (None: unset).
            for key, value in deadline_env.items():
                previous_env[key] = os.environ.get(key)
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

            shown = (Live(display, console=self.console, refresh_per_second=10) if self.verbose
                     else self._showing(row))
            with shown:
                result = self.agent.run(
                    copy_path=copy_path,
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

            if result.error and self.verbose:  # otherwise the run's error says it, once
                terminal.show(self.console, result.error, Text("Agent error: ", style="red"), style="red")
            if self.verbose:
                self.console.print()
            return result
        except KeyboardInterrupt:
            self._status("Interrupted - stopping agent...")
            raise
        finally:
            for key, value in previous_env.items():  # always restored
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
        self._live_row(iteration)
        self.log.info(f"iter {iteration} started")
        self.run_status.phase("agent", iteration)
        self.run_status.note(calls=[])
        # An earlier attempt with this number (killed before it was recorded) is kept
        # aside, so nothing of it is taken for this attempt's.
        self._start_command_usage(iteration, self.history.supersede(iteration))
        workspace = IterationWorkspace(iteration, self.run_state, self.history, self.repo, warn=self.log.warn)
        workspace.drop_rejected()  # from an earlier attempt with this number that was never recorded
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
                with critical_section():  # Ctrl-C waits until the attempt is recorded
                    self._describe_attempt(result, workspace)
                    if not result.accepted and attempt.agent_attempted:
                        try:
                            result.rejected_ref = workspace.keep_rejected()
                        except Exception as e:  # noqa: BLE001 - never mask the iteration's outcome
                            self.log.warn(f"Could not keep iteration {iteration}'s rejected tree: {e}")
                        if result.rejected_ref:
                            try:
                                workspace.store_rejected_patch()
                            except Exception as e:  # noqa: BLE001 - evidence, not the outcome
                                self.log.warn(f"Could not write iteration {iteration}'s rejected.patch: {e}")
                    self._complete_record(result, attempt.started_at, attempt.monotonic, attempt.evaluation)
                    # Failures before the agent call don't consume the iteration.
                    if attempt.agent_attempted:
                        if workspace.record(result):
                            self.segment_log.iteration(ended_at=result.ended_at, **iteration_fields(result))
            except BaseException as e:
                if failure is None:
                    failure = e
                else:
                    self.log.warn(f"Could not record failed iteration: {e}")
            finally:
                try:
                    workspace.close()
                finally:
                    self.run_status.note(copy=None)

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
        agent_copy = workspace.agent_copy(hidden=self.config.protected)
        if self.verbose:
            terminal.show(self.console, str(agent_copy.working_dir), "  Working dir: ")
        self.run_status.note(copy=str(agent_copy.working_dir))
        prompt = self.build_prompt(iteration, agent_copy.working_dir, workspace.members,
                                   rejected_diff=workspace.rejected_diff, imported=workspace.imported,
                                   head=workspace.base)
        workspace.write_prompt(prompt)
        if self.verbose:
            self.console.print(Panel(Text(clean_block(prompt.strip())), title="[bold]Agent Prompt[/bold]", border_style="dim"))
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
            # Kept for the record and the next agent (rejected.patch), never checked or
            # accepted; the loop goes on (`_run` stops after consecutive failures).
            try:
                self._freeze(workspace, checking=False)
            except Exception as e:  # noqa: BLE001 - the agent's failure is the outcome
                self.log.warn(f"Could not keep iteration {iteration}'s tree: {e}")
            self.log.info(f"iter {iteration} agent failed — {detail}")
            return self._rejected(iteration, attempt, f"Iteration {iteration} agent failed: {detail}",
                                  agent_error=detail)

        self._freeze(workspace)

        evaluation = attempt.evaluation = self._new_evaluation()
        self._evaluate(workspace, iteration, evaluation)
        metrics = evaluation.metrics
        if self.verbose:
            for name, value in metrics.items():
                self.console.print(Text(f"  {clean(self._metric_label(name))} ").append(clean(value), style="cyan"))
        if evaluation.rejection:
            return self._rejected(iteration, attempt, evaluation.rejection, metrics=metrics,
                                  failure_output=evaluation.failure_output)

        # Publish the validated tree to this run, independently of landing.
        self.run_status.phase("accept")
        if self.verbose:
            self._status("Accepting changes...")
        result = IterationResult(iteration=iteration, accepted=True, metrics=metrics,
                                 calls=[{"role": "agent", "usage": attempt.agent_usage}])
        self._describe_attempt(result, workspace)
        self._complete_record(result, attempt.started_at, attempt.monotonic, evaluation)
        workspace.accept(self._build_commit_message(iteration, metrics, summary=workspace.summary), result)

        # Stop commands see accepted content in a fresh copy. Errors (and interrupts,
        # handled by the caller) keep the recorded acceptance.
        if self.config.stop:
            try:
                result.stop, _ = self._check_stop(workspace, iteration, metrics, result)
            except Exception as e:  # noqa: BLE001 - acceptance already happened
                self._warn(f"Stop check failed: {e}")
                self.log.info(f"iter {iteration} stop check failed — {e}")
        return result

    def _freeze(self, workspace: IterationWorkspace, checking: bool = True) -> None:
        """Freeze the candidate: out-of-scope changes never reach the tree. The viewer
        shows the pin from "preparing checks" on (viewer.FROZEN_PHASES); a failed agent's
        attempt is frozen only to be kept (`checking=False`), never checked."""
        workspace.freeze(Scope(self.repo.prefix, tuple(self.config.editable), tuple(self.config.frozen_patterns())),
                         pinned=(lambda: self.run_status.phase("preparing checks")) if checking else None)
        self.run_status.note(copy=None)
        reverted = self._discarded(workspace)
        if reverted and self.verbose:
            self._warn(f"Discarded {len(reverted)} change(s) outside editable scope:")
            for f in reverted[:5]:
                terminal.show(self.console, f, "    ", style="dim")
            if len(reverted) > 5:
                terminal.show(self.console, f"... and {len(reverted) - 5} more", "    ", style="dim")

    def _discarded(self, workspace: IterationWorkspace) -> list[str]:
        """The out-of-scope paths dropped at freeze, relative to the project."""
        return [name.removeprefix(self.repo.prefix) for name in workspace.discarded]

    def _describe_attempt(self, result: IterationResult, workspace: IterationWorkspace) -> None:
        """The attempt's own fields on its record: summary, A, T, discarded paths."""
        result.summary = workspace.summary
        result.base = workspace.base
        result.tree = workspace.tree if workspace.iteration else None
        result.discarded = self._discarded(workspace)
        result.discarded_frozen = [path for path in result.discarded if path_in_scope(path, self.config.frozen_patterns())]

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
           `iterations/NNN/observe/<name>/`; a failed entry contributes no metrics and the
           next one runs. After each entry, the metric gates on its metrics (gates list
           order); a failure rejects and skips the remaining entries.

        At the baseline (iteration 0) nothing rejects: every gate command and entry runs
        and metric gates aren't checked. Raises CandidateRejected if a command or judge
        changed the candidate (the gate or entry that did is recorded as failed)."""
        baseline = iteration == 0
        checks = workspace.eval_copy("checks")
        command_env = {**self._command_env(iteration), **self._candidate_env(workspace),
                       "AUTOHELIX_OBSERVATIONS": None, "AUTOHELIX_METRICS": None}
        records = self.history.load()  # the reference for metric gates: the last accepted iteration
        where = "baseline" if baseline else f"iter {iteration}"
        notes: list[Text] = []  # printed once the live display is gone, whatever happens

        def verify(command: str) -> None:
            # Checks are read-only: one command must not alter what a later one validates.
            try:
                changed = checks.changed()
            except RuntimeError:
                changed = ["its copy was removed or replaced"]
            if changed:
                raise CandidateRejected(f"checks modified candidate files: {command} ({', '.join(changed[:5])})")

        positions = {id(gate): index for index, gate in enumerate(self.config.gates)}
        current: dict = {}  # the running command's call folders; the gate item of the one that just ran

        @contextmanager
        def gate_call(spec):
            with workspace.call_folder(f"{RunPaths.GATES}/{positions[id(spec)] + 1}") as folders:
                current["folders"] = folders
                yield {**_call_env(folders), "AUTOHELIX_PREVIOUS": None}

        def gate_result(spec, result) -> None:
            index = positions[id(spec)]
            evaluation.constraints.append(result)
            item = current["item"] = evaluation.gates[index]
            item.update(passed=result.passed, return_code=result.return_code,
                        duration_s=None if result.duration_s is None else round(result.duration_s, 3))
            if result.timed_out:
                item["timed_out"] = True
            if not result.passed:
                item["output"] = _tail(result.output)
            self.log.info(f"{where} gate: {spec.text} — {'passed' if result.passed else 'FAILED'}")
            # Its output is stored (passing or not, also if it changed the candidate) while
            # its call folder still exists, after its outcome is recorded.
            self._store_command_output(iteration, current["folders"], spec.command, result,
                                       lambda: self.history.gate_dir(iteration, index + 1))

        def verify_gate(command: str) -> None:
            try:
                verify(command)
            except CandidateRejected as e:  # it passed, but by changing what it checks: not a pass
                if item := current.get("item"):
                    item.update(passed=False, reason=str(e))
                raise

        row = self._live_row()
        row.set("gates")
        if not baseline:
            self.log.info(f"iter {iteration} gates started")
            self.run_status.phase("checks")
            if self.verbose:
                self._status("Running gates...")
        try:
            with self._showing(row):
                run_gate_commands(self.config.gate_commands(), checks.working_dir, project_path=self.project_path,
                                  extra_env=command_env, after_command=verify_gate,
                                  stop_on_failure=not baseline, per_command=gate_call, on_result=gate_result)
                failed = next((r for r in evaluation.constraints if not r.passed), None)
                # At the baseline, gate commands only guard against mutation; failing is fine.
                if failed is not None and not baseline:
                    evaluation.rejection = f"gate failed: {self._gate_text(failed.command)}"
                    evaluation.failure_output = _tail(failed.output)
                elif self.config.observe:
                    row.set("observe")
                    if not baseline:
                        self.run_status.phase("observe")
                        self.log.info(f"iter {iteration} observe started")
                    try:
                        self._observe(workspace, iteration, checks, evaluation, command_env, records,
                                      verify=verify, notes=notes)
                    finally:
                        self.run_status.note(entry=None)
        finally:
            # The row shows the outcome (`_print_row`); details with --verbose.
            if self.verbose and not baseline:
                for r in evaluation.constraints:
                    mark = Text("  ").append("✓ " if r.passed else "✗ ", style="green" if r.passed else "red")
                    terminal.show(self.console, self._gate_text(r.command), mark)
                    if not r.passed:
                        self.console.print(Panel(Text(clean_block(r.output[:2000])), title="Gate output", border_style="red"))
            if self.verbose:
                terminal.print_lines(self.console, notes)
        workspace.remove(checks)
        if evaluation.metrics and not baseline:
            self.log.info(f"iter {iteration} observe: "
                          + ", ".join(f"{k}={v:g}" for k, v in evaluation.metrics.items()))

    def _observe(self, workspace: IterationWorkspace, iteration: int, checks, evaluation: _Evaluation,
                 command_env: dict, records, *, verify, notes: list[Text]) -> None:
        """Observe entries in order (see `_evaluate`), each with its own call folder and,
        as `$AUTOHELIX_PREVIOUS`, a copy of its output from the last accepted iteration
        in which it succeeded; in a run that imports another (`run --import`), until the
        entry has succeeded in this run, the imported run's last successful output of the
        entry with the same name.

        An entry's output (stdout.txt, stderr.txt, captures) is staged in its call
        folder after the command (and its process group) ended, then replaces
        `iterations/NNN/observe/<name>/`, so nothing an earlier command wrote there
        survives. This guards what the harness stores, not run state as such: candidate
        code and the agent can still write run state by absolute path (until commands
        run in a sandbox)."""
        baseline = iteration == 0
        where = "baseline" if baseline else f"iter {iteration}"
        gates = [(index, gate) for index, gate in enumerate(self.config.gates) if isinstance(gate, MetricGate)]
        imported = imported_run(self.paths, self.run_info)
        imported_history = History(self.project_path, imported[0].run_id) if imported else None
        for entry in self.config.observe:
            self.run_status.note(entry=entry.name)  # the viewer marks the running entry
            self._live_row().set("observe", entry.name)
            previous = self.history.previous_observations(entry.name, iteration)
            # Until the entry has succeeded in this run (a failed baseline entry included),
            # it continues from the imported run's last successful output.
            if imported_history and not any(
                    r.accepted and r.iteration < iteration and any(
                        isinstance(item, dict) and item.get("name") == entry.name and item.get("ok")
                        for item in r.observe)
                    for r in records):
                try:
                    previous = imported_history.previous_observations(entry.name, sys.maxsize)
                except Exception as e:  # noqa: BLE001 - another run's history never stops this one
                    self.log.warn(f"No $AUTOHELIX_PREVIOUS for {entry.name} from the imported run: {e}")
                    previous = None
            with workspace.call_folder(f"{RunPaths.OBSERVE}/{entry.name}", previous=previous) as folders:
                env = {**command_env, **_call_env(folders),
                       "AUTOHELIX_PREVIOUS": str(folders["previous"]) if "previous" in folders else None}
                result = run_observe_entry(entry, checks.working_dir, project_path=self.project_path,
                                           extra_env=env)
                record = result.record()
                evaluation.observe.append(record)  # before captures: a capture problem can't lose it
                # Stored before anything can reject: a rejected iteration keeps its output.
                problems = self._store(
                    folders,
                    lambda stage: self.history.stage_output(stage, result.stdout, result.stderr,
                                                            checks.working_dir, entry.capture),
                    lambda stage: self.history.install_observation(iteration, entry.name, stage))
            if problems:
                record["capture_errors"] = problems
                for problem in problems:
                    self.log.warn(f"{where} observe {entry.name}: {problem}")
            if result.ok:
                evaluation.metrics.update(result.values)
                for name, error in result.errors.items():
                    self.log.info(f"{where} observe {entry.name}: metric '{name}' missing — {error}")
            else:
                notes += terminal.warning(f"observe {entry.name} failed ({result.error}); its metrics are missing",
                                          self.console.width)
                self.log.info(f"{where} observe {entry.name} failed — {result.error}")
            try:
                verify(entry.command)
            except CandidateRejected as e:  # it ran, but by changing what it observes: a failure
                record.update(ok=False, error=str(e))
                raise
            if baseline:
                continue
            # Metric gates waiting for this entry, in gates list order (a gate's metric is
            # always declared: `config._parse_gates`).
            for index, gate in gates:
                if gate.metric not in entry.metrics:
                    continue
                if failure := self._check_metric_gate(gate, evaluation.gates[index], evaluation.metrics, records):
                    notes += terminal.hanging(Text("  ").append("✗ ", style="red"), failure, self.console.width)
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

    @staticmethod
    def _store(folders: dict, fill, install):
        """Store one command's output: `fill(stage)` writes it into a fresh `output/` in
        the command's call folder (made after the command and its process group ended),
        the transcripts its `ask`/`review` calls left in its `logs/` join it
        (`IterationWorkspace.move_transcripts`), then `install(stage)` puts it in place in
        run state (the call's folder, `iterations/NNN/<call>/`, where the calls' usage
        records name them), replacing whatever is there. Returns what `fill` returned."""
        stage = IterationWorkspace.stage_folder(folders)
        filled = fill(stage)
        IterationWorkspace.move_transcripts(folders, stage)
        install(stage)
        return filled

    def _store_command_output(self, iteration: int, folders: dict, command: str, result, target) -> None:
        """A gate's or stop command's output (`History.stage_command_output`), installed
        at `target()` (`History.gate_dir` / `stop_dir`). A failure to store it is logged,
        never fatal: the command's outcome counts either way."""
        try:
            self._store(folders,
                        lambda stage: self.history.stage_command_output(stage, command, result.stdout,
                                                                        result.stderr),
                        lambda stage: self.history.install(target(), stage))
        except Exception as e:  # noqa: BLE001 - evidence, not the outcome
            self.log.warn(f"Could not store the output of {command!r} (iteration {iteration}): {e}")

    def _complete_record(self, result, started_at, iter_start, evaluation: _Evaluation | None = None):
        result.started_at = started_at
        result.ended_at = time.time()
        if evaluation is not None:
            result.gates = [dict(item) for item in evaluation.gates]
            result.observe = [dict(item) for item in evaluation.observe]
        else:
            result.gates, result.observe = [], []
        agents = [call for call in result.calls if call["role"] == "agent"]
        for call in agents:
            call["usage"]["wall_clock_ms"] = int((time.monotonic() - iter_start) * 1000)
            call["log"] = self._relative_log(self._agent_logs.get(result.iteration))
        commands = []
        for usage in read_usage(self.paths.usage(result.iteration)):
            log = self._relative_log(self.paths.usage_log(result.iteration, usage))  # stored, else None
            usage.pop("log", None)
            usage.pop("attempt", None)
            commands.append({"role": "command", "usage": usage, "log": log})
        result.calls = agents + commands

    def _start_command_usage(self, iteration: int, moved: Path | None = None) -> None:
        """Start an empty usage file for the commands this iteration runs. A baseline
        retry keeps the earlier attempts' calls, which still count toward its cost: a
        `superseded` marker says their transcripts moved with the earlier attempt's
        folder to `moved` (`History.supersede`; none if it wasn't kept)."""
        path = self.paths.usage(iteration)
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = next((r for r in self.history.load() if r.iteration == iteration), None)
        if previous is None or previous.accepted:
            path.write_text("")
        elif read_usage(path):
            append_usage(superseded_marker(moved.name if moved is not None else ""), path)

    def _command_env(self, iteration: int) -> dict[str, str]:
        """Variables for every command the harness runs (checks, metrics, stop):
        where to record model usage, and which config to read. `$AUTOHELIX_LOG_DIR` and
        `$AUTOHELIX_CALL` are set per call (`_call_env`)."""
        return {
            USAGE_ENV: str(self.paths.usage(iteration)),
            "AUTOHELIX_CONFIG": str(self.paths.config),  # the run's stored copy
            "AUTOHELIX_LOG_DIR": None,  # per call (`_call_env`)
            "AUTOHELIX_CALL": None,
            "AUTOHELIX_RUN_ID": self.paths.run_id,
            "AUTOHELIX_RUN_DIR": str(self.paths.run_dir),
            "AUTOHELIX_ITERATION": str(iteration),
        }

    def _check_stop(self, workspace: IterationWorkspace, iteration: int,
                    metrics: dict[str, float], record: IterationResult) -> tuple[dict[str, str] | None, list]:
        """Run the stop commands in a fresh copy of the accepted tip, each recorded in
        `record.stops` as it ends (like a gate: outcome, return code, timed out). Returns
        (stop, errors): stop is {"command", "reason"} if one exited 0; errors are the
        results that exited with anything other than 0 or 1 (or timed out). Changing
        the files or a judge violation is an error.

        Each command also gets `$AUTOHELIX_OBSERVATIONS`, a copy (made for that call)
        of the accepted iteration's `iterations/NNN/observe/` (empty if it observed
        nothing), and its own `$AUTOHELIX_SCRATCH`."""
        self.run_status.phase("stop check", iteration)
        metrics_path = self.paths.metrics
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(json.dumps(metrics))
        env = {**self._command_env(iteration), **self._candidate_env(workspace),
               "AUTOHELIX_METRICS": str(metrics_path), "AUTOHELIX_PREVIOUS": None}
        observations = self.paths.observe_dir(iteration)

        positions = {id(spec): index for index, spec in enumerate(self.config.stop, start=1)}
        current: dict = {}  # the running command's call folders

        @contextmanager
        def stop_call(spec):
            with workspace.call_folder(f"{RunPaths.STOP}/{positions[id(spec)]}", observations=observations) as folders:
                current["folders"] = folders
                yield {**_call_env(folders), "AUTOHELIX_OBSERVATIONS": str(folders["observations"])}

        record.stops = []

        def stop_result(spec, result) -> None:  # stored while its call folder still exists
            position = positions[id(spec)]
            record.stops.append(StopCheckResult.of(spec, result).record(spec.text))
            self._store_command_output(iteration, current["folders"], spec.command, result,
                                       lambda: self.history.stop_dir(iteration, position))

        stop = workspace.stop_copy()
        try:
            row = self._live_row()
            row.set("stop")
            with self._showing(row):
                results = run_stop_checks(self.config, stop.working_dir, project_path=self.project_path, extra_env=env,
                                          per_command=stop_call, on_result=stop_result)
            if changed := stop.changed():
                raise AutoHelixRunError("stop commands modified accepted files: " + ", ".join(changed[:5]))
        finally:
            workspace.remove(stop)
        errors = [r for r in results if r.error]
        for r in errors:  # shown under the iteration's row (`terminal.Table._notes`, from `record.stops`)
            self.log.info(f"iter {iteration} stop command error ({r.problem()}): {r.command}")
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
        self.segment_log.iteration(ended_at=baseline.ended_at, **iteration_fields(baseline))

    def _capture_baseline(self) -> dict[str, str] | None:
        """Evaluate A in a checks copy and check stop commands.

        Returns the stop condition the baseline already meets, if any."""
        started_at = time.time()  # the record's span: `history.record_seconds`
        workspace = self._baseline_workspace()
        evaluation = self._new_evaluation()
        self._live_row(0)
        try:
            self.log.info("baseline capture started")
            self.run_status.phase("baseline", 0)
            self._start_command_usage(0, self.history.supersede(0))
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
            outcomes = {item["name"]: item for item in evaluation.observe}

            def why(names) -> str:
                """`observe entry `bench` exited 0 but reported no `speed``, per entry."""
                by_entry: dict[str, list[str]] = {}
                for name in sorted(names):
                    by_entry.setdefault(self.config.metric_source(name).name, []).append(f"`{name}`")
                parts = []
                for entry, metrics in by_entry.items():
                    item = outcomes.get(entry) or {}
                    how = "exited 0 but" if item.get("ok") else f"failed ({item.get('error') or 'did not run'}) and"
                    parts.append(f"observe entry `{entry}` {how} reported no {', '.join(metrics)}")
                return "; ".join(parts)
            gated = {gate.metric for gate in self.config.metric_gates()}
            advisory = {name for name in missing
                        if self.config.metric_source(name).review is not None and name not in gated}
            if advisory:
                text = f"Baseline: {why(advisory)} (a review's number; no gate uses it)"
                self._warn(text)
                self.log.warn(text)
            if missing - advisory:
                names = ", ".join(sorted(missing - advisory))
                self.log.info(f"baseline FAILED — missing metrics: {names}")
                raise RuntimeError(f"Baseline setup error: {why(missing - advisory)}: every declared metric "
                                   "needs a value at the baseline")
            self._print_row(IterationResult(iteration=0, accepted=True, metrics=metrics,
                                            gates=evaluation.gates, observe=evaluation.observe))
            if metrics:
                self.log.info("baseline: " + ", ".join(f"{name}: {value:g}" for name, value in metrics.items()))

            baseline = IterationResult(iteration=0, accepted=True, metrics=metrics, commit=None, reason="baseline",
                                       base=workspace.base)
            if self.config.stop:
                # A broken stop command fails here, before anything is recorded, so a
                # typo surfaces before the first iteration is paid for.
                baseline.stop, errors = self._check_stop(workspace, 0, metrics, baseline)
                if errors:
                    self.log.info("baseline FAILED — stop command error")
                    tail = "\n".join(errors[0].output.strip().splitlines()[-5:])
                    raise AutoHelixRunError(
                        f"Stop command {errors[0].problem()} on the baseline: {errors[0].command} "
                        "(expected exit 0 to stop or 1 to continue)" + (f"\n{tail}" if tail else "")
                    )
            self._complete_record(baseline, started_at, time.monotonic(), evaluation)
            self._write_baseline(baseline)
            return baseline.stop
        except BaseException as error:
            baseline = IterationResult(iteration=0, accepted=False, reason=f"baseline failed: {error}",
                                       base=workspace.base)
            self._complete_record(baseline, started_at, time.monotonic(), evaluation)
            if any(r.iteration == 0 and r.accepted for r in self.history.load()):
                raise
            self._write_baseline(baseline)
            if isinstance(error, RuntimeError) and len(error.args) == 1 and isinstance(error.args[0], str):
                # A run without an accepted baseline is a disposable attempt: fixing the
                # config means a new run (a resume refuses a changed config).
                short = short_id(self.paths.run_id)
                said = error.args[0]  # after a command's output lines, on a line of its own
                said = f"{said}\n" if "\n" in said else f"{said.rstrip('.')}. "
                error.args = (f"{said}Fix it and run "
                              f"`{run_command(config_relative(self.project_path, self.config_file))}` again "
                              f"(`autohelix delete {short}` removes this attempt).",)
            raise
        finally:
            workspace.close()

    def run(self, budget: Budget | None = None, report: bool = True) -> str:
        """Run the optimization loop for `budget` (default: `DEFAULT_ITERATIONS` more),
        recording live status in .internal/status.json.

        When the loop finishes normally (not interrupted) after running at least one
        iteration, writes the run summary unless `report=False` (`--no-report`).
        However a started run stops, it then saves report.html. SIGTERM ends it as
        Ctrl-C does (`_terminate_interrupts`).

        A Harness runs one segment: a second call raises (a new Harness redoes the
        start's checks, as each `run`/`resume` command does).

        Returns "finished" when the segment ended politely (a budget, a stop
        condition, `autohelix stop`), "interrupted" when an interrupt in the loop
        ended it (recorded and landed; one in the baseline or the summary
        propagates). Raises `AutoHelixRunError`, already shown in the ending
        (`autohelix_reported`), when the agent kept failing or auto landing failed.
        """
        if self._ran:
            raise RuntimeError("A Harness runs one segment; make a new one for the next (as `resume` does).")
        self._ran = True
        with _terminate_interrupts():
            self._run_segment(budget, report)
        if self._interrupted_at is not None:
            return "interrupted"
        stop = self.run_status.data.get("stop_reason") or {}
        failure = self._land_error or (stop.get("kind") == "agent" and stop.get("detail"))
        if failure:
            error = AutoHelixRunError(f"auto landing failed: {failure}" if failure is self._land_error else failure)
            error.autohelix_reported = True
            raise error
        return "finished"

    def _run_segment(self, budget: Budget | None, report: bool) -> None:
        outcome = "finished"
        self._segment_started = False
        self._segment = None
        self.run_status.data = {}
        self._landing = None  # the ending's landing line (`_land`), the files written: for the ending
        self._landed_count: int | None = None  # commits auto landing put on the target (None: none landed)
        self._files: list[tuple[str, str]] = []
        self._interrupted_at: int | None = None
        self._land_error: Exception | None = None  # why auto landing failed (`run` then raises)
        self._segment_checked = False  # past the start's checks: from there the segment lands
        try:
            self._land_attempted = False
            if self._run(budget or Budget()) and report:
                self._summary()
        except KeyboardInterrupt:
            outcome = "interrupted"
            self._note_interrupt_request()  # in the baseline (the loop notes its own)
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
                        self._end_segment_log(sys.exc_info()[1])
                        clear_stop_request(self.paths)  # answered (or too late for this segment)
                        self._write_static()
                        self._print_ending(original or sys.exc_info()[1])
            finally:
                try:
                    # Only the live view reads the pin, while an iteration is judged.
                    git(self.project_path, "update-ref", "-d", self.paths.pin, check=False)
                    remove_copies(self.run_state.copies_root, self.log.warn)
                finally:
                    self.close()

    def _summary(self) -> None:
        """The run summary, after the loop and its landing: how the segment ended stands
        whatever happens here (a failure or Ctrl-C ends only the summary, said in a line).
        None after the agent failed 3 times in a row: a fourth call would most likely fail
        too, and there is nothing new to summarise."""
        if (self.run_status.data.get("stop_reason") or {}).get("kind") == "agent":
            return
        from autohelix.report import write_report

        self.run_status.phase("report")
        later = "`autohelix report` writes it"
        try:
            if write_report(self.config, self.project_path, self.console, run_id=self.paths.run_id, _owned=True):
                self._files.append(("summary", terminal.shown_path(self.paths.report)))
        except KeyboardInterrupt:
            self._warn(f"Summary interrupted; {later}.")
        except Exception as e:  # noqa: BLE001 - the segment's outcome stands
            self.log.warn(f"Summary failed: {e}")
            self._warn(f"Summary failed: {clean(str(e), 300)}; {later}.")

    def _end_segment_log(self, error: BaseException | None) -> None:
        """This segment's end record in segments.jsonl: how it ended (the status's state
        and stop reason, the error it failed with), the run's last iteration and how
        many records the segment added to the history."""
        try:
            records = self.history.load()
            last = max((r.iteration for r in records), default=0)
            added = len(records) - self._segment_records
        except Exception:  # noqa: BLE001 - the log is best-effort; the outcome stands
            last = added = None
        failure = None
        if error is not None and not isinstance(error, KeyboardInterrupt):
            failure = clean(f"{type(error).__name__}: {error}", 500)
        self.segment_log.end(state=self.run_status.data.get("state"),
                             stop_reason=self.run_status.data.get("stop_reason"), last_iteration=last,
                             iterations=added, error=failure)

    def close(self) -> None:
        if hasattr(self, "_ownership"):
            self._ownership.close()

    def __del__(self):
        self.close()

    def _new_run_ready(self) -> None:
        """Readiness checks for a first start, run before any run state exists: a run
        starts from a commit, so tracked files must be clean."""
        require_clean(self.project_path)
        if not self._preflight():
            raise RuntimeError("Preflight checks failed. Fix the issues above before running.")
        self._preflight_done = True

    def _preflight(self) -> bool:
        """Print preflight warnings and errors; False if there are errors (the caller
        then raises, saying the checks failed)."""
        issues = preflight_check(self.config, self.project_path)
        for w in (i for i in issues if i.level == "warning"):
            self._warn(w.message)
        errors = [i for i in issues if i.level == "error"]
        for e in errors:
            terminal.show(self.console, e.message, Text("  ").append("FAIL ", style="red"))
        return not errors

    def _target_tip(self) -> str | None:
        target = self.run_info.get("target_branch")
        return git(self.project_path, "rev-parse", "--verify", f"refs/heads/{target}",
                   check=False).stdout.strip() if target else None

    def _land(self) -> None:
        """Auto-land at segment end (`runs.land` refuses a target that can't fast-forward).
        Keeps what landed for the ending (`_landing`: `N commits on <branch>`, `N
        commits on autohelix/<id> (manual landing)`, `failed: …`; `_landed_count`)."""
        if not self._segment_checked or self._land_attempted:
            return  # a start that was refused (its checks failed) lands nothing
        self._land_attempted = True
        target = self.run_info.get("target_branch")
        # An interrupt (Ctrl-C, `stop --now`, a second one) waits for the landing: once
        # attempted, the finally does not retry it.
        with critical_section():
            try:
                if self.land_mode == "auto":
                    before = self._target_tip()
                    land(self.paths, self.run_info, self.run_state.expected_tip)
                    self._landed_count = self._commits_between(before, self.run_state.expected_tip)
                    self._landing = terminal.landed(self._landed_count, target)
                else:
                    found, words = self._relation()
                    self._landing = terminal.unlanded(found.ahead, f"autohelix/{self.paths.run_id}", words.note,
                                                      "yellow" if found.relation == "diverged" else None)
            except Exception as e:
                self._landing = terminal.not_landed(e)
                self._land_error = e
                self.log.warn(f"auto-land: {e}")

    def _commits_between(self, old: str | None, new: str) -> int:
        """Commits `new` has that `old` lacks (all of them without `old`)."""
        span = f"{old}..{new}" if old else new
        result = git(self.project_path, "rev-list", "--count", span, check=False)
        return int(result.stdout.strip() or 0) if result.returncode == 0 else 0

    def _write_static(self) -> None:
        from autohelix.viewer import write_static

        try:
            path = write_static(self.project_path, run_id=self.paths.run_id)
            self._files.insert(0, ("page", terminal.shown_path(path)))
        except Exception as e:  # noqa: BLE001 - never hide the run's own outcome
            self._warn(f"Could not save report.html: {e}")

    def _run(self, budget: Budget) -> bool:
        """Returns True if the loop ran at least one iteration and was not interrupted."""
        segment_start = time.monotonic()  # --time counts from here, baseline included
        segment_started_at = time.time()
        start_iter = self.history.get_last_iteration() + 1
        max_iter = budget.last_iteration(start_iter - 1)
        needs_baseline = not any(r.iteration == 0 and r.accepted for r in self.history.load())

        # The segment starts as soon as the run lock is held (before anything can refuse):
        # `autohelix stop` and `wait` then see it, and a start that fails is a recorded
        # segment that failed. Status records the iteration and cost caps as run totals
        # (spent before this segment plus its own), the time cap as this segment's.
        cost_before = summarize_costs(self.history.cost_usages).cost_usd or 0
        self._segment_records = len(self.history.load())  # the end record counts what it adds
        # A stop request no segment answered (its own ended first): gone before the status
        # says running, the only time `autohelix stop` writes one.
        clear_stop_request(self.paths)
        # The number its start record gets: in the status, so a stop request names this segment.
        self._segment = self.segment_log.next_number()
        # Set before the first write: from here the segment's ending writes its end record.
        self._segment_started = True
        self.run_status.start(
            max_iterations=max_iter, run_id=self.paths.run_id, segment=self._segment,
            max_cost_usd=None if budget.cost_usd is None else round(cost_before + budget.cost_usd, 6),
            max_time_seconds=budget.time_seconds, segment_started_at=segment_started_at)
        # Records an earlier segment wrote but never logged (it died in between): theirs,
        # before this start.
        self.segment_log.catch_up(self.history.load())
        executable = getattr(self.agent, "executable", None)
        if previous := read_segments(self.paths.segments):
            if (before := previous[-1].get("executable")) and executable and before != executable:
                self._warn(f"The agent now starts {terminal.home_path(executable)} "
                           f"(the run's last segment started {terminal.home_path(before)}).")
        self.segment_log.start(segment=self._segment, started_at=segment_started_at,
                               start_iteration=0 if start_iter == 1 and needs_baseline else start_iter,
                               budget={**asdict(budget), "default": budget.default}, executable=executable)

        # Again at each segment: the ignore rules may have changed since this Harness was
        # created (a long-lived programmatic one).
        try:
            ensure_runtime_ignored(self.project_path)
        except GitExcludeError as e:
            raise AutoHelixRunError(str(e)) from e

        if needs_baseline:
            require_clean(self.project_path)  # also a resumed run whose baseline was never accepted
            if git(self.project_path, "rev-parse", "HEAD").stdout.strip() != self.run_state.expected_tip:
                raise AutoHelixRunError("Baseline checkout moved since this run started. Restore its original commit, "
                                        f"or start a new run with `{run_command(self.run_info['config_path'])}` "
                                        f"(and `autohelix delete {short_id(self.paths.run_id)}` this one).")

        # Show header
        self._print_header(budget, start_iter)
        remove_copies(self.run_state.copies_root, self.log.warn)  # left by a segment that crashed
        if not self.repo.attr_source:
            self._warn(ATTR_SOURCE_LIMITATION)
        if warning := scope_warning(self.config):
            self._warn(warning)
            self.log.info(warning)
        if (self.project_path / ".autohelix" / "prompt.md").exists():
            self._warn(".autohelix/prompt.md is no longer read: the built-in "
                               "prompt is used. To keep a custom prompt, move it next to your config, set "
                               "`prompt_template: <file>` there, and update its .autohelix/ paths to "
                               f"{ITERATION_DIR}/. Otherwise delete it.")
        if self.config.prompt_template and (unknown := unknown_variables(load_template(self.config))):
            self._warn(f"{self.config.prompt_template.name} uses variables the prompt doesn't have "
                       f"(they render empty): {', '.join(unknown)}. See docs/concepts.md#prompt for the names.")
        if self.config.prompt_template and uses_old_layout(load_template(self.config)):
            self._warn(f"{self.config.prompt_template.name} names .autohelix/ paths "
                               f"in the agent's copy; they are now {ITERATION_DIR}/ (notes written to the old "
                               "place are lost).")

        # Run preflight checks before the baseline (silent on success); a first
        # start already ran them before allocating the run.
        if needs_baseline and not self._preflight_done and not self._preflight():
            raise AutoHelixRunError("Preflight checks failed. Fix the issues above before running.")
        self._segment_checked = True

        archived = self.run_info.pop("archived", None)
        for key, value in self._identity_changes.items():  # `resume --land` / `--group`
            if value is None:
                self.run_info.pop(key, None)
            else:
                self.run_info[key] = value
        if archived or self._identity_changes:
            write_json(self.paths.identity, self.run_info)  # resuming brings an archived run back

        # Capture baseline on first run
        baseline_stop = None
        if start_iter == 1 and needs_baseline:
            with self._showing(self._live_row(0)):
                baseline_stop = self._capture_baseline()

        # Dollars this segment has charged (known costs): the baseline's, then each iteration's.
        spent = (summarize_costs(self.history.cost_usages).cost_usd or 0) - cost_before

        cost_unknown = False  # some of this segment's charges are unavailable
        # Whether the main agent's cost was known in one of this segment's iterations
        # (None: no agent call yet). With --cost the only bound, unknown charges and an
        # agent never priced mean the backend can't price: --cost may never be reached.
        # Priced judges don't count (they alone would take long to reach the cap). One
        # unknown after a priced agent call (a timeout) only warns; three agent failures
        # in a row stop the segment anyway.
        priced: bool | None = None
        cost_only = budget.cost_usd is not None and max_iter is None and budget.time_seconds is None

        def out_of_budget() -> dict | None:
            if cost_only and cost_unknown and priced is False:
                return {"kind": "budget", "budget": "cost",
                        "detail": "cost unknown: --cost can't bound this segment (add -n or --time)"}
            if budget.cost_usd is not None and spent >= budget.cost_usd:
                return {"kind": "budget", "budget": "cost",
                        "detail": f"cost budget reached ({format_cap(budget.cost_usd)} this segment)"}
            if budget.time_seconds is not None and time.monotonic() - segment_start >= budget.time_seconds:
                return {"kind": "budget", "budget": "time",
                        "detail": f"time budget reached ({format_limit(budget.time_seconds)} this segment)"}
            return None

        config_name = config_relative(self.project_path, self.config_file)
        self.log.info(f"run started — config: {config_name}, max_iter: {max_iter or 'none'}, "
                      f"resuming from iter {start_iter}")

        ran = False
        cost_warning_shown = False

        def warn_unknown_cost() -> None:
            nonlocal cost_warning_shown, cost_unknown
            cost_unknown = True
            if budget.cost_usd is not None and not cost_warning_shown:
                terminal.warn(self.console, "cost unknown; --cost counts known costs only", indent=terminal.INDENT)
                cost_warning_shown = True

        if start_iter == 1 and needs_baseline:
            baseline = next((r for r in self.history.load() if r.iteration == 0 and r.accepted), None)
            if baseline is not None and baseline.cost.incomplete:
                warn_unknown_cost()

        # Why the loop ended: stop conditions, then `autohelix stop`, then budgets.
        stop_reason: dict | None = None
        if baseline_stop:
            stop_reason = self._condition_stop(baseline_stop, 0)
        iteration = start_iter
        agent_failures = 0  # consecutive failed agent calls in this segment
        try:
            for iteration in itertools.count(start_iter):
                if stop_reason:
                    break
                # First right after the baseline, then after each iteration: never during one.
                if stop_reason := self._requested_stop():
                    break
                if stop_reason := out_of_budget():
                    break
                if max_iter is not None and iteration > max_iter:
                    stop_reason = {"kind": "budget", "budget": "iterations",
                                   "detail": f"iteration budget reached ({max_iter})"}
                    break
                with self._showing(self._live_row(iteration)):
                    result = self.run_iteration(iteration)
                ran = True
                self._print_row(result)

                if result.stop:
                    stop_reason = self._condition_stop(result.stop, iteration)
                    break
                agent_failures = agent_failures + 1 if result.agent_error is not None else 0
                if agent_failures >= MAX_CONSECUTIVE_AGENT_FAILURES:
                    stop_reason = {"kind": "agent", "iteration": iteration, "error": result.agent_error,
                                   "detail": f"the agent failed {agent_failures} times in a row: "
                                             f"{clean(result.agent_error, 200)}"}
                    break
                if result.cost.incomplete:
                    warn_unknown_cost()
                if any(call["role"] == "agent" for call in result.calls):
                    if summarize_costs([result.usage]).cost_usd is not None:
                        priced = True
                    elif result.agent_error is None:  # a failed call says nothing about pricing
                        priced = bool(priced)
                spent += result.cost.cost_usd or 0
            self.run_status.note(stop_reason=stop_reason)
            self.log.info(f"run stopped — {stop_reason['detail']}")
        except KeyboardInterrupt:
            by = " by autohelix stop --now" if self._note_interrupt_request() else ""
            self.run_status.stop("interrupted")  # after the note: readers see the reason with the state
            self.log.info(f"Interrupted{by} at iteration {iteration}. Progress saved.")
            self._interrupted_at = iteration
            ran = False
        self._log_totals()
        self._land()  # before the report; the ending (`_print_ending`) says what landed
        return ran

    def _requested_stop(self, now: bool = False) -> dict | None:
        """The stop reason for this segment's `autohelix stop` request (with `now`: only a
        `--now` one), else None. The request is the CLI's file; a bad one is no request."""
        request = read_stop_request(self.paths, self._segment)
        if request is None or (now and request.get("now") is not True):
            return None
        why = clean(request["why"], WHY_LIMIT) if isinstance(request.get("why"), str) and request["why"].strip() else None
        requested_at = request.get("requested_at")
        detail = "requested by autohelix stop" + " --now" * now + (f": {why}" if why else "")
        return {"kind": "request", **({"now": True} if now else {}), "detail": detail, "why": why,
                "requested_at": requested_at if isinstance(requested_at, (int, float)) else None}

    def _note_interrupt_request(self) -> bool:
        """After an interrupt: when `autohelix stop --now` sent it, note that as the stop
        reason (the segment end record carries it). The state stays interrupted."""
        if not self._segment_started or self.run_status.data.get("stop_reason"):
            return False  # an earlier reason (the loop's own end) stands
        if (reason := self._requested_stop(now=True)) is None:
            return False
        self.run_status.note(stop_reason=reason)
        return True

    @staticmethod
    def _condition_stop(stop: dict[str, str], iteration: int) -> dict:
        where = "baseline" if iteration == 0 else f"iteration {iteration}"
        return {"kind": "condition", "iteration": iteration, **stop,
                "detail": f"stop condition met at {where}: {stop['reason']}"}

    def _log_totals(self) -> None:
        iterations = [r for r in self.history.load() if r.iteration > 0]
        accepted = sum(1 for r in iterations if r.accepted)
        seconds = sum(r.usage.get("wall_clock_ms", r.usage.get("duration_ms", 0)) for r in iterations) / 1000
        self.log.info(f"run complete — {accepted} accepted, {len(iterations) - accepted} rejected, "
                      f"{format_total_cost(self.history.cost_usages)}, {format_duration(seconds)}")

    def _print_ending(self, error: BaseException | None) -> None:
        """The end block (`terminal.Table.ending`): how the segment ended, each metric's
        baseline → tip, totals, what landed, what
        to type next, the files written. An error printed here is marked reported (the
        CLI then exits without printing it again)."""
        at = self._interrupted_at
        if at is None and error is not None and not isinstance(error, Exception):
            at = self.run_status.data.get("iteration") or 0
        outcome = terminal.outcome(self.run_status.data.get("stop_reason"), error, at)
        if isinstance(error, Exception):
            try:
                error.autohelix_reported = True
            except AttributeError:
                pass
        try:
            table = self._rows()
            short = short_id(self.paths.run_id)
            # What lands the commits that wait (a manual run, an auto landing that failed):
            # `autohelix land` when the target can fast-forward, else `git merge`.
            commands = terminal.next_commands(short, also=self._relation()[1].commands)
            lines = table.ending(outcome, spent=format_total_cost(self.history.cost_usages), landing=self._landing,
                                 commands=commands, files=self._files)
        except Exception as e:  # noqa: BLE001 - the outcome line still shows
            self.log.warn(f"Could not render the ending: {e}")
            lines = [Text(f"  {outcome[0]}  {outcome[1]}")]
        self._print_lines(lines)

    def _relation(self) -> tuple[Landing, LandingWords]:
        """The run's relation to its target now (`runs.landing`) and its words."""
        target = self.run_info.get("target_branch")
        try:
            found = landing(self.paths, self.run_info)
        except Exception:  # noqa: BLE001 - display only
            found = Landing(None, target, None, None)
        return found, landing_words(found.payload(), self.paths.run_id, target, land_mode=self.land_mode)
