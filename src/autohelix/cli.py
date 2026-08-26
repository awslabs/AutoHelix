# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Command-line interface."""

import subprocess
from importlib.resources import files as pkg_files
from pathlib import Path

import click

from autohelix.config import load_config
from autohelix.harness import AutoHelixRunError, Harness
from autohelix.history import History
from autohelix.state import (
    archive_state,
    ensure_gitignore_entry,
    read_stored_config_path,
)


def _parallel_marker_path(project_path: Path) -> Path:
    """Location of the parallel-run marker (points at resumable worktree state)."""
    return project_path / ".autohelix" / "parallel" / "run.json"


def _warn_if_parallel(project_path: Path, console) -> None:
    """If this repo was driven by `autohelix parallel`, warn that top-level
    commands (report/watch) see nothing — the run state lives in the workers'
    worktrees, not here. Lists where to point instead. Best-effort only.
    """
    import json as _json

    marker = _parallel_marker_path(project_path)
    if not marker.exists():
        return
    try:
        data = _json.loads(marker.read_text())
        workers = data.get("workers", [])
    except (OSError, ValueError):
        return
    if not workers:
        return
    console.print(
        "[yellow]This repo was run with `autohelix parallel`.[/yellow] "
        "Top-level state is empty — each worker keeps its own state in its worktree."
    )
    console.print("  Point at a worker instead, e.g.:")
    for w in workers:
        console.print(f"    [cyan]-p {w.get('worktree')}[/cyan]", highlight=False)
    console.print()


def _get_version() -> str:
    """Get version from package metadata."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("autohelix")
    except PackageNotFoundError:
        return "dev"


@click.group()
@click.version_option(version=_get_version(), prog_name="autohelix")
def main():
    """AutoHelix - Autonomous code improvement harness."""


def _run_git(args: list[str], cwd: Path, error_msg: str) -> subprocess.CompletedProcess:
    """Run a git command, raising ClickException on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        hint = f"\n  git {' '.join(args)} failed: {stderr}" if stderr else ""
        raise click.ClickException(f"{error_msg}{hint}")
    return result


CONFIG_TEMPLATE = (
    pkg_files("autohelix.templates").joinpath("config_template.yaml").read_text()
)


@main.command()
@click.argument("path", type=click.Path(), default=".")
def init(path: str):
    """Initialize AutoHelix in a project directory.

    Creates .autohelix/ state directory, a prompt template, and
    autohelix.yaml config (if not already present).
    """
    project_path = Path(path).resolve()
    autohelix_dir = project_path / ".autohelix"

    if autohelix_dir.exists():
        click.echo(f"Already initialized ({autohelix_dir})")
        return

    # Require git
    git_check = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=project_path,
        capture_output=True,
    )
    if git_check.returncode != 0:
        click.echo(
            "Not a git repository. AutoHelix requires git.\n"
            "Run: git init && git add -A && git commit -m 'init'"
        )
        raise SystemExit(1)

    # Create .autohelix/ structure. `notes/` is the agent-owned memory dir; the
    # default prompt teaches the iter-N.md convention, but it is free-form.
    autohelix_dir.mkdir()
    (autohelix_dir / "notes").mkdir()

    # Write prompt template
    from autohelix.prompt_template import DEFAULT_TEMPLATE

    (autohelix_dir / "prompt.md").write_text(DEFAULT_TEMPLATE)

    # Ensure .autohelix/ is gitignored
    ensure_gitignore_entry(project_path, ".autohelix/")
    ensure_gitignore_entry(project_path, "__pycache__/")

    # Write config template if none exists
    config_path = project_path / "autohelix.yaml"
    if not config_path.exists():
        config_path.write_text(CONFIG_TEMPLATE)

    click.echo(f"Initialized AutoHelix at {project_path}")
    click.echo(f"\nNext steps:")
    click.echo(f"  1. Edit autohelix.yaml — set your goal")
    click.echo(f"  2. autohelix run")


