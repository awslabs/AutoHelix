# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Command-line interface."""

import json
import os
import subprocess
from importlib.resources import files as pkg_files
from pathlib import Path

import click

from autohelix.config import load_config
from autohelix.harness import Harness
from autohelix.history import History
from autohelix.runs import (NoRunToResume, RunPaths, RunLock, archive as archive_run, delete as delete_run,
                            land as land_run, resolve_run, run_tip, short_id)
from autohelix.formatting import format_total_cost
from autohelix import run_table
from autohelix.skill import skill
from autohelix.state import (
    GitExcludeError,
    ensure_git_exclude_entry,
    ensure_runtime_ignored,
    read_stored_config_path,
)


def _parallel_marker_path(project_path: Path) -> Path:
    """Location of the parallel-run marker (points at resumable worktree state)."""
    return project_path / ".autohelix" / "parallel" / "run.json"


def _warn_if_parallel(project_path: Path, console) -> None:
    """If this repo was driven by `autohelix parallel`, warn that top-level
    commands (report/view) see nothing — the run state lives in the workers'
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


main.add_command(skill)


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

    # Validate before creating runtime/config files, and on repeated init calls.
    try:
        ensure_runtime_ignored(project_path)
        # Auxiliary cache exclusion is best effort; runtime state must be ignored.
        ensure_git_exclude_entry(project_path, "__pycache__/")
    except (GitExcludeError, OSError, subprocess.CalledProcessError) as e:
        raise click.ClickException(str(e)) from e

    if autohelix_dir.exists():
        click.echo(f"Already initialized ({autohelix_dir})")
        return

    # Create .autohelix/ structure. `notes/` is the agent-owned memory dir; the
    # default prompt teaches the iter-N.md convention, but it is free-form.
    autohelix_dir.mkdir()

    # Write prompt template
    from autohelix.prompt_template import DEFAULT_TEMPLATE

    (autohelix_dir / "prompt.md").write_text(DEFAULT_TEMPLATE)

    # Write config template if none exists
    config_path = project_path / "autohelix.yaml"
    if not config_path.exists():
        config_path.write_text(CONFIG_TEMPLATE)

    click.echo(f"Initialized AutoHelix at {project_path}")
    click.echo(f"\nNext steps:")
    click.echo(f"  1. Edit autohelix.yaml — set your goal")
    click.echo(f"  2. autohelix run")


def _run_id(project: Path, value: str | None) -> str | None:
    """A run id as typed (full, or its last part), checked; None follows `current`."""
    if value is None:
        return None
    try:
        return resolve_run(project, value)
    except RuntimeError as error:
        raise click.ClickException(str(error)) from error


def _segment_options(command):
    """The options `run` and `resume` share."""
    for option in reversed([
        click.option("--path", "-p", type=click.Path(exists=True, file_okay=False), default=".",
                     help="Project directory (default: current dir)"),
        click.option("--config", "-c", "config_file", type=click.Path(exists=True, dir_okay=False),
                     help="Config file (default: autohelix.yaml; for resume, the run's own)"),
        click.option("--verbose", is_flag=True, help="Show agent command details and heartbeat messages"),
        click.option("--no-report", is_flag=True, help="Don't write the run summary when the run finishes"),
        click.option("--land", "land_mode", type=click.Choice(["auto", "manual"]),
                     help="Fast-forward the target branch at the end (auto, the default) or leave it to "
                          "`autohelix land` (manual)."),
    ]):
        command = option(command)
    return command


def _start(path: str, config_file: str | None, **selection) -> Harness:
    project_path = Path(path).resolve()
    if not (project_path / ".autohelix").exists():
        click.echo("Error: AutoHelix not initialized.")
        click.echo("Run 'autohelix init' first.")
        raise SystemExit(1)
    # Refuse before the harness creates logs, settings, or agent state.
    try:
        ensure_runtime_ignored(project_path)
    except GitExcludeError as e:
        raise click.ClickException(str(e)) from e
    config_path = Path(config_file).resolve() if config_file else None
    try:
        return Harness(project_path, config_file=config_path, **selection)
    except NoRunToResume as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(3)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from e
    except FileNotFoundError as e:
        click.echo(f"Error: {e}")
        click.echo("Run 'autohelix init' first, then edit autohelix.yaml.")
        raise SystemExit(1)


def _segment(harness: Harness, iterations: int | None, report: bool) -> None:
    try:
        harness.run(max_iterations=iterations, report=report)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from e


@main.command()
@_segment_options
@click.option("--iterations", "-n", type=click.IntRange(min=1), help="Iterations to run (default: budget.iterations)")
@click.option("--name", help="Readable part of the new run's id.")
def run(path: str, config_file: str | None, verbose: bool, no_report: bool, land_mode: str | None,
        iterations: int | None, name: str | None):
    """Start a new run of the optimization loop.

    Always a new run, with its own budget, notes and history; to continue one,
    use `autohelix resume`. Reads autohelix.yaml (or --config).
    """
    harness = _start(path, config_file, verbose=verbose, new=True, name=name, land_mode=land_mode)
    _segment(harness, iterations, not no_report)


@main.command()
@click.argument("run_id", required=False)
@_segment_options
@click.option("--iterations", "-n", type=click.IntRange(min=1), help="How many more iterations to run.")
@click.option("--until", type=click.IntRange(min=1), help="Run up to iteration N (repeatable; for scripts).")
def resume(run_id: str | None, path: str, config_file: str | None, verbose: bool, no_report: bool,
           land_mode: str | None, iterations: int | None, until: int | None):
    """Continue a run: RUN_ID (or its last part), else this config's latest.

    The latest is the most recently active run for the config that isn't
    archived, preferring runs that captured their baseline. Without -n or --until it runs to
    budget.iterations; cost and time budgets stay run totals from the config.
    Exits 3 when there is no run to resume.
    """
    if iterations and until:
        raise click.UsageError("Use either -n (more iterations) or --until (up to an iteration), not both.")
    selected = _run_id(Path(path), run_id)
    harness = _start(path, config_file, verbose=verbose, run_id=selected, create=False, land_mode=land_mode)
    if iterations:
        until = harness.history.get_last_iteration() + iterations
    _segment(harness, until, not no_report)


@main.command()
@click.argument("run_id", required=False)
@click.option("--path", "-p", type=click.Path(exists=True), default=".")
@click.option("--into", help="Target branch, required for runs created detached.")
def land(run_id: str | None, path: str, into: str | None):
    """Fast-forward a target branch to this run's accepted code."""
    try:
        paths = RunPaths(Path(path), _run_id(Path(path), run_id))
        if not paths.run_id:
            raise RuntimeError("No current run.")
        with RunLock(paths.lock):
            data = paths.read_identity()
            land_run(paths, data, run_tip(paths, data), into)
        click.echo(f"Landed {short_id(paths.run_id)}.")
    except RuntimeError as e:
        raise click.ClickException(str(e)) from e


