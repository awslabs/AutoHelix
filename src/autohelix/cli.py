# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Command-line interface."""

import json
import os
import subprocess
from importlib.resources import files as pkg_files
from pathlib import Path

import click

from autohelix.config import ConfigNotFound, find_config, load_config
from autohelix.harness import Harness
from autohelix.history import History
from autohelix.runs import (NoRunToResume, RunPaths, RunLock, archive as archive_run, check_group,
                            delete as delete_run, land as land_run, resolve_run, run_tip, short_id)
from autohelix import run_table
from autohelix.skill import skill
from autohelix.state import (
    GitExcludeError,
    ensure_git_exclude_entry,
    ensure_runtime_ignored,
    read_stored_config_path,
)


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

    # The agent prompt is built in (a config can replace it with `prompt_template:`).
    autohelix_dir.mkdir()

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
                     help="Config file (default: autohelix.yaml, else the only autohelix/*.yaml; "
                          "for resume RUN_ID, the run's own)"),
        click.option("--verbose", is_flag=True, help="Show agent command details and heartbeat messages"),
        click.option("--no-report", is_flag=True, help="Don't write the run summary when the run finishes"),
        click.option("--land", "land_mode", type=click.Choice(["auto", "manual"]),
                     help="Fast-forward the target branch at the end of each segment (auto) or leave it to "
                          "`autohelix land` (manual). Stored with the run; a new run's default is the config's "
                          "land:, else auto (manual with --group)."),
    ]):
        command = option(command)
    return command


def _group(value: str | None) -> str | None:
    try:
        return None if value is None else check_group(value)
    except RuntimeError as error:
        raise click.BadParameter(str(error), param_hint="--group") from error


def _git_project(path: str) -> Path:
    """The project directory, refused outside git before anything (.autohelix/
    included) is created there."""
    project = Path(path).resolve()
    try:
        inside = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=project, capture_output=True).returncode == 0
    except OSError:  # no git at all
        inside = False
    if not inside:
        raise click.ClickException("Not a git repository. AutoHelix requires git.\n"
                                   "Run: git init && git add -A && git commit -m 'init'")
    return project


def _start(project: Path, config: Path | None, **selection) -> Harness:
    """The harness for a segment. There is no setup step: the first run creates
    .autohelix/ and the local git excludes."""
    # Refuse before the harness creates logs, settings, or agent state.
    try:
        ensure_runtime_ignored(project)
    except GitExcludeError as e:
        raise click.ClickException(str(e)) from e
    try:  # best effort: only runtime state must be ignored
        ensure_git_exclude_entry(project, "__pycache__/")
    except (OSError, subprocess.CalledProcessError):
        pass
    try:
        return Harness(project, config_file=config, **selection)
    except NoRunToResume as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(3)
    except (RuntimeError, FileNotFoundError) as e:  # FileNotFoundError: a run's stored config is gone
        raise click.ClickException(str(e)) from e


def _segment(harness: Harness, iterations: int | None, report: bool) -> None:
    try:
        harness.run(max_iterations=iterations, report=report)
    except RuntimeError as e:
        if getattr(e, "autohelix_reported", False):  # printed first, above landing and the run page
            raise SystemExit(1) from e
        raise click.ClickException(str(e)) from e


@main.command()
@click.argument("config_name", metavar="[CONFIG]", required=False)
@_segment_options
@click.option("--iterations", "-n", type=click.IntRange(min=1), help="Iterations to run (default: budget.iterations)")
@click.option("--name", help="Readable part of the new run's id (default: the config's name:, else its file name).")
@click.option("--group", help="Share notes and code with the project's other runs of this group "
                              "(landing defaults to manual).")
def run(config_name: str | None, path: str, config_file: str | None, verbose: bool, no_report: bool,
        land_mode: str | None, iterations: int | None, name: str | None, group: str | None):
    """Start a new run of the optimization loop.

    Always a new run, with its own budget, notes and history; to continue one,
    use `autohelix resume`. CONFIG names a config in autohelix/ (`perf` for
    autohelix/perf.yaml); without it (or --config), the run reads
    autohelix.yaml, else the only config in autohelix/.

    With --group NAME, each iteration's agent also sees the other non-archived
    runs of that group: their notes and history, and their code on
    autohelix/<id>. Start several, compare them on the dashboard, land the
    winner and archive the rest.
    """
    if config_name and config_file:
        raise click.UsageError("Give a config name or --config, not both.")
    group = _group(group)
    project = _git_project(path)
    try:
        config = Path(config_file).resolve() if config_file else find_config(project, config_name)
    except ConfigNotFound as e:
        raise click.ClickException(str(e)) from e
    harness = _start(project, config, verbose=verbose, new=True, name=name, land_mode=land_mode,
                     group=group or None)
    _segment(harness, iterations, not no_report)