@main.command()
@click.option("--path", "-p", type=click.Path(exists=True), default=".", help="Project directory (default: current dir)")
@click.option("--iterations", "-n", type=int, help="Maximum iterations to run")
@click.option("--verbose", is_flag=True, help="Show agent command details and heartbeat messages")
@click.option("--config", "-c", "config_file", type=click.Path(exists=True), help="Path to config file (default: autohelix.yaml in project dir)")
def run(path: str, iterations: int | None, verbose: bool, config_file: str | None):
    """Run the optimization loop.

    Reads configuration from autohelix.yaml (or --config).
    Requires 'autohelix init' to have been run first.
    """
    project_path = Path(path).resolve()
    autohelix_dir = project_path / ".autohelix"

    if not autohelix_dir.exists():
        click.echo("Error: AutoHelix not initialized.")
        click.echo("Run 'autohelix init' first.")
        raise SystemExit(1)

    config_path = Path(config_file).resolve() if config_file else None
    try:
        harness = Harness(project_path, verbose=verbose, config_file=config_path)
    except FileNotFoundError as e:
        click.echo(f"Error: {e}")
        click.echo("Run 'autohelix init' first, then edit autohelix.yaml.")
        raise SystemExit(1)
    try:
        harness.run(max_iterations=iterations)
    except AutoHelixRunError as e:
        raise click.ClickException(str(e)) from e


@main.command()
@click.argument("path", type=click.Path(exists=True), default=".")
def clear(path: str):
    """Clear AutoHelix state so the next run starts fresh.

    Archives history, notes, reviews, and logs to .autohelix/archive/<timestamp>/.

    This only resets RUN STATE. It does NOT touch your code or external outputs.
    For a full reset you typically also need:
      1. git reset --hard <initial-commit>   (roll back the agent's per-iteration commits)
      2. delete any outputs written outside the repo (e.g. model checkpoints)
    Then commit so the tree is clean — `run` refuses to start on a dirty tree.
    """
    import shutil
    import subprocess

    from rich.console import Console

    console = Console()
    project_path = Path(path).resolve()
    autohelix_dir = project_path / ".autohelix"

    if not autohelix_dir.exists():
        console.print("[red]AutoHelix not initialized. Run 'autohelix init' first.[/red]")
        return

    if not click.confirm("Archive state?"):
        return

    archive_state(project_path, console)

    # Clean up worktrees (not archived — they're git state, not useful to keep)
    work_path = autohelix_dir / "worktrees"
    if work_path.exists():
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        for line in result.stdout.split("\n"):
            if line.startswith("worktree ") and "autohelix" in line:
                wt_path = line.replace("worktree ", "")
                subprocess.run(
                    ["git", "worktree", "remove", wt_path, "--force"],
                    cwd=project_path,
                    capture_output=True,
                )
        if work_path.exists():
            shutil.rmtree(work_path)
        console.print("  [dim]✓[/dim] Cleaned up worktrees")

    # Ensure .claudeignore excludes archive from agent context
    claudeignore_path = project_path / ".claudeignore"
    needs_archive_entry = True
    if claudeignore_path.exists():
        content = claudeignore_path.read_text()
        if ".autohelix/archive" in content:
            needs_archive_entry = False
    if needs_archive_entry:
        with open(claudeignore_path, "a") as f:
            if claudeignore_path.exists() and claudeignore_path.stat().st_size > 0:
                f.write("\n")
            f.write("# AutoHelix archives (past runs)\n.autohelix/archive/\n")

    console.print("\n[green]Done.[/green]")


@main.command(context_settings={"ignore_unknown_options": True})
@click.option("--worker", "worker_configs", multiple=True,
              type=click.Path(exists=True),
              help="Per-worker config file (repeatable). One worker per --worker. "
                   "Omit to resume the repo's previous parallel run.")
@click.option("--path", "-p", type=click.Path(exists=True), default=".",
              help="Target git repo (default: current dir)")
@click.option("--sync-notes-every", "sync_every", type=int, default=None,
              help="Sync notes between workers every N iterations (default: 1).")
