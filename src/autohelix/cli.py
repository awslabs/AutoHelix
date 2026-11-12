# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Command-line interface."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from importlib.resources import files as pkg_files
from pathlib import Path

import click
from click.core import ParameterSource

from autohelix.config import (CONFIG_DIR, CONFIG_FILE, ConfigError, ConfigNotFound, find_config, load_config,
                              project_configs, run_command)
from autohelix.harness import Harness
from autohelix.history import History
from autohelix.runs import (NoRunToResume, RunPaths, RunLock, archive as archive_run, check_group,
                            config_gone, delete as delete_run, land as land_run, project_runs, resolve_run,
                            run_tip, short_id, valid_name)
from autohelix import run_table
from autohelix.skill import SKILL_NAME, skill
from autohelix.state import read_stored_config_path
from autohelix.workspace import SCRUBBED_GIT_ENV


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
    # Launched from a git hook or `rebase -x`, inherited GIT_DIR and friends would send every
    # git call (ours, and those of the agents and commands we start) to another repository.
    for key in SCRUBBED_GIT_ENV:
        os.environ.pop(key, None)


main.add_command(skill)


CONFIG_TEMPLATE = (
    pkg_files("autohelix.templates").joinpath("config_template.yaml").read_text()
)

# The agents `setup` offers, in the order it looks for them: executable -> name.
SETUP_AGENTS = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}


def _found_agents() -> list[str]:
    return [agent for agent in SETUP_AGENTS if shutil.which(agent)]


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _skill_dir() -> Path:
    """The bundled setup skill's folder (installs are unpacked, so it is on disk)."""
    bundled = pkg_files("autohelix") / "skills" / SKILL_NAME
    if not (isinstance(bundled, Path) and (bundled / "SKILL.md").is_file()):
        raise click.ClickException(f"The bundled setup skill isn't a folder on disk ({bundled}); reinstall "
                                   "AutoHelix, or `autohelix skill install` it and ask your agent to use it.")
    return bundled.resolve()


def _launcher() -> Path | None:
    """The `autohelix` executable this process was started as, else the one on PATH."""
    started = Path(sys.argv[0])
    if started.name == "autohelix" and started.is_file():
        return started.resolve()
    found = shutil.which("autohelix")
    return Path(found).resolve() if found else None


def _agent_argv(agent: str, project: Path, target: str, exists: bool) -> list[str]:
    """The interactive session `setup` starts: the agent's own CLI with a first prompt
    that points at the bundled skill (nothing is installed or written). The prompt names
    AutoHelix's own Python and launcher, which the agent's shell may not find (pipx,
    `uv tool`, a venv that isn't activated); its PATH is left alone for the project's
    own commands."""
    skill_dir = _skill_dir()
    where = (f"The config is `{target}`, already there (perhaps still the commented template): complete it there"
             if exists else f"Write the config to `{target}`")
    launcher = _launcher()
    tools = f"use AutoHelix's own Python, `{sys.executable}`" + (f" (its CLI is `{launcher}`)" if launcher else "")
    prompt = (f"Read `{skill_dir / 'SKILL.md'}` and follow it to help me set up an AutoHelix loop for this "
              f"project. {where}, with `agent.backend: {agent}` unless we decide otherwise. Write the file "
              f"directly (don't run `autohelix setup`). To check it, {tools}. "
              "Start by asking what I want to achieve.")
    if agent == "claude":  # --add-dir lets it read the skill; it takes several folders, hence --
        return ["claude", "--add-dir", str(skill_dir), "--", prompt]
    if agent == "codex":  # reads outside the workspace without a flag (its --add-dir grants writes)
        return ["codex", prompt]
    return ["opencode", str(project), "--prompt", prompt]


def _git_problem(project: Path) -> str | None:
    """Why `run` refuses this folder (not a git checkout with a commit to start from), or
    None: `run` raises it, `setup` (which needs no git) warns with it. (`main` has dropped
    inherited GIT_* variables, so this is the checkout's own repository.)"""
    def git(*args: str):
        return subprocess.run(["git", *args], cwd=project, capture_output=True, text=True)

    try:
        inside = git("rev-parse", "--is-inside-work-tree")
    except OSError as e:
        return f"git not found ({e}); AutoHelix requires git."
    if inside.returncode:
        if "not a git repository" not in inside.stderr.lower():  # e.g. dubious ownership (safe.directory)
            return f"git failed in {project}:\n{inside.stderr.strip()}"
        return "Not a git repository. AutoHelix requires git.\nRun: git init && git add -A && git commit -m 'init'"
    if inside.stdout.strip() != "true":
        return (f"{project} is not in a git working tree (a bare repository, or inside .git/); "
                "run AutoHelix in a checkout.")
    if git("rev-parse", "--verify", "-q", "HEAD").returncode:
        if git("for-each-ref", "--count=1", "refs/heads/").stdout.strip():
            return ("HEAD has no commit yet (a new or orphan branch); a run starts from a commit. "
                    "Commit on it, or switch to a branch that has one.")
        return "No commits yet; a run starts from a commit.\nRun: git add -A && git commit -m 'init'"
    return None


