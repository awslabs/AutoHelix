# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Command-line interface."""

import functools
import json
import os
import shlex
import shutil
import subprocess
import sys
from importlib.resources import files as pkg_files
from pathlib import Path

import click

from autohelix.config import (CONFIG_DIR, CONFIG_FILE, ConfigError, ConfigNotFound, find_config,
                              project_configs, run_command)
from autohelix.agents import CLI_NAMES, JUDGE_CLIS, RENAMED_CLIS, executable
from autohelix.awake import keep_awake
from autohelix.harness import DEFAULT_ITERATIONS, Budget, Harness
from autohelix.history import History
from autohelix.run_status import read_run_status
from autohelix.runs import (RunPaths, planned_run, RunLock, archive as archive_run, check_group, check_why,
                            default_run, delete as delete_run, find_project, is_live,
                            load_run_config, land as land_run, project_runs, resolve_run, resolve_runs,
                            config_relative, group_landing_note, run_tip, set_options, short_id)
from autohelix import run_table
from autohelix.skill import SKILL_NAME, skill
from autohelix.workspace import SCRUBBED_GIT_ENV, run_git


def _get_version() -> str:
    """Get version from package metadata."""
    from autohelix.terminal import version

    return version()


START_HINT = "Run `autohelix` in a terminal to start."


@click.group(invoke_without_command=True)
@click.version_option(version=_get_version(), prog_name="autohelix")
@click.pass_context
def main(ctx: click.Context):
    """AutoHelix - Autonomous code improvement harness.

    Bare `autohelix` in a terminal shows the project's runs and offers to start a
    coding agent session (Claude Code, Codex or OpenCode) with the bundled skill:
    to ask about the runs, or to set up a new experiment.
    """
    # Launched from a git hook or `rebase -x`, inherited GIT_DIR and friends would send every
    # git call (ours, and those of the agents and commands we start) to another repository.
    for key in SCRUBBED_GIT_ENV:
        os.environ.pop(key, None)
    if ctx.invoked_subcommand is not None:
        return
    if not _interactive():  # a script, a pipe, an agent's shell tool: the help, as without a subcommand
        click.echo(ctx.get_help(), err=True)
        click.echo(f"\n{START_HINT}", err=True)
        ctx.exit(2)
    _entry(ctx)


main.add_command(skill)


PROJECT_HELP = "Project directory (default: the current folder, or the nearest one above it with runs)."


def _path_option(help: str = PROJECT_HELP):
    """The one `--path/-p` (a folder, as a Path). Commands that read runs pass it to
    `_project`; `run` takes the folder as given."""
    return click.option("--path", "-p", type=click.Path(exists=True, file_okay=False, path_type=Path),
                        default=".", help=help)


def _shown(path: Path) -> str:
    """A folder as typed from here: relative to the current folder, or absolute when that is shorter."""
    relative = os.path.relpath(path, Path.cwd())
    return relative if len(relative) <= len(str(path)) else str(path)


def _project(path: Path) -> Path:
    """The project a command that reads runs acts on (`runs.find_project`: the nearest
    folder with runs, from `path` up to its checkout's top), named when it isn't `path`."""
    project = find_project(path)
    if project != Path(path).resolve():
        click.echo(click.style(f"Using project {_shown(project)}", dim=True), err=True)
    return project


def _warn(message: str, err: bool = False) -> None:
    """A warning on the command's own screen (`terminal.warning`), on stderr with `err`."""
    from rich.console import Console

    from autohelix import terminal

    terminal.warn(Console(stderr=err), message)


def _chosen_run(project: Path, value: str | None) -> RunPaths:
    """The run a command acts on: `value` (an id or its last part), else `runs.default_run`'s
    (the running run, else the most recently active one that isn't archived), named."""
    if value is not None:
        return RunPaths(project, _run_id(project, value))
    try:
        paths, _, why = default_run(project)
    except RuntimeError as error:
        raise click.ClickException(str(error)) from error
    click.echo(click.style(f"Run {short_id(paths.run_id)} (the {why} run; name one to choose another)", dim=True),
               err=True)
    return paths


TEMPLATE = pkg_files("autohelix.templates") / "config_template.yaml"
EXAMPLES = "https://github.com/awslabs/AutoHelix/tree/main/examples"

# The agents bare `autohelix` offers, in the order it looks for them: `agent.cli` -> name.
# (`mock`, a CLI for tests, is never offered.) Found as the runs find them
# (`$AUTOHELIX_<CLI>_CMD`, else the name on PATH: `agents.executable`).
AGENTS = CLI_NAMES


def _found_agents() -> list[str]:
    return [agent for agent in AGENTS if shutil.which(executable(agent))]


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def docs_dir() -> Path | None:
    """The user docs: autohelix/docs in an installed package (the wheel ships the repo's
    docs/ there), else the checkout's docs/ (an editable install has no copy); None
    without either."""
    packaged = pkg_files("autohelix") / "docs"
    if isinstance(packaged, Path) and packaged.is_dir():
        return packaged.resolve()
    checkout = Path(__file__).resolve().parent.parent.parent / "docs"
    return checkout if (checkout / "concepts.md").is_file() else None


@main.command("docs")
def docs_command():
    """Print the folder of the user docs that came with this AutoHelix (README.md is
    their index). For people and for agents: an installed skill finds them this way."""
    folder = docs_dir()
    if folder is None:
        raise click.ClickException("This AutoHelix has no user docs on disk; they are at "
                                   "https://github.com/awslabs/AutoHelix/tree/main/docs")
    click.echo(folder)


def _skill_dir() -> Path:
    """The bundled AutoHelix skill's folder (installs are unpacked, so it is on disk)."""
    bundled = pkg_files("autohelix") / "skills" / SKILL_NAME
    if not (isinstance(bundled, Path) and (bundled / "SKILL.md").is_file()):
        raise click.ClickException(f"The bundled AutoHelix skill isn't a folder on disk ({bundled}); reinstall "
                                   "AutoHelix, or `autohelix skill install` it and ask your agent to use it.")
    return bundled.resolve()


def _launcher() -> Path | None:
    """The `autohelix` executable this process was started as, else the one on PATH."""
    started = Path(sys.argv[0])
    if started.name == "autohelix" and started.is_file():
        return started.resolve()
    found = shutil.which("autohelix")
    return Path(found).resolve() if found else None


def _agent_argv(agent: str, project: Path, prompt: str) -> list[str]:
    """The interactive session bare `autohelix` starts: the agent's own CLI with the
    first prompt (`first_prompt`: it points at the bundled skill and the docs; nothing is
    installed or written)."""
    command = executable(agent)
    if agent == "claude-code":  # --add-dir lets it read the skill and the docs; it takes several folders, hence --
        docs = docs_dir()
        return [command, "--add-dir", str(_skill_dir()), *([str(docs)] if docs else []), "--", prompt]
    if agent == "codex":  # reads outside the workspace without a flag (its --add-dir grants writes)
        return [command, prompt]
    return [command, str(project), "--prompt", prompt]