@main.command()
@click.argument("run_id", required=False)
@_segment_options
@click.option("--iterations", "-n", type=click.IntRange(min=1), help="How many more iterations to run.")
@click.option("--until", type=click.IntRange(min=1), help="Run up to iteration N (repeatable; for scripts).")
@click.option("--group", help="Set or change the run's group from its next iteration; '' clears it.")
def resume(run_id: str | None, path: str, config_file: str | None, verbose: bool, no_report: bool,
           land_mode: str | None, iterations: int | None, until: int | None, group: str | None):
    """Continue a run: RUN_ID (or its last part), else this config's latest.

    The config is --config, else the one a bare `autohelix run` reads. Its
    latest is the most recently active run for the config that isn't
    archived, preferring runs that captured their baseline. Without -n or --until it runs to
    budget.iterations; cost and time budgets stay run totals from the config.
    The run keeps its landing mode and group unless --land or --group changes
    them. Exits 3 when there is no run to resume.
    """
    if iterations and until:
        raise click.UsageError("Use either -n (more iterations) or --until (up to an iteration), not both.")
    group = _group(group)
    project = _git_project(path)
    selected = _run_id(project, run_id)
    config = Path(config_file).resolve() if config_file else None
    if config is None and selected is None:
        try:
            config = find_config(project)
        except ConfigNotFound as e:
            found = f"Several configs: {', '.join(e.configs)}." if e.configs else "No config found."
            raise click.ClickException(f"{found} Name a run (`autohelix resume <id>`) "
                                       "or a config (-c PATH).") from e
    harness = _start(project, config, verbose=verbose, run_id=selected, create=False, land_mode=land_mode,
                     group=group)
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
    """Delete runs: their history, notes, logs, ref, kept rejected trees and leftover copies.

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


@main.command()
@click.argument("question")
@click.option("--yes", "yes_means", help='What a "yes" answer means.')
@click.option("--no", "no_means", help='What a "no" answer means.')
@click.option("--rubric", type=click.Path(exists=True, dir_okay=False), help="File with criteria for the answer.")
@click.option("--agent", "agent_type", type=click.Choice(["claude", "codex", "mock"]),
              help="Agent backend (default: agent.backend from the config, else claude).")
@click.option("--model", help="Model (default: agent.model from the config).")
@click.option("--effort", help="Reasoning effort (default: agent.reasoning_effort from the config).")
@click.option("--timeout", type=click.IntRange(min=1), help="Seconds before giving up (default 600).")
@click.option("--command", "agent_command",
              help="The backend's executable (default: agent.command from the config, unless --agent "
                   "names another backend).")
def ask(question: str, yes_means: str | None, no_means: str | None, rubric: str | None,
        agent_type: str | None, model: str | None, effort: str | None, timeout: int | None,
        agent_command: str | None):
    """Ask an agent a yes/no question about the current directory.

    Exits 0 for yes, 1 for no, 2 if no valid answer was produced; prints the
    reason. Use it in gates: (or as an `ask:` entry there), stop:, or any script. The agent gets
    read-only tools and no project settings; agent settings come from
    $AUTOHELIX_CONFIG, else the config a bare `autohelix run` here would read
    (if there is exactly one), else the defaults.
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
                              timeout=timeout, command=agent_command)
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


def _review_metrics(ctx, param, values: tuple[str, ...]) -> list[str]:
    import re

    names: list[str] = []
    for value in values:
        name, _, direction = value.rpartition(":")
        if not re.fullmatch(r"\w+", name) or direction not in ("higher", "lower"):
            raise click.BadParameter(f"{value!r}: expected NAME:higher or NAME:lower")
        if name in names:
            raise click.BadParameter(f"{name!r} is given twice")
        names.append(name)
    return names


@main.command()
@click.argument("prompt")
@click.option("--previous", is_flag=True,
              help="Include this entry's review from the last accepted iteration (in a run).")
@click.option("--metric", "metrics", multiple=True, metavar="NAME:higher|lower", callback=_review_metrics,
              help="A number the review must report, printed as ##autohelix[NAME=value]. Repeatable.")
@click.option("--agent", "agent_type", type=click.Choice(["claude", "codex", "mock"]),
              help="Agent backend (default: agent.backend from the config, else claude).")