@click.option("--verbose", is_flag=True, help="Verbose per-worker run output")
@click.argument("run_args", nargs=-1, type=click.UNPROCESSED)
def parallel(worker_configs: tuple[str, ...], path: str, sync_every: int | None,
             verbose: bool, run_args: tuple[str, ...]):
    """Run multiple AutoHelix workers in parallel on the same goal.

    Each --worker config becomes an independent worker in its own git worktree
    (branch parallel/worker-N). Workers share notes via symlinks so each agent
    can read what peers have tried. Notes are synced every --sync-notes-every iterations.

    Per-worker settings (model, timeout, agent, budget, etc.) come from each
    worker's own config file, not from the command line.

    Run with no --worker to resume the repo's previous parallel run (from -p or
    the current dir): the recorded configs and sync interval are reused.
    Completed iterations are not repeated.

    \b
        autohelix parallel --worker a.yaml --worker b.yaml --worker c.yaml
        autohelix parallel --worker a.yaml --worker b.yaml --sync-notes-every 3
        autohelix parallel                 # resume previous run in this repo
    """
    import json as json_mod

    from rich.console import Console

    from autohelix.parallel import leaderboard, run_parallel

    console = Console()

    repo = Path(path).resolve()

    # Resume: with no --worker, reconstruct the run from the repo's marker.
    if not worker_configs:
        marker = _parallel_marker_path(repo)
        data = None
        if marker.exists():
            try:
                data = json_mod.loads(marker.read_text())
            except (OSError, ValueError):
                data = None
        recorded = (data or {}).get("configs")
        if not recorded:
            raise click.UsageError(
                "No --worker given and no previous parallel run found in this repo "
                f"({repo}). Pass --worker <config> (repeatable), or run from the repo "
                "of a prior `autohelix parallel` to resume it."
            )
        missing = [c for c in recorded if not Path(c).exists()]
        if missing:
            raise click.UsageError(
                "Cannot resume — worker config(s) no longer exist: "
                + ", ".join(missing)
            )
        worker_configs = tuple(recorded)
        if sync_every is None:
            sync_every = (data or {}).get("sync_every", 1)
        if not run_args:
            run_args = tuple((data or {}).get("run_args", []))
        console.print(f"  [dim]Resuming previous run ({len(worker_configs)} workers).[/dim]")

    if sync_every is None:
        sync_every = 1

    forbidden = {"-n", "--iterations", "-c", "--config", "-p", "--path"}
    for arg in run_args:
        flag = arg.split("=", 1)[0]
        if flag in forbidden:
            raise click.UsageError(
                f"'{flag}' is managed by `parallel` and cannot be forwarded to workers. "
                "Set per-worker iterations via budget.iterations in each config; "
                "config/path are wrapper-controlled."
            )

    configs = [Path(c).resolve() for c in worker_configs]

    fwd = f"  [dim]forwarding: {' '.join(run_args)}[/dim]" if run_args else ""
    console.print(f"\n  [bold]AutoHelix parallel[/bold] — {len(configs)} workers, sync notes every {sync_every}{fwd}\n")
    for i, cfg in enumerate(configs, start=1):
        console.print(f"  [dim]worker-{i}:[/dim] {cfg.name}", highlight=False)
    console.print()

    workers = run_parallel(repo, configs, sync_every=sync_every,
                           verbose=verbose, run_args=list(run_args))

    # Write a small marker recording where the resumable state lives and how the
    # run was launched. The worktrees remain the source of truth (they hold each
    # worker's live .autohelix/ state); the marker lets top-level report/watch
    # warn that the main repo's state is empty and point at the workers, and lets
    # a bare `autohelix parallel` resume without re-listing --worker configs.
    marker = _parallel_marker_path(repo)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json_mod.dumps({
        "configs": [str(c) for c in configs],
        "sync_every": sync_every,
        "run_args": list(run_args),
        "workers": [
            {"name": w.name, "worktree": str(w.worktree), "branch": w.branch}
            for w in workers
        ],
    }, indent=2))

    ranked = leaderboard(workers)
    scored_metrics = {w.best_metric for w in ranked if w.best_score is not None}
    is_ranked = len(scored_metrics) <= 1
    heading = "Leaderboard" if is_ranked else "Results"
    console.print(f"\n  [bold]{heading}[/bold]")
    for i, w in enumerate(ranked, start=1):
        prefix = f"   {i}." if is_ranked else "   •"
        cost = f"  cost=${w.total_cost:.2f}" if w.total_cost else ""
        delta = ""
        delta_style = ""
        if w.best_score is not None and w.best_metric:
            hist = History(w.worktree)
            results = hist.load()
            baseline_r = next((r for r in results if r.iteration == 0), None)
            if baseline_r and w.best_metric in baseline_r.metrics:
                baseline_val = baseline_r.metrics[w.best_metric]
                if baseline_val != 0 and baseline_val != w.best_score:
                    from autohelix.formatting import format_delta
                    delta_text = format_delta(w.best_score, baseline_val)
                    pct = (w.best_score - baseline_val) / baseline_val * 100
                    improved = (pct < 0) if w.direction == "lower" else (pct > 0)
                    delta_style = "green" if improved else "red"
                    delta = f" ({delta_text})"
        if w.error:
            console.print(f"{prefix} {w.name}  [red]error: {w.error}[/red]")
        elif w.best_score is not None:
            # delta_style is only set when the score differs from baseline; guard
            # against empty markup tags (`[][/]`), which raise MarkupError.
            delta_markup = f" [{delta_style}]{delta}[/{delta_style}]" if delta_style else delta
            console.print(f"{prefix} {w.name}  {w.best_metric}={w.best_score:g}{delta_markup}  {cost}", highlight=False)
        else:
            console.print(f"{prefix} {w.name}  [dim]no score[/dim]  {cost}", highlight=False)

    # Worktrees and branches are left in place so a re-run resumes each worker
    # and the user can merge the winner or discard the rest.
    root = repo.parent / f"{repo.name}-parallel"
    branches = [w.branch for w in workers]
    # Only offer to merge a worker that actually produced a score — merging an
    # errored/no-score branch is a footgun.
    best = ranked[0] if ranked and ranked[0].best_score is not None else None

    console.print(f"\n  [dim]Code on branches: {', '.join(branches)}[/dim]")
    console.print(f"  [dim]Worktrees (resumable state) at: {root}[/dim]")
    console.print(f"\n  [dim]Re-run `autohelix parallel` (from {repo}) to resume each worker from where it stopped.[/dim]")
    if best:
        losers = [w for w in workers if w.branch != best.branch]
        console.print(f"\n  [bold]Best:[/bold] {best.name}", highlight=False)
        console.print("\n  [dim]# from your main repo, on a clean branch you want the result on:[/dim]")
        console.print(f"    [cyan]git merge {best.branch}[/cyan]", highlight=False)
        if losers:
            console.print("\n  [dim]# inspect a loser before discarding:[/dim]")
            console.print(f"    [cyan]git diff HEAD..{losers[0].branch}[/cyan]", highlight=False)
        console.print("\n  [dim]# when done — removes worktrees and deletes branches (unmerged work is lost):[/dim]")
        for b in branches:
            console.print(f"    [cyan]git worktree remove {root / b.split('/')[-1]}[/cyan]", highlight=False)
        console.print(f"    [cyan]git branch -D {' '.join(branches)}[/cyan]", highlight=False)
    console.print()