def _git_problem(project: Path) -> tuple[str, str | None] | None:
    """Why `run` refuses this folder (not a git checkout with a commit to start from) and
    the git command that fixes it, when there is one; None when a run can start here.
    `run` raises it, bare `autohelix` shows it. (`main` has dropped inherited GIT_*
    variables, so this is the checkout's own repository.)"""
    def git(*args: str):
        return run_git(["git", *args], cwd=project, capture_output=True, text=True)  # English messages

    try:
        inside = git("rev-parse", "--is-inside-work-tree")
    except OSError as e:
        return f"git not found ({e}); AutoHelix requires git.", None
    if inside.returncode:
        if "not a git repository" not in inside.stderr.lower():  # e.g. dubious ownership (safe.directory)
            return f"git failed in {project}:\n{inside.stderr.strip()}", None
        return "Not a git repository. AutoHelix requires git.", "git init && git add -A && git commit -m 'init'"
    if inside.stdout.strip() != "true":
        return (f"{project} is not in a git working tree (a bare repository, or inside .git/); "
                "run AutoHelix in a checkout."), None
    if git("rev-parse", "--verify", "-q", "HEAD").returncode:
        if git("for-each-ref", "--count=1", "refs/heads/").stdout.strip():
            return ("HEAD has no commit yet (a new or orphan branch); a run starts from a commit. "
                    "Commit on it, or switch to a branch that has one."), None
        return "No commits yet; a run starts from a commit.", "git add -A && git commit -m 'init'"
    return None


def _checkout_top(folder: Path) -> Path | None:
    """The top of the git checkout `folder` is in; None outside one (no git, a bare
    repository, inside .git/)."""
    try:
        top = run_git(["git", "rev-parse", "--show-toplevel"], cwd=folder, capture_output=True, text=True)
    except OSError:
        return None
    return Path(top.stdout.strip()).resolve() if top.returncode == 0 and top.stdout.strip() else None


def entry_project(folder: Path) -> Path | None:
    """The project bare `autohelix` shows: the folder `runs.find_project` picks when it
    finds runs (`folder` or the nearest one above it with runs, within the checkout),
    else `folder` or the nearest one above it with a config (where `autohelix run`
    would find one), else the top of the git checkout; None outside a checkout."""
    top = _checkout_top(folder)
    if top is None:
        return None
    found = find_project(folder)
    if RunPaths(found).runs.is_dir():
        return found
    return next((candidate for candidate in (folder, *folder.parents)
                 if candidate.is_relative_to(top) and project_configs(candidate)), top)


def _home(path: Path) -> str:
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) and path != home else str(path)


def _unrun(configs: list[str], runs: list[dict]) -> list[str]:
    """The configs no run was started from."""
    used = {run.get("config_file") for run in runs}
    return [config for config in configs if config not in used]


def entry_question(agent: str) -> str:
    """The entry screen's one question with one agent installed (Enter/q)."""
    return f"Start {AGENTS[agent]} with AutoHelix?"


# With several agents the question is which one (q quits).
ENTRY_TITLE = "Start an agent with AutoHelix:"


def _run_line(run: dict) -> str:
    """A run as the first prompt lists it: id, config, title; its state (while running,
    with the iteration under way and its phase), accepted of finished iterations, cost,
    commits to land."""
    if run.get("error"):
        return f"- {run['id']}: unreadable ({run['error']})"
    config = f", {run['config_file']}" if run.get("config_file") else ""
    title = f' "{run["title"]}"' if run.get("title") else ""
    progress = run["progress"]
    state = run_table._word(run)
    if run_table._live(run):
        under_way = run_table._under_way(run)
        doing = ", ".join(filter(None, [under_way and f"iteration {under_way}", run_table._phase(run)]))
        state += f" ({doing})" if doing else ""
    done = progress["iterations"]
    facts = [state, f"{run.get('accepted') or 0} of {done} iterations accepted" if done else "baseline only"]
    cost = progress.get("cost_usd")
    if isinstance(cost, (int, float)) and cost:
        facts.append(f"${cost:.2f}" + ("+ (some calls unpriced)" if progress.get("cost_incomplete") else ""))
    if (run.get("landing") or {}).get("commits"):
        facts.append(run_table._landing(run))  # `2 to land`, `2 diverged`
    return f"- {short_id(run['id'])} ({run['id']}{config}){title}: {', '.join(facts)}"


def first_prompt(agent: str, project: Path, runs: list[dict], configs: list[str],
                 git: tuple[str, str | None] | None = None) -> str:
    """The session's first prompt: facts only; how the session opens and goes on is the
    skill's (SKILL.md). The bundled skill, AutoHelix's own CLI, Python and docs, any git
    problem, the project's runs and configs, and which agent CLI the session runs in."""
    skill_dir = _skill_dir()
    launcher = _launcher()
    docs = docs_dir()
    current = run_table.current_runs(runs)
    archived = len(runs) - len(current)
    unrun = _unrun(configs, runs)
    python = shlex.quote(sys.executable)
    tools = (f"`autohelix` below means `{shlex.quote(str(launcher))}`, and AutoHelix's Python is `{python}`: "
             "use them rather than whatever `autohelix` or `python` your shell finds." if launcher else
             f"AutoHelix's Python is `{python}`: use it rather than whatever `python` your shell finds.")
    parts = [f"Read `{skill_dir / 'SKILL.md'}` and follow it: you are helping me with AutoHelix in this "
             f"project ({project}).",
             tools + (f" AutoHelix's user docs are in `{docs}`: read them when needed." if docs else "")]
    if git:
        problem, fix = git
        parts.append(f"Git: {problem.removesuffix('.')}" + (f" (`{fix}` fixes it)" if fix else "")
                     + ". `autohelix run` refuses until that is fixed.")
    if current:
        listed = "\n".join(_run_line(run) for run in current)
        parts.append("The project's runs (live ones first, then by last activity; `autohelix view ID --json` and "
                     f"`.autohelix/runs/ID/` have their records):\n{listed}"
                     + (f"\n({archived} archived, not listed: `autohelix view --json` has them.)" if archived else ""))
    elif archived:
        parts.append(f"The project has no current runs, {archived} archived (`autohelix view --json` lists them).")
    else:
        parts.append("The project has no runs yet.")
    if configs:
        parts.append("Its configs: " + ", ".join(f"{c} (no runs yet)" if c in unrun else c for c in configs) + ".")
    else:
        parts.append("It has no AutoHelix config yet.")
    parts.append(f"This session runs in {AGENTS[agent]} (`agent.cli: {agent}`). I started it from AutoHelix's "
                 "start screen" + (", which showed me these runs as a table" if current else "")
                 + "; open as the skill says.")
    return "\n\n".join(parts)


def _interruptible(command):
    """`run`/`resume` and the entry screen: Ctrl-C at any point (the checks before the
    segment, preflight's agent commands, the segment; the screen's questions) exits 130,
    not click's "Aborted!" (1)."""
    @functools.wraps(command)
    def wrapper(*args, **kwargs):
        try:
            return command(*args, **kwargs)
        except KeyboardInterrupt:
            raise SystemExit(130) from None
    return wrapper