def _kind(path: Path) -> str:
    return ("a file" if path.is_file() else "a folder" if path.is_dir() else
            "a broken link" if path.is_symlink() else "a special file")


@click.command(short_help="Write a loop's config, with an agent or from the template.")
@click.argument("name", required=False)
@click.option("--path", "-p", type=click.Path(exists=True, file_okay=False), default=".",
              help="Project directory (default: current dir)")
@click.option("--agent", type=click.Choice(list(SETUP_AGENTS)),
              help="Draft the config with this agent, without asking.")
def setup(name: str | None, path: str, agent: str | None):
    """Write a loop's config: autohelix.yaml, or autohelix/NAME.yaml for NAME.

    In a terminal, offers to draft it with a coding agent (Claude Code, Codex or
    OpenCode; it asks which when several are installed) in a session that follows the
    bundled setup skill; otherwise writes the commented template. `autohelix run [NAME]`
    then starts runs of the loop.

    setup itself never overwrites a file: with --agent on an existing config, the agent
    edits it with you.
    """
    if name is not None:
        if name == CONFIG_DIR:
            why = f"is reserved: runs of {CONFIG_DIR}/{CONFIG_DIR}.yaml would get no name; choose another."
        elif (name and Path(name).is_dir()  # `init PATH`, by habit; with -p, NAME is a name
              and click.get_current_context().get_parameter_source("path") == ParameterSource.DEFAULT):
            why = f"is a folder; to set up there, use `-p {shlex.quote(name)}`."
        elif not valid_name(name):
            why = ("must start with a lowercase letter or digit and use only lowercase letters, digits or "
                   "hyphens, at most 32 characters (runs of this config are named after it).")
        else:
            why = None
        if why:
            raise click.BadParameter(f"{name!r} {why}", param_hint="NAME")
    project = Path(path).resolve()
    cwd = Path.cwd().resolve()
    relative = f"{CONFIG_DIR}/{name}.yaml" if name else CONFIG_FILE
    target = project / relative
    shown = str(target.relative_to(cwd) if target.is_relative_to(cwd) else target)  # openable from here
    if name and (target.parent.exists() or target.parent.is_symlink()) and not target.parent.is_dir():
        raise click.ClickException(f"{shown.removesuffix('/' + target.name)} is {_kind(target.parent)}, not a "
                                   "folder of configs; move it away first.")
    exists = target.exists() or target.is_symlink()
    if exists and not target.is_file():
        raise click.ClickException(f"{shown} is {_kind(target)}, not a config file; move it away first.")
    found = _found_agents()
    named = f" {name}" if name else ""
    elsewhere = "" if project == cwd else f" -p {shlex.quote(path)}"
    if exists and agent is None:
        hint = f", or complete it with an agent: `autohelix setup{named}{elsewhere} --agent {found[0]}`" if found else ""
        raise click.ClickException(f"{shown} already exists; setup never overwrites a config. Edit it{hint}.")
    if name is None and not exists and (others := project_configs(project)):
        raise click.ClickException(f"{CONFIG_DIR}/ already has {', '.join(Path(c).name for c in others)}, which "
                                   f"{CONFIG_FILE} would shadow for a bare `autohelix run`. "
                                   f"Name this one: `autohelix setup <name>{elsewhere}`.")
    git_warning = _git_problem(project)

    def warn_git():
        if git_warning:
            click.echo(click.style(f"Warning: {git_warning}", fg="yellow"), err=True)

    if agent is not None:
        if agent not in found:
            raise click.ClickException(f"--agent {agent}: {SETUP_AGENTS[agent]} (`{agent}`) is not on PATH."
                                       + (f" Found: {', '.join(found)}." if found else ""))
    elif found and _interactive():
        from autohelix.menu import choose

        click.echo(click.style("AutoHelix setup", bold=True) + click.style(
            f" · {relative}" + (f" in {project}" if elsewhere else ""), dim=True) + "\n")
        drafts = "it asks what you want to achieve, then writes the config"
        with_agent = (f"With {SETUP_AGENTS[found[0]]}", drafts) if len(found) == 1 else ("With an agent", drafts)
        if choose("How do you want to write it?",
                  [with_agent, ("Empty config", "the commented template, to edit by hand")]) == 0:
            agent = found[0] if len(found) == 1 else found[
                choose("Which agent?", [(SETUP_AGENTS[a], "") for a in found])]

    if agent is not None:
        argv = _agent_argv(agent, project, relative, exists)
        warn_git()
        click.echo(f"Starting {SETUP_AGENTS[agent]} with the AutoHelix setup skill…")
        sys.stdout.flush()
        sys.stderr.flush()
        os.chdir(project)
        try:
            os.execvp(argv[0], argv)  # the agent's session replaces this process
        except OSError as e:
            raise click.ClickException(f"Could not start {agent}: {e}") from e
        return

    try:
        target.parent.mkdir(exist_ok=True)
        with open(target, "x") as f:
            f.write(CONFIG_TEMPLATE)
    except OSError as e:
        raise click.ClickException(f"Could not write {shown}: {e}") from e
    click.echo(f"Wrote {shown}.")
    warn_git()
    click.echo("Next:\n"
               "  1. Edit it: the goal (its first line is the run's title), scope, gates, observe.\n"
               "  2. Commit it, and any scripts its commands use (runs start from a commit).\n"
               f"  3. autohelix run{named}{elsewhere}")
    if found:
        click.echo(f"An agent can draft the gates and observe entries with you: "
                   f"`autohelix setup{named}{elsewhere} --agent {found[0]}`.\n"
                   f"For your usual agent sessions: `autohelix skill install --agent {found[0]}`.")
    else:
        click.echo("No coding agent found (claude, codex or opencode on PATH).\n"
                   f"With one, `autohelix setup{named}{elsewhere} --agent <agent>` drafts the gates and "
                   "observe entries with you.")