@main.command()
@click.argument("message")
@click.option("--path", "-p", type=click.Path(exists=True), default=".", help="Project directory")
def hint(message: str, path: str):
    """Send a hint to the running agent (picked up next iteration).

    Appends a timestamped message to .autohelix/hints.md.
    The agent sees this file in its "Read before starting" list.
    """
    from datetime import datetime

    project_path = Path(path).resolve()
    hints_path = project_path / ".autohelix" / "hints.md"

    if not hints_path.parent.exists():
        click.echo("Error: AutoHelix not initialized. Run 'autohelix init' first.")
        raise SystemExit(1)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"- [{timestamp}] {message}\n"

    with open(hints_path, "a") as f:
        f.write(entry)

    click.echo(f"Hint added: {message}")


@main.command()
@click.argument("path", type=click.Path(exists=True), default=".")
def watch(path: str):
    """Live-tail the current iteration's agent output.

    Run this in a second terminal while `autohelix run` is active.
    Shows tool calls, text output, and elapsed time in a live panel.
    """
    import time

    from rich.console import Console
    from rich.live import Live

    console = Console()
    project_path = Path(path).resolve()
    logs_dir = project_path / ".autohelix" / "logs"

    if not logs_dir.exists():
        _warn_if_parallel(project_path, console)
        console.print("[yellow]No agent logs found. Is autohelix running?[/yellow]")
        raise SystemExit(1)

    def _find_latest_log() -> Path | None:
        logs = sorted(logs_dir.glob("iter-*/agent.log"), key=lambda p: p.stat().st_mtime)
        return logs[-1] if logs else None

    log_path = _find_latest_log()
    if not log_path:
        console.print("[yellow]No iteration logs found. Waiting...[/yellow]")
        for _ in range(30):
            time.sleep(1)
            log_path = _find_latest_log()
            if log_path:
                break
        if not log_path:
            console.print("[red]No logs appeared. Is autohelix running?[/red]")
            raise SystemExit(1)

    # Extract iteration number from parent directory name
    iter_name = log_path.parent.name.replace("iter-", "Iteration ")

    from autohelix.agents import AgentConfig, AgentEvent
    from autohelix.agents.claudecode import ClaudeCodeAgent
    from autohelix.display import LiveDisplay

    display = LiveDisplay(console=console, title=iter_name, verbose=True)

    # Use ClaudeCodeAgent's line parser (same logic as --verbose harness)
    agent = ClaudeCodeAgent(config=AgentConfig(), heartbeat_seconds=30)

    import json as json_mod

    # Skip event types that duplicate streamed content when replaying logs
    _SKIP_TYPES = {"assistant", "user"}

    def on_event(event: AgentEvent) -> None:
        display.handle_event(event)

    try:
        with Live(display, console=console, refresh_per_second=4) as live:
            while True:
                with open(log_path) as f:
                    while True:
                        line = f.readline()
                        if line:
                            line = line.strip()
                            if line:
                                try:
                                    if json_mod.loads(line).get("type") in _SKIP_TYPES:
                                        continue
                                except (json_mod.JSONDecodeError, ValueError):
                                    pass
                                agent._handle_line(line, on_event)
                        else:
                            newer = _find_latest_log()
                            if newer and newer != log_path:
                                log_path = newer
                                iter_name = log_path.parent.name.replace("iter-", "Iteration ")
                                display = LiveDisplay(console=console, title=iter_name, verbose=True)
                                agent = ClaudeCodeAgent(config=AgentConfig(), heartbeat_seconds=30)
                                live.update(display)
                                break
                            time.sleep(0.3)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped watching.[/dim]")