@_interruptible
def _entry(ctx: click.Context) -> None:
    """Bare `autohelix` in a terminal: the project's runs and one question; an agent
    session starts only on a keypress (q or Esc quits, Ctrl-C exits 130)."""
    from rich.console import Console

    from autohelix import menu, terminal
    from autohelix.viewer import ProjectRuns

    console = Console()
    folder = Path.cwd().resolve()
    project = entry_project(folder)
    if project is None:  # outside a checkout: nothing to start here
        problem, fix = _git_problem(folder) or (f"{folder} is not in a git checkout.", None)
        console.print()
        terminal.print_lines(console, run_table.outside_lines(_home(folder), problem, fix, console.width))
        ctx.exit(1)
    git = _git_problem(project)
    console.print()
    terminal.print_lines(console, run_table.entry_header(_home(project), console.width, git and (git[0], "yellow")))
    data = ProjectRuns(project).runs()  # can take seconds: the header is up meanwhile
    runs = data["runs"]
    configs = project_configs(project)
    terminal.print_lines(console, run_table.entry_lines(data, console.width))
    try:
        found = _found_agents()
    except ConfigError as e:  # a variable that is gone (`agents.executable`)
        terminal.warn(console, str(e))
        ctx.exit(1)
    if not found:
        terminal.warn(console, "No coding agent found to start: install Claude Code (`claude`), Codex (`codex`) "
                      "or OpenCode (`opencode`). To write a config by hand, start from the packaged template "
                      f"({TEMPLATE}) or getting-started.md" + (f" in {docs_dir()}" if docs_dir() else "") + ".")
        ctx.exit(1)
    console.print()
    try:
        if len(found) == 1:
            menu.confirm(entry_question(found[0]))
            agent = found[0]
        else:
            agent = found[menu.choose(ENTRY_TITLE, [(AGENTS[a], "") for a in found])]
    except menu.Quit:
        ctx.exit(0)
    argv = _agent_argv(agent, project, first_prompt(agent, project, runs, configs, git))
    console.print(f"\n  Starting {AGENTS[agent]} with the AutoHelix skill…", style="dim", highlight=False)
    sys.stdout.flush()
    sys.stderr.flush()
    os.chdir(project)
    try:
        os.execvp(argv[0], argv)  # the agent's session replaces this process
    except OSError as e:
        raise click.ClickException(f"Could not start {argv[0]}: {e}") from e


def _run_id(project: Path, value: str | None) -> str | None:
    """A run id as typed (full, or its last part), checked; None stays None."""
    if value is None:
        return None
    try:
        return resolve_run(project, value)
    except RuntimeError as error:
        raise click.ClickException(str(error)) from error


def _segment_options(command):
    """The options `run` and `resume` share (`--path` aside: they find the project differently)."""
    for option in reversed([
        click.option("--verbose", is_flag=True, help="Show agent command details and heartbeat messages"),
        click.option("--no-report", is_flag=True, help="Don't write the run summary when the run finishes"),
        click.option("--land", "land_mode", type=click.Choice(["auto", "manual"]),
                     help="Fast-forward the target branch at the end of each segment (auto) or leave it to "
                          "`autohelix land` (manual). Stored with the run; a new run's default is auto "
                          "(manual with --group)."),
    ]):
        command = option(command)
    return command


def _group(value: str | None) -> str | None:
    try:
        return None if value is None else check_group(value)
    except RuntimeError as error:
        raise click.BadParameter(str(error), param_hint="--group") from error


def _why(value: str | None) -> str | None:
    try:
        return None if value is None else check_why(value)
    except RuntimeError as error:
        raise click.BadParameter(str(error), param_hint="--why") from error


def _git_project(path: Path) -> Path:
    """The project directory, refused before anything (.autohelix/ included) is
    created there unless it is in a git checkout with a commit to start from."""
    project = Path(path).resolve()
    if found := _git_problem(project):
        problem, fix = found
        raise click.ClickException(problem + (f"\nRun: {fix}" if fix else ""))
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


def _how_to_write(name: str | None = None) -> str:
    """Where to look for writing a config (`run` without one): bare `autohelix` with an
    agent, an example, the docs' configs, the packaged template."""
    file = f"{CONFIG_DIR}/{name}.yaml" if name else CONFIG_FILE
    docs = docs_dir()
    where = f" (in {docs})" if docs else ""
    return (f"Write {file}: run `autohelix` in a terminal to set it up with an agent, or start from an example "
            f"({EXAMPLES}), the config in getting-started.md or config.md{where}, or the packaged template "
            f"({TEMPLATE}).")


def _segment_config(project: Path, name: str | None, command: str) -> Path:
    """The config a segment uses (`find_config`), its errors with this command's advice
    (`resume`'s only when there are runs to name; else, as `run`'s, how to start one)."""
    resume = command == "resume" and bool(project_runs(project))
    pick = ("Name a run or a config: `autohelix resume <id|name>`." if resume
            else "Pick one: `autohelix run <name>`.")
    try:
        config = find_config(project, name)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="CONFIG") from e
    except ConfigNotFound as e:
        write = f" {_how_to_write(name)}"
        if e.configs:
            advice = " " + pick if name is None else write
        else:
            advice = _ancestor_configs(project) or (" " + pick if resume else write)
        raise click.ClickException(str(e) + advice) from e
    others = [Path(c).stem for c in project_configs(project) if c != CONFIG_FILE]
    if name is None and config == project / CONFIG_FILE and others:
        example = f"`autohelix {'resume' if resume else 'run'} {others[0]}`"
        click.echo(click.style(f"Using {CONFIG_FILE}; {CONFIG_DIR}/ also has: {', '.join(others)} ({example})",
                               dim=True))
    return config


def _start(project: Path, config: Path | None, **selection) -> Harness:
    """The harness for a segment. There is no setup step: the first run creates
    the local git excludes and .autohelix/, once nothing is left to refuse."""
    try:
        return Harness(project, config_file=config, **selection)
    except (RuntimeError, FileNotFoundError, ConfigError) as e:
        raise click.ClickException(str(e)) from e


class JudgeCli(click.Choice):
    """`--cli` of `ask`/`review`: claude-code or codex. The test CLI (`mock`) is accepted
    but not offered; `claude` is an error naming `claude-code`."""

    def __init__(self):
        super().__init__([name for name in JUDGE_CLIS if name != "mock"])

    def convert(self, value, param, ctx):
        if value in RENAMED_CLIS:
            self.fail(f"{value!r} is now {RENAMED_CLIS[value]!r}", param, ctx)
        return value if value == "mock" else super().convert(value, param, ctx)


def _removed_option(message: str):
    """A flag that is gone: hidden, and an error naming its replacement when given."""
    def fail(ctx, param, value):
        if value is not None:
            raise click.UsageError(message, ctx)
    return fail