main.add_command(setup)
# `init` by reflex: the same command, not listed.
main.add_command(click.Command("init", callback=setup.callback, params=setup.params, help=setup.help,
                               hidden=True))


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
    """The project directory, refused before anything (.autohelix/ included) is
    created there unless it is in a git checkout with a commit to start from."""
    project = Path(path).resolve()
    if problem := _git_problem(project):
        raise click.ClickException(problem)
    return project


def _ancestor_configs(project: Path) -> str:
    """For a project without a config: the nearest folder above it, up to the top of
    its repository, that has one (`run` from src/ by mistake)."""
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=project, capture_output=True, text=True)
    if top.returncode:
        return ""
    top_path = Path(top.stdout.strip()).resolve()
    for folder in project.parents:
        if not folder.is_relative_to(top_path):
            break
        if configs := project_configs(folder):
            return f" {folder} has {', '.join(configs)}; run from there or pass -p {folder}."
    return ""


def _segment_config(project: Path, name: str | None, command: str) -> Path:
    """The config a segment uses (`find_config`), its errors with this command's advice
    (`resume`'s only when there are runs to name; else, as `run`'s, how to start one)."""
    resume = command == "resume" and bool(project_runs(project))
    pick = "Name a run: `autohelix resume <id>`." if resume else "Pick one: `autohelix run <name>`."
    try:
        config = find_config(project, name)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="CONFIG") from e
    except ConfigNotFound as e:
        if name is None:
            write = " Write one with `autohelix setup`."
        else:  # `setup` takes run names only
            write = f" Write it with `autohelix setup {name}`." if valid_name(name) else ""
        if e.configs:
            advice = " " + pick if name is None else write
        else:
            advice = _ancestor_configs(project) or (" " + pick if resume else write)
        raise click.ClickException(str(e) + advice) from e
    others = [Path(c).stem for c in project_configs(project) if c != CONFIG_FILE]
    if name is None and config == project / CONFIG_FILE and others:
        example = "resume their runs by id" if resume else f"`autohelix run {others[0]}`"
        click.echo(click.style(f"Using {CONFIG_FILE}; {CONFIG_DIR}/ also has: {', '.join(others)} ({example})",
                               dim=True))
    return config


def _start(project: Path, config: Path | None, **selection) -> Harness:
    """The harness for a segment. There is no setup step: the first run creates
    the local git excludes and .autohelix/, once nothing is left to refuse."""
    try:
        return Harness(project, config_file=config, **selection)
    except NoRunToResume as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(3)
    except (RuntimeError, FileNotFoundError, ConfigError) as e:  # FileNotFoundError: a run's stored config is gone
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
@click.option("--group", help="Share notes and code with the project's other runs of this group "
                              "(landing defaults to manual).")
def run(config_name: str | None, path: str, verbose: bool, no_report: bool, land_mode: str | None,
        iterations: int | None, group: str | None):
    """Start a new run of the optimization loop.

    Always a new run, with its own budget, notes and history; to continue one,
    use `autohelix resume`. CONFIG names a config in autohelix/ (`perf` for
    autohelix/perf.yaml); without it, the run reads autohelix.yaml, else the
    only config in autohelix/.

    With --group NAME, each iteration's agent also sees the other non-archived
    runs of that group: their notes and history, and their code (each run's
    ref, refs/autohelix/<id>). Start several, compare them on the dashboard, land the
    winner and archive the rest.
    """
    group = _group(group)
    project = _git_project(path)
    config = _segment_config(project, config_name, "run")
    harness = _start(project, config, verbose=verbose, new=True, land_mode=land_mode,
                     group=group or None)
    _segment(harness, iterations, not no_report)