@main.command()
@click.argument("run_ids", nargs=-1, required=True)
@click.option("--path", "-p", type=click.Path(exists=True), default=".")
@click.option("--undo", is_flag=True, help="Bring archived runs back.")
def archive(run_ids: tuple[str, ...], path: str, undo: bool):
    """Set runs aside: hidden from the run list and skipped by a bare resume.

    Their history, notes and commits stay; `autohelix resume RUN_ID` (or
    --undo) brings one back.
    """
    project = Path(path)
    for value in run_ids:
        paths = RunPaths(project, _run_id(project, value))
        try:
            archive_run(paths, undo=undo)
        except RuntimeError as e:
            raise click.ClickException(str(e)) from e
        click.echo(f"{'Unarchived' if undo else 'Archived'} {short_id(paths.run_id)}.")


@main.command()
@click.argument("run_ids", nargs=-1, required=True)
@click.option("--path", "-p", type=click.Path(exists=True), default=".")
@click.option("--force", is_flag=True, help="Also delete runs with unlanded commits (they are lost).")
def delete(run_ids: tuple[str, ...], path: str, force: bool):
    """Delete runs: their history, notes, logs, ref and leftover copies.

    Refuses while a run is running, and for runs with commits not on their
    target branch unless --force. To keep a run but hide it, use archive.
    """
    project = Path(path)
    for value in run_ids:
        paths = RunPaths(project, _run_id(project, value))
        try:
            delete_run(paths, force=force, warn=lambda message: click.echo(f"Warning: {message}", err=True))
        except RuntimeError as e:
            raise click.ClickException(str(e)) from e
        click.echo(f"Deleted {short_id(paths.run_id)}.")


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

    # Workers run `autohelix resume <id> --until N`: run-only and selection flags would fail there.
    forbidden = {"-n", "--iterations", "--until", "-c", "--config", "-p", "--path", "--name", "--new", "--run"}
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
    # worker's live .autohelix/ state); the marker lets top-level report/view
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
        hist = History(w.worktree, w.run_id)
        results = hist.load()
        usages = hist.cost_usages
        cost_text = format_total_cost(usages) if usages else None
        cost = (
            f"  cost={cost_text}" if cost_text and cost_text.startswith("$")
            else f"  {cost_text}" if cost_text else ""
        )
        delta = ""
        delta_style = ""
        if w.best_score is not None and w.best_metric:
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
    if any(w.error for w in workers):
        raise click.ClickException("One or more parallel workers failed; see worker errors above.")