def _judge_options(command):
    """`--cli`, `--model`, `--effort` of `ask`/`review`, and the flags that are gone."""
    for option in reversed([
        click.option("--cli", "cli_name", type=JudgeCli(),
                     help="The agent CLI: claude-code or codex (default: agent.cli from the config, "
                          "else claude-code). Its executable: $AUTOHELIX_CLAUDE_CODE_CMD or "
                          "$AUTOHELIX_CODEX_CMD, else PATH."),
        click.option("--model", help="Model, passed through (default: agent.model from the config, else "
                                     "the CLI's own default)."),
        click.option("--effort", help="Reasoning effort, passed through (default: agent.effort from the config)."),
        click.option("--agent", hidden=True, expose_value=False, is_eager=True,
                     callback=_removed_option("--agent is now --cli (claude-code or codex)")),
        click.option("--command", hidden=True, expose_value=False, is_eager=True,
                     callback=_removed_option("--command is gone: set AUTOHELIX_CLAUDE_CODE_CMD or "
                                              "AUTOHELIX_CODEX_CMD to the executable")),
    ]):
        command = option(command)
    return command


def _duration_option(ctx, param, value: str | None) -> int | None:
    if value is None:
        return None
    from autohelix.config import parse_duration

    try:
        return parse_duration(value)
    except (ValueError, OverflowError) as e:
        raise click.BadParameter(f"{value!r} is not a duration ({e}); e.g. 90m, 2h, 1h30m") from e


def _budget_options(more: str):
    """The budget flags `run` and `resume` share: how long this segment goes on
    (`more`: "" for a new run, " more" on resume)."""
    def decorate(command):
        for option in reversed(_budget_option_list(more)):
            command = option(command)
        return command
    return decorate


def _budget_option_list(more: str) -> list:
    return [
        click.option("--iterations", "-n", type=click.IntRange(min=1), metavar="N",
                     help=f"Run up to N{more} iterations (default {DEFAULT_ITERATIONS}, only when no budget "
                          "flag is given)."),
        click.option("--until", type=click.IntRange(min=1), metavar="N",
                     help="Run up to iteration N in total (safe to repeat; for scripts)."),
        click.option("--cost", "cost_usd", type=click.FloatRange(min=0, min_open=True), metavar="USD",
                     help="Stop once this segment has spent USD more (every call it charges, baseline "
                          "included; known costs only)."),
        click.option("--time", "time_seconds", callback=_duration_option, metavar="DURATION",
                     help="Stop starting iterations after DURATION of this segment's wall clock "
                          "(e.g. 2h, 90m; baseline included)."),
    ]


def _budget(iterations: int | None, until: int | None, cost_usd: float | None,
            time_seconds: int | None) -> Budget:
    try:
        return Budget(iterations=iterations, until=until, cost_usd=cost_usd, time_seconds=time_seconds)
    except ValueError as e:
        raise click.UsageError(str(e)) from e


def _interrupted_note(harness: Harness) -> str | None:
    """After an interrupted segment (Ctrl-C, killed, reboot), what it had left, for a
    resume given no budget flag: `1 more (default); the interrupted segment had 8 left: -n 8`."""
    status = read_run_status(harness.project_path, harness.paths.run_id) or {}
    cap = status.get("max_iterations")
    # A stop reason: it had ended for its own reason (a budget, a condition) before that.
    if status.get("state") not in ("interrupted", "crashed") or type(cap) is not int or status.get("stop_reason"):
        return None
    left = cap - harness.history.get_last_iteration()
    if left <= 0 or left == DEFAULT_ITERATIONS:
        return None
    return (f"{DEFAULT_ITERATIONS} more (default); the interrupted segment had {left} left: "
            f"-n {left}")


def _stopped_note(harness: Harness) -> str | None:
    """After a stop condition ended the last segment: resume continues, and says when
    it stops again (stop conditions are checked after each accepted iteration)."""
    stop = (read_run_status(harness.project_path, harness.paths.run_id) or {}).get("stop_reason")
    if not isinstance(stop, dict) or stop.get("kind") != "condition":
        return None
    from autohelix.formatting import clean

    reason = clean(stop.get("reason") or stop.get("command") or "a stop condition held", 200)
    return (f"Run {short_id(harness.paths.run_id)} stopped because {reason}; it stops again after the next "
            "accepted iteration if that still holds.")


def _last_iteration(project: Path, config: Path | None, run_id: str | None) -> int:
    """The last iteration of the run `resume` would continue, read before anything is
    written; -1 when there is none to read (`_start` then refuses as usual)."""
    try:
        planned = planned_run(project, config, run_id=run_id, create=False)
    except (RuntimeError, ValueError):
        return -1
    return History(project, planned.run_id).get_last_iteration() if planned else -1


def _segment(harness: Harness, budget: Budget, report: bool) -> None:
    """Run a segment; exit 0 when it ended politely (a budget, a stop condition,
    `autohelix stop`), 1 on an error (the agent kept failing, landing failed),
    130 when interrupted (Ctrl-C, SIGTERM, `autohelix stop --now`; see
    `_interruptible`). The machine stays awake meanwhile (`keep_awake`)."""
    try:
        with keep_awake():
            ended = harness.run(budget, report=report)
    except RuntimeError as e:
        if getattr(e, "autohelix_reported", False):  # printed first, above landing and the run page
            raise SystemExit(1) from e
        raise click.ClickException(str(e)) from e
    if ended == "interrupted":
        raise SystemExit(130)


@main.command()
@click.argument("config_name", metavar="[CONFIG]", required=False)
@_path_option("Project directory (default: the current folder).")
@_segment_options
@_budget_options("")
@click.option("--group", help="Share notes and code with the project's other runs of this group "
                              "(landing defaults to manual).")
@click.option("--import", "import_run", metavar="RUN_ID",
              help="Continue from another run: its notes and history (read-only) in every iteration, its last "
                   "observe outputs at the baseline. Code still comes from your checkout.")
@click.option("--why", help="Why this run is started (one line, shown by `autohelix view`).")
@click.option("--check", is_flag=True,
              help="Only make the checks a run makes before it writes anything (config, prompt template, "
                   "clean checkout, agent command, scope); exit 0 if it would start, else 1.")
@_interruptible
def run(config_name: str | None, path: Path, verbose: bool, no_report: bool, land_mode: str | None,
        iterations: int | None, until: int | None, cost_usd: float | None, time_seconds: int | None,
        group: str | None, import_run: str | None, why: str | None, check: bool):
    """Start a new run of the optimization loop.

    Always a new run, with its own notes and history; to continue one, use
    `autohelix resume`. CONFIG names a config in autohelix/ (`perf` for
    autohelix/perf.yaml); without it, the run reads autohelix.yaml, else the
    only config in autohelix/.

    The config says what one iteration does; the budget flags say how long this
    command (a segment of the run) goes on: -n N iterations (or --until N),
    --cost USD, --time DURATION, whichever comes first. Without any of them,
    1 iteration: type -n for a loop. With only --cost or --time, no iteration cap.
    `autohelix stop` ends it after its current iteration; Ctrl-C, `kill PID`
    (SIGTERM) or `autohelix stop --now` at once (the iteration recorded, the run
    landed).

    With --group NAME, each iteration's agent also sees the other non-archived
    runs of that group: their notes and history, and their code (each run's
    ref, refs/autohelix/<id>). Groups are for runs that should build on each
    other's work. To compare approaches, start separate runs with --land manual,
    compare them with `autohelix view`, land the winner and archive the rest.

    With --import RUN_ID (or its last part), the new run continues from that
    one: its agents see its run.json, history and notes (read-only, in
    .autohelix-iteration/runs/<id>/) and its code's ref, and the baseline's
    observe entries get its last outputs as $AUTOHELIX_PREVIOUS. Your notes,
    budgets, metric reference and group are this run's own. To fork from the
    run's code, `git checkout autohelix/<id>` first.

    With --check, nothing is started or written: the command makes every check
    the run would make before writing anything, prints what it finds, and exits
    0 when the run would start (for setup and CI).
    """
    budget = _budget(iterations, until, cost_usd, time_seconds)
    group = _group(group)
    why = _why(why)
    project = _git_project(path)
    imported = _run_id(project, import_run)
    config = _segment_config(project, config_name, "run")
    harness = _start(project, config, verbose=verbose, new=True, land_mode=land_mode,
                     group=group or None, import_run=imported, why=why, check_only=check)
    if check:
        shown = config_relative(project, config)
        click.echo(f"OK: {shown} is ready; `{run_command(shown)}` would start a new run.")
        return
    _segment(harness, budget, not no_report)