@main.command()
@click.argument("path", type=click.Path(exists=True), default=".")
@click.option("--config", "-c", "config_file", type=click.Path(exists=True), help="Path to config file (default: from last run, or autohelix.yaml)")
def report(path: str, config_file: str | None):
    """Generate an agent-written analysis of the run.

    Uses a one-shot agent call to analyze iteration history, notes,
    and git log, then writes .autohelix/output/report.md with insights on
    what worked, what failed, and what to try next.
    """
    from rich.console import Console
    from rich.live import Live

    from autohelix.agents import AgentConfig, AgentEvent, create_agent
    from autohelix.display import LiveDisplay

    console = Console()
    project_path = Path(path).resolve()

    # Resolve config: explicit flag > stored path > default
    if config_file:
        resolved_config = Path(config_file).resolve()
    else:
        resolved_config = read_stored_config_path(project_path)

    try:
        config, _ = load_config(project_path, config_file=resolved_config)
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)

    history = History(project_path)
    results = history.load()

    if not results:
        _warn_if_parallel(project_path, console)
        console.print("[yellow]No iterations run yet. Nothing to report.[/yellow]")
        raise SystemExit(1)

    # Gather context for the report agent
    report_prompt = _build_report_prompt(config, history, project_path)

    # Create agent for the report
    agent_config = AgentConfig(
        type=config.agent.type,
        command=config.agent.command,
        model=config.agent.model,
        timeout_seconds=config.agent.timeout_seconds or 300,
        auto_memory=False,
    )
    agent = create_agent(agent_config, heartbeat_seconds=30)

    report_path = project_path / ".autohelix" / "output" / "report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    logs_dir = project_path / ".autohelix" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "report.log"

    display = LiveDisplay(console=console, title="preparing report", verbose=False)

    def on_event(event: AgentEvent) -> None:
        display.handle_event(event)

    with Live(display, console=console, refresh_per_second=10, transient=True) as live:
        result = agent.run(
            worktree_path=project_path,
            prompt=report_prompt,
            iteration=0,
            log_path=log_path,
            event_callback=on_event,
        )
        display.flush()

    if report_path.exists():
        console.print(f"\n[green]Report written to .autohelix/output/report.md[/green]")
    else:
        console.print(f"\n[yellow]Agent finished but report.md was not created.[/yellow]")