@main.command()
@click.argument("question")
@click.option("--yes", "yes_means", help='What a "yes" answer means.')
@click.option("--no", "no_means", help='What a "no" answer means.')
@click.option("--rubric", type=click.Path(exists=True, dir_okay=False), help="File with criteria for the answer.")
@click.option("--agent", "agent_type", type=click.Choice(["claude", "codex", "opencode", "mock"]),
              help="Agent backend (default: agent.type from the config, else claude).")
@click.option("--model", help="Model (default: agent.model from the config).")
@click.option("--effort", help="Reasoning effort (default: agent.reasoning_effort from the config).")
@click.option("--timeout", type=click.IntRange(min=1), help="Seconds before giving up (default 600).")
def ask(question: str, yes_means: str | None, no_means: str | None, rubric: str | None,
        agent_type: str | None, model: str | None, effort: str | None, timeout: int | None):
    """Ask an agent a yes/no question about the current directory.

    Exits 0 for yes, 1 for no, 2 if no valid answer was produced; prints the
    reason. Use it in stop: or constraints:, or in any script. The agent gets
    read-only tools and no project settings; agent settings come from
    $AUTOHELIX_CONFIG or ./autohelix.yaml.
    """
    import signal

    from autohelix.formatting import format_cost
    from autohelix.judge import JudgeError, ask as run_ask, judge_config, load_base_config
    from autohelix.usage_log import ENV as USAGE_ENV

    def _terminate(signum, frame):  # the harness timed this command out: stop the agent too
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    cwd = Path.cwd()
    try:
        config = judge_config(load_base_config(cwd), agent=agent_type, model=model, effort=effort,
                              timeout=timeout)
        answer = run_ask(question, cwd, config, yes=yes_means, no=no_means,
                         rubric=Path(rubric).read_text() if rubric else None)
    except JudgeError as e:
        click.echo(f"autohelix ask: no valid answer: {e}", err=True)
        raise SystemExit(2)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as e:  # noqa: BLE001 - exit 1 would read as "no"
        click.echo(f"autohelix ask: failed: {type(e).__name__}: {e}", err=True)
        raise SystemExit(2)
    click.echo(answer.reason)
    if not os.environ.get(USAGE_ENV):
        click.echo(f"autohelix ask: {'yes' if answer.yes else 'no'} · {format_cost(answer.cost_usd, precision=4)}", err=True)
    raise SystemExit(0 if answer.yes else 1)


@main.command()
@click.argument("message")
@click.option("--path", "-p", type=click.Path(exists=True), default=".", help="Project directory")
@click.option("--run", "run_id", help="Run id or its last part (default: the current run).")
def hint(message: str, path: str, run_id: str | None):
    """Send a hint to the running agent (picked up next iteration).

    Appends a timestamped message to the selected run's hints.md.
    The agent sees this file in its "Read before starting" list.
    """
    from datetime import datetime

    project_path = Path(path).resolve()
    try:
        paths = RunPaths(project_path, _run_id(project_path, run_id))
        if not paths.run_id:
            raise RuntimeError("No current run; start a run first.")
        paths.read_identity()
    except RuntimeError as error:
        raise click.ClickException(str(error)) from error
    hints_path = paths.hints

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"- [{timestamp}] {message}\n"

    with open(hints_path, "a") as f:
        f.write(entry)

    click.echo(f"Hint added: {message}")