def _resume_target(project: Path, value: str | None) -> tuple[str | None, Path | None]:
    """What `resume VALUE` continues: (run id, None) for a run id or its last part, else
    (None, the config) for a config name (`perf`: autohelix/perf.yaml), whose latest run
    resumes, as a bare `resume` does for the config a bare `run` reads."""
    if value is None:
        return None, _segment_config(project, None, "resume")
    try:
        return resolve_run(project, value), None
    except RuntimeError as error:
        try:
            find_config(project, value)
        except (ConfigNotFound, ValueError):
            if "several runs" in str(error):
                raise click.ClickException(str(error)) from error
            raise click.ClickException(
                f"No run or config {value!r} in {project}: name a run (its id or last part, e.g. k3f9) or a config "
                f"(`perf` for {CONFIG_DIR}/perf.yaml); `autohelix view --once` lists runs.") from error
    return None, _segment_config(project, value, "resume")


@main.command()
@click.argument("target", metavar="[RUN_ID | CONFIG]", required=False)
@_path_option()
@_segment_options
@_budget_options(" more")
@click.option("--group", help="Set or change the run's group from its next iteration; '' clears it.")
@_interruptible
def resume(target: str | None, path: Path, verbose: bool, no_report: bool, land_mode: str | None,
           iterations: int | None, until: int | None, cost_usd: float | None, time_seconds: int | None,
           group: str | None):
    """Continue a run: RUN_ID (or its last part), or the latest run of CONFIG (default: the config a bare `run` reads).

    A run uses the config it stored when it started. If the file has changed
    since (comments and formatting aside), resume refuses: `autohelix run
    --import <id>` applies the new config in a new run; a deleted file is fine.
    With a config name (`resume perf`, like `run perf`), or none, the run is
    that config's most recently active run that isn't archived, preferring
    runs that captured their baseline. A run id takes precedence over a config
    of the same name.

    Budgets count this segment (this command): `resume -n 10 --cost 5` runs 10 more
    iterations, up to $5 more, whatever earlier segments spent; --until N
    runs up to iteration N in total. Without a budget flag, 1 more. The run
    keeps its landing mode and group unless --land or --group changes them;
    with nothing to run (--until already reached) they are stored at once.
    Exits as `run` does: 0, 1 (also when there is no run to resume), 130.
    """
    budget = _budget(iterations, until, cost_usd, time_seconds)
    group = _group(group)
    project = _git_project(_project(path))
    selected, config = _resume_target(project, target)
    if budget.until is not None and (done := _last_iteration(project, config, selected)) >= budget.until:
        if land_mode or group is not None:
            paths = planned_run(project, config, run_id=selected, create=False)
            try:
                changed = set_options(paths, land_mode=land_mode, group=group)
            except RuntimeError as error:
                raise click.ClickException(str(error)) from error
            click.echo(f"Run {short_id(paths.run_id)}: {', '.join(changed) or 'no change'}; already at iteration "
                       f"{done}, --until {budget.until} runs nothing.")
            if note := group_landing_note(paths, paths.read_identity(), group, land_mode):
                _warn(note)
            return
        click.echo(f"Already at iteration {done}; --until {budget.until} runs nothing.")
        return
    harness = _start(project, config, verbose=verbose, run_id=selected, create=False, land_mode=land_mode,
                     group=group)
    if budget.default and (note := _interrupted_note(harness)):
        _warn(note)
    if note := _stopped_note(harness):
        _warn(note)
    _segment(harness, budget, not no_report)


@main.command()
@click.argument("run_id", required=False)
@_path_option()
@click.option("--into", help="Target branch, required for runs created detached.")
def land(run_id: str | None, path: Path, into: str | None):
    """Fast-forward a target branch to a run's accepted code (RUN_ID or its last part, else
    the running run, else the most recently active one)."""
    paths = _chosen_run(_project(path), run_id)
    try:
        with RunLock(paths.lock):
            data = paths.read_identity()
            land_run(paths, data, run_tip(paths, data), into)
        click.echo(f"Landed {short_id(paths.run_id)}.")
    except RuntimeError as e:
        raise click.ClickException(str(e)) from e


@main.command()
@click.argument("run_ids", nargs=-1, required=True)
@_path_option()
@click.option("--undo", is_flag=True, help="Bring archived runs back (their archive note stays).")
@click.option("--why", help="Why they are archived (one line, kept with each run; replaces an earlier note).")
def archive(run_ids: tuple[str, ...], path: Path, undo: bool, why: str | None):
    """Set runs aside: hidden from the run list and skipped by a bare resume.

    Their history, notes and commits stay; `autohelix resume RUN_ID` (or
    --undo) brings one back. Every id is checked first: an unknown one does
    nothing at all. Exits 1 if any run could not be archived.
    """
    if undo and why is not None:
        raise click.UsageError("--why goes with archiving, not --undo.")
    why = _why(why)
    done = "Unarchived" if undo else "Archived"

    def act(paths: RunPaths) -> str:
        archive_run(paths, undo=undo, why=why)
        return f"{done} {short_id(paths.run_id)}."

    _, failed = _each_run(_project(path), run_ids, act)
    if failed:
        raise SystemExit(1)


def _each_run(project: Path, values, act) -> tuple[list[str], bool]:
    """`archive`, `delete` and `stop` on several runs: every id resolved first (an unknown
    one refuses the command, nothing done), then `act` on each, printing the line it
    returns or the error it raises (RuntimeError) and going on. Returns the short ids it
    acted on and whether any failed (the command then exits 1)."""
    try:
        ids = resolve_runs(project, values)
    except RuntimeError as error:
        raise click.ClickException(f"{error} Nothing was done.") from error
    handled, failed = [], False
    for run_id in ids:
        try:
            message = act(RunPaths(project, run_id))
        except RuntimeError as error:
            click.echo(f"Error: {error}", err=True)
            failed = True
            continue
        click.echo(message)
        handled.append(short_id(run_id))
    return handled, failed