@click.option("--model", help="Model (default: agent.model from the config).")
@click.option("--effort", help="Reasoning effort (default: agent.reasoning_effort from the config).")
@click.option("--timeout", type=click.IntRange(min=1),
              help="Seconds before giving up (default: the command's timeout in a run, else 1800).")
@click.option("--command", "agent_command",
              help="The backend's executable (default: agent.command from the config, unless --agent "
                   "names another backend).")
@click.option("--base", help="Outside a run: the commit the working tree is compared with (default HEAD).")
def review(prompt: str, previous: bool, metrics: list[str], agent_type: str | None, model: str | None,
           effort: str | None, timeout: int | None, agent_command: str | None, base: str | None):
    """Review a change with an agent; prints the review as feedback.

    PROMPT is the review request; put it after `--` (`autohelix review -- "..."`)
    so a prompt starting with `-` isn't read as an option. The agent gets
    read-only tools and no project settings; agent settings come from
    $AUTOHELIX_CONFIG, else the config a bare `autohelix run` here would read
    (if there is exactly one), else the defaults.

    In a run (an `observe:` entry's `review:`), it reviews a fresh copy of the
    candidate. By hand, it reviews the current directory; the change is the
    working tree (new files included) against --base.

    Prints the review, then one ##autohelix[NAME=value] line per --metric.
    Exits 0 on success, 2 if no valid review was produced, a requested number
    is missing, or the reviewer changed files.
    """
    import signal

    from autohelix.formatting import format_cost
    from autohelix.judge import JudgeError, judge_config, load_base_config
    from autohelix.review import DEFAULT_TIMEOUT, review as run_review, review_schema
    from autohelix.usage_log import ENV as USAGE_ENV

    def _terminate(signum, frame):  # the harness timed this command out: stop the agent too
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    cwd = Path.cwd()
    try:
        if not prompt.strip():
            raise JudgeError("the review prompt is empty")
        config = judge_config(load_base_config(cwd), agent=agent_type, model=model, effort=effort,
                              timeout=timeout, command=agent_command, schema=review_schema(metrics),
                              default_timeout=DEFAULT_TIMEOUT)
        result = run_review(prompt, cwd, config, metrics=metrics, previous=previous, base=base,
                            timeout=timeout,
                            warn=lambda message: click.echo(f"autohelix review: {message}", err=True))
    except JudgeError as e:
        click.echo(f"autohelix review: no valid review: {e}", err=True)
        raise SystemExit(2)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as e:  # noqa: BLE001 - a failed review is never a success
        click.echo(f"autohelix review: failed: {type(e).__name__}: {e}", err=True)
        raise SystemExit(2)
    click.echo(result.output())
    if not os.environ.get(USAGE_ENV):
        click.echo(f"autohelix review: ok · {format_cost(result.cost_usd, precision=4)}", err=True)


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
              help="Config file (default: the run's own, else the project's)")
def view(run_id: str | None, path: Path, once: bool, as_json: bool, web: bool, port: int | None,
         open_browser: bool, config_file: Path | None):
    """One run, live in the terminal: RUN_ID (or its last part), else the current run.

    On top, the run's status, budgets, latest accepted metrics, landing state
    and one row per iteration; below, the live output of the agent, a review
    or the summary agent. Keeps waiting when the run stops; Ctrl-C stops watching
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

    console = Console()
    if not (project_path / ".autohelix").is_dir():
        console.print(f"[red]No runs in {project_path}.[/red] Start one with `autohelix run`.")
        raise SystemExit(1)
    try:
        config, _ = load_config(project_path, config_file=config_file or read_stored_config_path(project_path, run_id))
        backend = config.agent.backend
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
    try:
        resolved_config = (Path(config_file).resolve() if config_file else
                           read_stored_config_path(project_path, run_id) or find_config(project_path))
        config, _ = load_config(project_path, config_file=resolved_config)
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)

    if not RunPaths(project_path, run_id).run_id or History(project_path, run_id).get_last_iteration() == 0:
        console.print("[yellow]No iterations run yet. Nothing to report.[/yellow]")
        raise SystemExit(1)

    if not write_report(config, project_path, console, resolved_config, run_id=run_id):
        raise SystemExit(1)
    from autohelix.viewer import write_static

    write_static(project_path, resolved_config, run_id=run_id)
    console.print(f"[dim]Run page saved to {RunPaths(project_path, run_id).output / 'report.html'}[/dim]")


if __name__ == "__main__":
    main()