_JSON_HELP = "Print once as JSON and exit (for scripts and agents; fields are only ever added)."
_ONCE_HELP = "Print the table once and exit."
_WEB_HELP = "Serve a live web page on 127.0.0.1 instead."
_PORT_HELP = "Port for --web (default 8765)."


def _mode(as_json: bool, once: bool, web: bool, port: int | None, open_browser: bool) -> str:
    """live (the default), once, json or web; one at a time."""
    if as_json + once + web > 1:
        raise click.UsageError("Use one of --json, --once and --web.")
    if not web and (port is not None or open_browser):
        raise click.UsageError("--port and --open apply to --web.")
    return "json" if as_json else "once" if once else "web" if web else "live"


def _mode_options(command):
    """The options `view` and `dashboard` share: the project and one mode."""
    for option in reversed([
        click.option("--path", "-p", type=click.Path(exists=True, file_okay=False, path_type=Path), default="."),
        click.option("--once", is_flag=True, help=_ONCE_HELP),
        click.option("--json", "as_json", is_flag=True, help=_JSON_HELP),
        click.option("--web", is_flag=True, help=_WEB_HELP),
        click.option("--port", type=click.IntRange(0, 65535), help=_PORT_HELP),
        click.option("--open", "open_browser", is_flag=True, help="With --web: open the page in a browser."),
    ]):
        command = option(command)
    return command


@main.command()
@click.argument("run_id", required=False)
@_mode_options
@click.option("--config", "-c", "config_file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Config file (default: from last run, or autohelix.yaml)")
def view(run_id: str | None, path: Path, once: bool, as_json: bool, web: bool, port: int | None,
         open_browser: bool, config_file: Path | None):
    """One run, live in the terminal: RUN_ID (or its last part), else the current run.

    On top, the run's status, budgets, latest accepted metrics, landing state
    and one row per iteration; below, the live output of the agent, reviewer
    or summary agent. Keeps waiting when the run stops; Ctrl-C stops watching
    (not the run). Safe to run alongside `autohelix run`; it only reads
    .autohelix/.

    --once prints the table and exits; --json prints it as JSON. --web serves
    a live page on 127.0.0.1 instead (the same server lists all runs at /).
    Over SSH: ssh -L 8765:127.0.0.1:8765 host, then open http://localhost:8765.
    """
    mode = _mode(as_json, once, web, port, open_browser)
    run_id = _run_id(path, run_id)
    if mode == "live":
        _view_live(path.resolve(), run_id, config_file)
        return
    try:
        selected = RunPaths(path, run_id).run_id
    except RuntimeError as error:
        raise click.ClickException(str(error)) from error
    if mode == "web":
        _serve(path, port, open_browser, f"/run/{selected}/" if selected else "/",
               config_file=config_file, run_id=run_id)
        return
    from autohelix.viewer import ProjectRuns

    runs = ProjectRuns(path)
    if not selected or not runs.exists(selected):
        raise click.ClickException(f"No run {run_id!r} in {path.resolve()}" if run_id else
                                   "No current run; start one with `autohelix run` or name one.")
    try:
        data = runs.run(selected, config_file)
    except RuntimeError as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(data, indent=2) if as_json else run_table.format_run(data), nl=as_json)


def _view_live(project_path: Path, run_id: str | None, config_file: Path | None) -> None:
    from rich.console import Console

    from autohelix.live import view as live_view
    from autohelix.run_status import read_run_status

    console = Console()
    if _parallel_marker_path(project_path).exists() and read_run_status(project_path) is None:
        _warn_if_parallel(project_path, console)  # lists the workers to view instead
        raise SystemExit(1)
    if not (project_path / ".autohelix").is_dir():
        console.print(f"[red]No .autohelix/ in {project_path}.[/red] Run this in an AutoHelix project.")
        raise SystemExit(1)
    try:
        config, _ = load_config(project_path, config_file=config_file or read_stored_config_path(project_path, run_id))
        backend = config.agent.type
    except Exception:  # noqa: BLE001 - the log parser can tell backends apart on its own
        backend = None
    try:
        live_view(project_path, backend, console, run_id=run_id, config_file=config_file)
    except KeyboardInterrupt:
        console.print("[dim]Stopped watching. The run is unaffected.[/dim]")