@main.command()
@click.argument("run_ids", nargs=-1, required=True)
@_path_option()
@click.option("--force", is_flag=True, help="Also delete runs with unlanded commits (they are lost).")
def delete(run_ids: tuple[str, ...], path: Path, force: bool):
    """Delete runs: their history, notes, logs, ref, kept rejected trees and leftover copies.

    Refuses while a run is running, and for runs with commits not on their
    target branch unless --force. To keep a run but hide it, use archive.
    Every id is checked first: an unknown one does nothing at all. Exits 1 if
    any run could not be deleted.
    """
    def act(paths: RunPaths) -> str:
        delete_run(paths, force=force, warn=lambda message: _warn(message, err=True))
        return f"Deleted {short_id(paths.run_id)}."

    _, failed = _each_run(_project(path), run_ids, act)
    if failed:
        raise SystemExit(1)


@main.command()
@click.argument("question")
@click.option("--yes", "yes_means", help='What a "yes" answer means.')
@click.option("--no", "no_means", help='What a "no" answer means.')
@click.option("--rubric", type=click.Path(exists=True, dir_okay=False), help="File with criteria for the answer.")
@_judge_options
@click.option("--timeout", callback=_duration_option, metavar="DURATION",
              help="How long before giving up, retry included: 90s, 10m (a bare number is seconds; default 10m).")
def ask(question: str, yes_means: str | None, no_means: str | None, rubric: str | None,
        cli_name: str | None, model: str | None, effort: str | None, timeout: int | None):
    """Ask an agent a yes/no question about the candidate, or by hand the current directory.

    In a run (a gate's `ask:`, a stop command), it judges a fresh copy of the
    candidate with candidate.diff, the complete change. By hand, it judges the
    current directory; the change is the working tree (new files included) against HEAD.

    Exits 0 for yes, 1 for no, 2 if no valid answer was produced; prints the
    reason. Use it in gates: (or as an `ask:` entry there), stop:, or any script. The agent gets
    read-only tools and no project settings; agent settings come from
    $AUTOHELIX_CONFIG, else the config a bare `autohelix run` here would read
    (if there is exactly one), else the defaults; by hand, the line on stderr names
    the agent and where its settings came from.
    """
    import signal

    from autohelix.formatting import format_cost
    from autohelix.judge import JudgeError, ask as run_ask, base_config, describe_agent, judge_config
    from autohelix.usage_log import ENV as USAGE_ENV

    def _terminate(signum, frame):  # the harness timed this command out: stop the agent too
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    cwd = Path.cwd()
    try:
        settings, source = base_config(cwd)
        config = judge_config(settings, cli=cli_name, model=model, effort=effort, timeout=timeout)
        answer = run_ask(question, cwd, config, yes=yes_means, no=no_means,
                         rubric=Path(rubric).read_text() if rubric else None)
    except (JudgeError, ConfigError) as e:  # ConfigError: a variable that is gone
        click.echo(f"autohelix ask: no valid answer: {e}", err=True)
        raise SystemExit(2)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as e:  # noqa: BLE001 - exit 1 would read as "no"
        click.echo(f"autohelix ask: failed: {type(e).__name__}: {e}", err=True)
        raise SystemExit(2)
    click.echo(answer.reason)
    if not os.environ.get(USAGE_ENV):
        used = describe_agent(config, "--cli" if cli_name else source)
        click.echo(f"autohelix ask: {'yes' if answer.yes else 'no'} · {used} · "
                   f"{format_cost(answer.cost_usd, precision=4)}", err=True)
    raise SystemExit(0 if answer.yes else 1)


def _review_metrics(ctx, param, values: tuple[str, ...]) -> list[str]:
    import re

    names: list[str] = []
    for value in values:
        name, _, direction = value.rpartition(":")
        if name and direction in ("higher", "lower"):
            raise click.BadParameter(f"{value!r}: the direction is gone, give the name only (--metric {name}); "
                                     "a `review:` entry's `metrics:` keeps it for its metric gate")
        name = value
        if not re.fullmatch(r"\w+", name):
            raise click.BadParameter(f"{value!r}: expected a metric NAME (letters, digits, _)")
        if name in names:
            raise click.BadParameter(f"{name!r} is given twice")
        names.append(name)
    return names


@main.command()
@click.argument("prompt")
@click.option("--previous", is_flag=True,
              help="Include this entry's review from the last accepted iteration (in a run).")
@click.option("--metric", "metrics", multiple=True, metavar="NAME", callback=_review_metrics,
              help="A number the review must report, printed as ##autohelix[NAME=value]. Repeatable.")
@_judge_options
@click.option("--timeout", callback=_duration_option, metavar="DURATION",
              help="How long before giving up, retry included: 90s, 10m (a bare number is seconds; default: "
                   "the command's deadline in a run, else 30m).")
@click.option("--base", help="Outside a run: the commit the working tree is compared with (default HEAD).")
def review(prompt: str, previous: bool, metrics: list[str], cli_name: str | None, model: str | None,
           effort: str | None, timeout: int | None, base: str | None):
    """Review a change with an agent; prints the review as feedback.

    PROMPT is the review request; put it after `--` (`autohelix review -- "..."`)
    so a prompt starting with `-` isn't read as an option. The agent gets
    read-only tools and no project settings; agent settings come from
    $AUTOHELIX_CONFIG, else the config a bare `autohelix run` here would read
    (if there is exactly one), else the defaults; by hand, the line on stderr names
    the agent and where its settings came from.

    In a run (an `observe:` entry's `review:`), it reviews a fresh copy of the
    candidate. By hand, it reviews the current directory; the change is the
    working tree (new files included) against --base.

    Prints the review, then one ##autohelix[NAME=value] line per --metric.
    Exits 0 on success, 2 if no valid review was produced, a requested number
    is missing, or the reviewer changed files.
    """
    import signal

    from autohelix.formatting import format_cost
    from autohelix.judge import JudgeError, base_config, describe_agent, judge_config
    from autohelix.review import DEFAULT_TIMEOUT, review as run_review, review_schema
    from autohelix.usage_log import ENV as USAGE_ENV

    def _terminate(signum, frame):  # the harness timed this command out: stop the agent too
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    cwd = Path.cwd()
    try:
        if not prompt.strip():
            raise JudgeError("the review prompt is empty")
        settings, source = base_config(cwd)
        config = judge_config(settings, cli=cli_name, model=model, effort=effort,
                              timeout=timeout, schema=review_schema(metrics),
                              default_timeout=DEFAULT_TIMEOUT)
        result = run_review(prompt, cwd, config, metrics=metrics, previous=previous, base=base,
                            timeout=timeout,
                            warn=lambda message: click.echo(f"autohelix review: {message}", err=True))
    except (JudgeError, ConfigError) as e:  # ConfigError: a variable that is gone
        click.echo(f"autohelix review: no valid review: {e}", err=True)
        raise SystemExit(2)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as e:  # noqa: BLE001 - a failed review is never a success
        click.echo(f"autohelix review: failed: {type(e).__name__}: {e}", err=True)
        raise SystemExit(2)
    click.echo(result.output())
    if not os.environ.get(USAGE_ENV):
        used = describe_agent(config, "--cli" if cli_name else source)
        click.echo(f"autohelix review: ok · {used} · {format_cost(result.cost_usd, precision=4)}", err=True)