@main.command()
@click.argument("run_id", required=False)
@_segment_options
@click.option("--iterations", "-n", type=click.IntRange(min=1), help="How many more iterations to run.")
@click.option("--until", type=click.IntRange(min=1), help="Run up to iteration N (repeatable; for scripts).")
@click.option("--group", help="Set or change the run's group from its next iteration; '' clears it.")
def resume(run_id: str | None, path: str, verbose: bool, no_report: bool, land_mode: str | None, iterations: int | None, until: int | None, group: str | None):
    """Continue a run: RUN_ID (or its last part), else the latest run of the config a bare `run` would use.

    A run uses the config it started with; without RUN_ID, the config is the
    one a bare `autohelix run` reads, and its latest is the most recently
    active run for that config that isn't
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
    config = None
    if not selected:
        config = _segment_config(project, None, "resume")
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
def view(run_id: str | None, path: Path, once: bool, as_json: bool, web: bool, port: int | None,
         open_browser: bool):
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
        _view_live(path.resolve(), run_id)
        return
    try:
        selected = RunPaths(path, run_id).run_id
    except RuntimeError as error:
        raise click.ClickException(str(error)) from error
    if mode == "web":
        _serve(path, port, open_browser, f"/run/{selected}/" if selected else "/", run_id=run_id)
        return
    from autohelix.viewer import ProjectRuns

    runs = ProjectRuns(path)
    if not selected or not runs.exists(selected):
        raise click.ClickException(f"No run {run_id!r} in {path.resolve()}" if run_id else
                                   f"No current run; name one, or {_how_to_start(path.resolve())}")
    try:
        data = runs.run(selected)
    except RuntimeError as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(data, indent=2) if as_json else run_table.format_run(data), nl=as_json)


def _how_to_start(project: Path) -> str:
    """How to start a first run here, by the configs there are (lowercase, a sentence end)."""
    configs = project_configs(project)
    if not configs:
        return "write a config with `autohelix setup`, then `autohelix run`."
    if len(configs) == 1:
        return f"start one with `{run_command(configs[0])}`."
    return f"start one with `autohelix run [CONFIG]` (configs: {', '.join(configs)})."


def _view_live(project_path: Path, run_id: str | None) -> None:
    from rich.console import Console
    from rich.markup import escape

    from autohelix.live import view as live_view

    console = Console()
    if not (project_path / ".autohelix").is_dir():
        how = _how_to_start(project_path)
        console.print(f"[red]No runs in {escape(str(project_path))}.[/red] {escape(how[0].upper() + how[1:])}",
                      highlight=False)
        raise SystemExit(1)
    try:
        # The run's own config, even when its file is gone (then none); the lookup only without a run.
        config, _ = load_config(project_path, config_file=read_stored_config_path(project_path, run_id))
        backend = config.agent.backend
    except Exception:  # noqa: BLE001 - the log parser can tell backends apart on its own
        backend = None
    try:
        live_view(project_path, backend, console, run_id=run_id)
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
def report(run_id: str | None, path: str):
    """Write (or rewrite) a run's summary (RUN_ID or its last part, default the current run).

    One agent call reads the run's history, notes, reviews, logs and commits, and writes
    the selected run's output/report.md. `run` and `resume` do this automatically when a run
    finishes; use this after an interrupted run or to regenerate it. It reads the config
    the run started with.
    """
    from rich.console import Console

    from autohelix.report import write_report

    console = Console()
    project_path = Path(path).resolve()
    run_id = _run_id(project_path, run_id)
    paths = RunPaths(project_path, run_id)
    if not paths.run_id or History(project_path, run_id).get_last_iteration() == 0:
        console.print("[yellow]No iterations run yet. Nothing to report.[/yellow]")
        raise SystemExit(1)
    config_path = read_stored_config_path(project_path, run_id)
    try:
        config, _ = load_config(project_path, config_file=config_path)
    except FileNotFoundError:
        data = paths.read_identity()
        console.print(f"[red]Error:[/red] {config_gone(project_path, paths.run_id, data['config_path'], data.get('base_commit'))}",
                      highlight=False)
        raise SystemExit(1)
    except ConfigError as e:
        console.print(f"[red]Error:[/red] {e}", highlight=False)
        raise SystemExit(1)

    if not write_report(config, project_path, console, config_path, run_id=run_id):
        raise SystemExit(1)
    from autohelix.viewer import write_static

    write_static(project_path, run_id=run_id)
    console.print(f"[dim]Run page: {(paths.output / 'report.html').relative_to(project_path)}[/dim]", highlight=False)


if __name__ == "__main__":
    main()