@main.command()
@_mode_options
@click.option("--metric", help="The table's metric column (default: the most recent run's first).")
def dashboard(path: Path, once: bool, as_json: bool, web: bool, port: int | None, open_browser: bool,
              metric: str | None):
    """This project's runs, live in the terminal.

    One row per run with its status, iterations of the budget, latest
    accepted metrics, landing state and last activity; archived runs are
    counted, not listed. Redrawn every 5 seconds until Ctrl-C.

    --once prints the table and exits; --json prints every run (archived ones
    too) as JSON. --web serves a live page on 127.0.0.1 instead; each run
    links to its run page. Over SSH: ssh -L 8765:127.0.0.1:8765 host, then
    open http://localhost:8765.
    """
    mode = _mode(as_json, once, web, port, open_browser)
    if metric and mode in ("json", "web"):
        raise click.UsageError("--metric applies to the table (live or --once); the web page has a menu.")
    if mode == "web":
        _serve(path, port, open_browser, "/")
        return
    from autohelix.viewer import ProjectRuns

    data = ProjectRuns(path).runs()
    if mode == "json":
        click.echo(json.dumps(data, indent=2))
        return
    names = run_table.metric_names([r for r in data["runs"] if not r.get("archived")])
    if metric and metric not in names:
        raise click.ClickException(f"No run has metric {metric!r}" + (f"; known: {', '.join(names)}" if names else ""))
    if mode == "once":
        click.echo(run_table.format_runs(data, metric=metric), nl=False)
        return
    from autohelix.live import dashboard as live_dashboard

    try:
        live_dashboard(path, metric=metric)
    except KeyboardInterrupt:
        pass


def _serve(project: Path, port: int | None, open_browser: bool, page: str, **options) -> None:
    from autohelix.viewer import make_server

    port = 8765 if port is None else port
    try:
        server = make_server(project, port, **options)
    except OSError as exc:
        raise click.ClickException(f"Cannot listen on port {port}: {exc}") from exc
    url = f"http://127.0.0.1:{server.server_port}{page}"
    click.echo(f"Viewing {project.resolve().name} at {url}  (Ctrl-C to stop)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


@main.command()
@click.argument("run_id", required=False)
@click.option("--path", "-p", type=click.Path(exists=True), default=".")
@click.option("--config", "-c", "config_file", type=click.Path(exists=True), help="Path to config file (default: the run's own)")
def report(run_id: str | None, path: str, config_file: str | None):
    """Write (or rewrite) a run's summary (RUN_ID or its last part, default the current run).

    One agent call reads the run's history, notes, reviews, logs and commits, and writes
    the selected run's output/report.md. `run` and `resume` do this automatically when a run
    finishes; use this after an interrupted run or to regenerate it.
    """
    from rich.console import Console

    from autohelix.report import write_report

    console = Console()
    project_path = Path(path).resolve()
    run_id = _run_id(project_path, run_id)
    resolved_config = Path(config_file).resolve() if config_file else read_stored_config_path(project_path, run_id)
    try:
        config, _ = load_config(project_path, config_file=resolved_config)
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)

    if not RunPaths(project_path, run_id).run_id or History(project_path, run_id).get_last_iteration() == 0:
        _warn_if_parallel(project_path, console)
        console.print("[yellow]No iterations run yet. Nothing to report.[/yellow]")
        raise SystemExit(1)

    if not write_report(config, project_path, console, resolved_config, run_id=run_id):
        raise SystemExit(1)
    from autohelix.viewer import write_static

    write_static(project_path, resolved_config, run_id=run_id)
    console.print(f"[dim]Run page saved to {RunPaths(project_path, run_id).output / 'report.html'}[/dim]")


if __name__ == "__main__":
    main()