@main.command(context_settings={"allow_extra_args": True})  # refused below, with --run's spelling
@click.argument("message")
@_path_option()
@click.option("--run", "run_id", help="Run id or its last part (default: the running run, else the most recently "
                                      "active one).")
@click.pass_context
def hint(ctx: click.Context, message: str, path: Path, run_id: str | None):
    """Send a hint to a run's agent (picked up next iteration).

    Appends a timestamped message to the run's hints.md (--run, else the
    running run, else the most recently active one that isn't archived). Each
    iteration's prompt holds the hints' text, under "Hints from the person
    running this run (newest last)"; a run that isn't running gets it when it
    is resumed.
    """
    from datetime import datetime

    if ctx.args:
        given = f'--run {message} "{" ".join(ctx.args)}"' if len(ctx.args) == 1 else '--run ID "MESSAGE"'
        raise click.UsageError(f"hint takes one message (quoted); the run goes in --run: autohelix hint {given}", ctx)
    paths = _chosen_run(_project(path), run_id)
    try:
        paths.read_identity()
    except RuntimeError as error:
        raise click.ClickException(str(error)) from error
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(paths.hints, "a") as f:
        f.write(f"- [{timestamp}] {message}\n")
    short = short_id(paths.run_id)
    idle = "" if is_live(paths) else f" (not running; read by its next `autohelix resume {short}`)"
    click.echo(f"Hint added to run {short}{idle}.")


def _process_command(pid: int) -> str:
    """A process's command line (`ps`), "" when there is no such process or ps fails."""
    try:
        result = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _stop_run(paths: RunPaths, why: str | None, now: bool) -> str:
    """Ask one run to stop (`autohelix stop`): what it does next.
    RuntimeError: refused (not running, --now not possible)."""
    import signal
    import socket

    from autohelix.run_status import read_stop_request, write_stop_request

    short = short_id(paths.run_id)
    # A running status whose process is gone (SIGKILL, out of memory: its lock is free)
    # reads as crashed (`read_run_status`).
    status = read_run_status(paths.project, paths.run_id) or {}
    state = status.get("state")
    if state != "running" or type(status.get("segment")) is not int:
        last = {"crashed": "its process is gone; it stopped unexpectedly", "running": None}.get(state, state)
        raise RuntimeError(f"Run {short} is not running" + (f" ({last})." if last else "."))
    pid, host = status.get("pid"), status.get("host")
    if now:
        if host != socket.gethostname():
            raise RuntimeError(f"Run {short} is running on {host}; --now works only there. Without --now it "
                               "stops after its current iteration.")
        if type(pid) is not int or pid <= 1 or "autohelix" not in _process_command(pid):
            raise RuntimeError(f"Run {short}'s process ({pid}) is not an autohelix process; not interrupted.")
    earlier = read_stop_request(paths, status["segment"])
    if now and earlier and earlier.get("now") is True:
        # A second SIGTERM would cut short the first one's cleanup (its grace before SIGKILL).
        return f"Run {short} is already being interrupted."
    try:
        write_stop_request(paths, segment=status["segment"], why=why, now=now)
    except OSError as error:
        raise RuntimeError(f"Could not ask run {short} to stop: {error}") from error
    if now:
        try:
            # SIGTERM, not SIGINT: a run a script started in the background ignores SIGINT.
            # The harness takes it as Ctrl-C; the request (above) says who sent it and why.
            os.kill(pid, signal.SIGTERM)
        except OSError as error:
            raise RuntimeError(f"Could not interrupt run {short} ({pid}): {error}; it still stops after "
                               "its current iteration.") from error
    return _stop_message(short, status, now)


def _stop_message(short: str, status: dict, now: bool) -> str:
    """What a stopped run does next, with the iteration its status names."""
    iteration = status.get("iteration")
    where = "the baseline" if iteration == 0 else f"iteration {iteration}" if type(iteration) is int else None
    if now:
        return f"Interrupting run {short}" + (f" ({where})." if where else ".")
    if iteration == 0:
        return f"Run {short} stops after the baseline."
    return f"Run {short} stops after its current iteration" + (f" ({iteration})." if where else ".")


@main.command()
@click.argument("run_ids", nargs=-1, required=True)
@_path_option()
@click.option("--why", help="Why they are stopped (one line, shown with the stop reason).")
@click.option("--now", is_flag=True,
              help="Also interrupt the current iteration, like Ctrl-C in the run's terminal (same host only).")
def stop(run_ids: tuple[str, ...], path: Path, why: str | None, now: bool):
    """End running runs after their current iteration (RUN_ID or its last part).

    The iteration under way is never interrupted; the segment then ends as when
    its budget runs out: it lands (auto landing), writes the summary (unless
    --no-report) and the run page. Asked during the baseline, it stops after
    the baseline.
    `autohelix resume RUN_ID` continues it later. Asking again replaces --why.

    --now also interrupts the current iteration, like Ctrl-C in the run's
    terminal (it sends SIGTERM, which a run takes as Ctrl-C; so does a plain
    `kill PID`): an interrupted iteration is recorded as rejected, the agent's
    and commands' processes are ended, the run lands, no summary is written.
    Only on the host the run is running on.

    Every id is checked first: an unknown one stops nothing. Exits 1 if any
    run could not be asked (not running, say).
    """
    why = _why(why)
    project = _project(path)
    asked, failed = _each_run(project, run_ids, lambda paths: _stop_run(paths, why, now))
    if asked:
        elsewhere = "" if project == Path.cwd().resolve() else f" -p {shlex.quote(_shown(project))}"
        click.echo(f"See when it has ended: autohelix wait{elsewhere} {' '.join(asked)} "
                   "(repeat until it prints ended or idle)")
    if failed:
        raise SystemExit(1)


_JSON_HELP = "Print once as JSON and exit (for scripts and agents; fields are only ever added)."