def _build_report_prompt(config, history: History, project_path: Path) -> str:
    """Build the prompt for the report agent."""
    results = history.load()
    iterations = [r for r in results if r.iteration > 0]
    baseline = next((r for r in results if r.iteration == 0), None)
    accepted = sum(1 for r in iterations if r.accepted)
    rejected = len(iterations) - accepted

    directions = config.metric_directions()
    best = history.get_best_metrics(directions)

    # Detect if run is still in progress
    max_iter = config.max_iterations
    in_progress = len(iterations) < max_iter

    lines = [
        "Analyze this AutoHelix optimization run and write a report.",
        "",
        f"Write the report to .autohelix/output/report.md",
        "",
        f"## Context",
        f"- Goal: {config.goal.strip()}",
        f"- Iterations: {len(iterations)}" + (f" of {max_iter} (run still in progress)" if in_progress else " (complete)"),
        f"- Accepted: {accepted}, Rejected: {rejected}",
    ]

    # Baseline and best metrics
    if baseline and baseline.metrics:
        lines.append("")
        lines.append("## Baseline metrics")
        for name, value in baseline.metrics.items():
            lines.append(f"- {name}: {value}")

    if best:
        lines.append("")
        lines.append("## Best metrics achieved")
        for name, (value, iter_num) in best.items():
            direction = directions.get(name, "higher")
            baseline_val = baseline.metrics.get(name) if baseline and baseline.metrics else None
            improvement = ""
            if baseline_val and baseline_val != 0:
                pct = ((value - baseline_val) / baseline_val) * 100
                improvement = f" ({pct:+.1f}% from baseline)"
            lines.append(f"- {name}: {value} (iteration {iter_num}){improvement}")

    # Full iteration history
    lines.append("")
    lines.append("## Iteration history")
    for r in iterations:
        if r.accepted:
            metrics_str = ", ".join(f"{k}={v}" for k, v in r.metrics.items())
            lines.append(f"- iter {r.iteration}: accepted — {metrics_str}")
        else:
            lines.append(f"- iter {r.iteration}: rejected — {r.reason or 'constraints failed'}")

    # Notes
    notes_dir = project_path / ".autohelix" / "notes"
    if notes_dir.exists():
        note_files = sorted(f for f in notes_dir.iterdir() if f.is_file() and f.suffix == ".md")
        if note_files:
            lines.append("")
            lines.append("## Agent notes")
            for note_file in note_files[-10:]:  # last 10 notes
                content = note_file.read_text().strip()
                if content:
                    lines.append(f"\n### {note_file.name}")
                    lines.append(content)

    # Last review (highest-numbered reviews/iter-N.md)
    reviews_dir = project_path / ".autohelix" / "reviews"
    if reviews_dir.exists():
        review_files = sorted(
            reviews_dir.glob("iter-*.md"),
            key=lambda p: int(p.stem.split("-", 1)[1]) if p.stem.split("-", 1)[1].isdigit() else -1,
        )
        if review_files:
            review = review_files[-1].read_text().strip()
            if review:
                lines.append("")
                lines.append("## Last review")
                lines.append(review)

    # Git log
    git_log = subprocess.run(
        ["git", "log", "--oneline", "-20"],
        cwd=project_path, capture_output=True, text=True,
    )
    if git_log.returncode == 0 and git_log.stdout.strip():
        lines.append("")
        lines.append("## Recent git history")
        lines.append(git_log.stdout.strip())

    lines.append("")
    lines.append("## Instructions")
    lines.append(
        "Write a concise report covering: (1) summary, (2) metric progression, "
        "(3) key changes that worked, (4) failed approaches, (5) suggestions for next steps. "
        "Be specific — reference iteration numbers and concrete strategies."
    )

    return "\n".join(lines)


if __name__ == "__main__":
    main()