@main.command()
@click.argument("run_id", required=False)
@_path_option()
@click.option("--terminal", is_flag=True, help="Follow one run live in the terminal (the running one, else the latest).")
@click.option("--once", is_flag=True, help="Print the runs table (with RUN_ID: that run's) once and exit.")
@click.option("--json", "as_json", is_flag=True, help=_JSON_HELP)
@click.option("--metric", help="With --once and no RUN_ID: the table's metric column (default: the most recent run's first).")
@click.option("--port", type=click.IntRange(0, 65535), help="The web page's port (default 8765).")
@click.option("--open", "open_browser", is_flag=True, help="Open the web page in a browser.")
def view(run_id: str | None, path: Path, terminal: bool, once: bool, as_json: bool, metric: str | None,
         port: int | None, open_browser: bool):
    """The project's runs on a live web page; --terminal follows a run here.

    The page is served on 127.0.0.1 until Ctrl-C: the run list at /, each run at
    /run/<id>/ (with RUN_ID, the page opens there). Over SSH: ssh -L 8765:127.0.0.1:8765 host, then open
    http://localhost:8765, or use --terminal.

    --terminal shows one run (RUN_ID or its last part, else the running run, else
    the most recently active): its header, one row per iteration, and the live
    output of the agent, a review or the summary agent. Keeps waiting when the
    run stops; Ctrl-C stops watching (not the run).

    --once prints the runs table (one line per run; archived runs are counted,
    not listed), or with RUN_ID that run's screen, and exits; --json prints the
    same as JSON (every run, archived ones too). All of it only reads .autohelix/.
    """
    if as_json + once + terminal > 1:
        raise click.UsageError("Use one of --terminal, --once and --json.")
    if (port is not None or open_browser) and (as_json or once or terminal):
        raise click.UsageError("--port and --open apply to the web page (without --terminal, --once, --json).")
    if metric and (not once or run_id):
        raise click.UsageError("--metric applies to the runs table (--once without RUN_ID); the web page has a menu.")
    project = _project(path)
    selected = _chosen_run(project, run_id).run_id if run_id or terminal else None
    if terminal:
        _view_live(project, selected)
        return
    if not (once or as_json):
        _serve(project, port, open_browser, f"/run/{selected}/" if selected else "/")
        return
    from rich.console import Console

    from autohelix import terminal as screen
    from autohelix.viewer import ProjectRuns

    runs = ProjectRuns(project)
    if selected is None:
        data = runs.runs()
        if as_json:
            click.echo(json.dumps(data, indent=2))
            return
        names = run_table.metric_names([r for r in data["runs"] if not r.get("archived")])
        if metric and metric not in names:
            raise click.ClickException(f"No run has metric {metric!r}" + (f"; known: {', '.join(names)}" if names else ""))
        console = Console()
        screen.print_lines(console, run_table.dashboard_lines(data, console.width, metric=metric))
        return
    if not runs.exists(selected):
        raise click.ClickException(f"Run {short_id(selected)} has no readable run.json in {project}.")
    try:
        data = runs.run(selected)
    except RuntimeError as error:
        raise click.ClickException(str(error)) from error
    if as_json:
        click.echo(json.dumps(data, indent=2))
        return
    console = Console()
    saved = run_table.Saved.read(RunPaths(project, selected))
    screen.print_lines(console, run_table.view_lines(data, saved, console.width,
                                                     show_mark=screen.shows_mark(console)))


def _view_live(project_path: Path, run_id: str) -> None:
    from rich.console import Console

    from autohelix.live import view as live_view

    console = Console()
    try:
        backend = load_run_config(RunPaths(project_path, run_id)).agent.cli
    except Exception:  # noqa: BLE001 - the log parser can tell backends apart on its own
        backend = None
    try:
        live_view(project_path, backend, console, run_id=run_id)
    except KeyboardInterrupt:
        console.print("Stopped watching. The run is unaffected.", style="dim")


def _serve(project: Path, port: int | None, open_browser: bool, page: str) -> None:
    from autohelix.viewer import make_server

    port = 8765 if port is None else port
    try:
        server = make_server(project, port)
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
@_path_option()
def report(run_id: str | None, path: Path):
    """Write (or rewrite) a run's summary (RUN_ID or its last part, else the running or latest run).

    One agent call reads the run's history, notes, reviews, logs and commits, and writes
    the selected run's report.md. `run` and `resume` do this automatically when a run
    finishes; use this after an interrupted run or to regenerate it. It reads the config
    the run stored when it started.
    """
    from rich.console import Console

    from autohelix.report import write_report

    console = Console()
    project_path = _project(path)
    paths = _chosen_run(project_path, run_id)
    run_id = paths.run_id
    if History(project_path, run_id).get_last_iteration() == 0:
        _warn(f"Run {short_id(run_id)} has no iterations yet. Nothing to report.")
        raise SystemExit(1)
    from rich.text import Text

    from autohelix import terminal

    try:
        config = load_run_config(paths)
    except (RuntimeError, ConfigError) as e:
        terminal.show(console, str(e), Text("Error: ", style="red"))
        raise SystemExit(1)
    try:
        written = write_report(config, project_path, console, run_id=run_id)
    except ConfigError as e:  # the agent's executable: a variable that is gone (`agents.executable`)
        terminal.show(console, str(e), Text("Error: ", style="red"))
        raise SystemExit(1)
    if not written:
        raise SystemExit(1)
    from autohelix.viewer import write_static

    write_static(project_path, run_id=run_id)
    for label, file in (("summary", paths.report), ("page", paths.report_html)):
        shown = file.relative_to(Path.cwd()) if file.is_relative_to(Path.cwd()) else file
        console.print(Text.assemble("  ", (f"{label:<9}", "dim"), str(shown)), highlight=False)


@main.command()
@click.argument("run_ids", nargs=-1, metavar="[RUN_ID]...")
@click.option("--timeout", "timeout_seconds", callback=_duration_option, metavar="DURATION",
              help="Give up after this long without an event (e.g. 30s, 10m, 1h30m): print a `timeout` "
                   "event and exit 2.")
@click.option("--follow", is_flag=True, help="Never exit: print one line per event, in order.")
@click.option("--after", "cursor", metavar="CURSOR",
              help="Continue after the event that printed this cursor (an earlier event's `cursor`).")
@_path_option()
def wait(run_ids: tuple[str, ...], timeout_seconds: int | None, follow: bool, cursor: str | None, path: Path):
    """Block until something happens in a run, print it as one JSON line, and exit.

    Events: a segment `started` (a new run or a resume), an `iteration` recorded
    (the baseline is 0), a segment `ended`, a run `crashed` (its process is gone).
    Watches the named runs (RUN_ID or its last part), else every run of the project,
    runs started later included.

    Each event has a `cursor`: `wait --after CURSOR` returns the next event after it,
    at once if one is already pending, so a loop of wait, inspect, `wait --after`
    misses nothing and repeats nothing. Without --after, only what happens from now
    on counts (to watch a run you are about to start, take a cursor first, e.g. from
    `wait --timeout 1s`).

    With run ids, when none of them is running and nothing is pending, prints an
    `idle` event at once. --timeout prints a `timeout` event (with a cursor) and
    exits 2; --follow never exits and prints every event (no `idle`).

    Only reads .autohelix/: safe beside running runs. Every event has `schema`
    (fields are only ever added), `event`, `run`, `short`, `time` and `cursor`.
    """
    from autohelix.events import Watcher

    if follow and timeout_seconds is not None:
        raise click.UsageError("--timeout doesn't apply with --follow (it never exits).")
    project = _project(path)
    try:
        ids = resolve_runs(project, run_ids)
    except RuntimeError as error:
        raise click.ClickException(str(error)) from error

    def emit(event: dict) -> None:
        click.echo(json.dumps(event))  # flushed: pipe-friendly

    try:
        try:
            watcher = Watcher(project, run_ids=ids or None, after=cursor)
        except ValueError as error:
            raise click.BadParameter(str(error), param_hint="--after") from error
        if follow:
            while True:
                emit(watcher.next())
        event = watcher.next(timeout=timeout_seconds, idle=bool(ids))
        emit(event)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except BrokenPipeError:
        # The reader went away (`wait --follow | head -1`): exit quietly.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(0)
    if event["event"] == "timeout":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
