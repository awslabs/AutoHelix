"""Bare `autohelix`: the entry screen (the project's runs, one question) and the agent
session it starts with the bundled skill; without a terminal, the help and exit 2."""

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import click
import click.testing
import pytest
import yaml
from click.testing import CliRunner

from autohelix import cli
from autohelix.cli import main
from autohelix.config import load_config
from tests.helpers import git


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A git checkout with a commit, the current directory, no agent installed, no TTY
    (CliRunner's streams aren't terminals)."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    path = tmp_path / "project"
    path.mkdir()
    git(path, "init", "-q")
    (path / "main.py").write_text("x = 1\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    monkeypatch.chdir(path)  # restored afterwards, also after the chdir before exec
    agents([])(monkeypatch)
    return path


def agents(names):
    def install(monkeypatch):
        real = shutil.which
        monkeypatch.setattr(shutil, "which",
                            lambda cmd, *a, **k: f"/usr/bin/{cmd}" if cmd in names else
                            None if cmd in ("claude", "codex", "opencode", "mock") else real(cmd, *a, **k))
    return install


@pytest.fixture
def execs(monkeypatch):
    """os.execvp, recorded instead of replacing the test process."""
    calls = []
    monkeypatch.setattr(os, "execvp", lambda file, argv: calls.append((file, list(argv), os.getcwd())))
    return calls


@pytest.fixture
def tty(monkeypatch):
    """CliRunner's stdin and stdout report a terminal, so the real `_interactive` says yes."""
    monkeypatch.setattr(click.testing._NamedTextIOWrapper, "isatty", lambda self: True, raising=False)


def entry(input=None):
    """Bare `autohelix`, 200 columns wide (long temporary paths fit on a line)."""
    return CliRunner(env={"COLUMNS": "200"}).invoke(main, [], input=input)


def lines(result):
    """The output's lines, without colours and trailing spaces."""
    return [line.rstrip() for line in click.unstyle(result.output).splitlines()]


def flat(text):
    """Output without the line wraps of a narrow terminal (nor colours)."""
    return " ".join(click.unstyle(text).split())


def files(project):
    return sorted(str(p.relative_to(project)) for p in project.rglob("*")
                  if ".git" not in p.parts and ".autohelix" not in p.parts)


def mock_config(project, name=None, commit=True):
    """A mock-agent config (autohelix.yaml, or autohelix/NAME.yaml), committed."""
    path = project / (f"autohelix/{name}.yaml" if name else "autohelix.yaml")
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump({"goal": "Make a checked change", "agent": {"cli": "mock", "mock": {
        "change_file": "main.py", "change_content": "x = {iteration}\n", "cost_usd": 0.1}}}))
    if commit:
        git(project, "add", "-A")
        git(project, "commit", "-qm", "config")
    return path


def start_run(project, *args):
    """A real one-iteration mock run; its short id."""
    result = CliRunner().invoke(main, ["run", *args, "-n", "1", "--no-report"])
    assert result.exit_code == 0, result.output
    from autohelix.runs import project_runs, short_id

    return short_id(project_runs(project)[0][0].run_id)


# --- without a terminal ------------------------------------------------------


@pytest.mark.parametrize("runs", [False, True])
def test_without_a_terminal_the_help_and_exit_2(project, monkeypatch, execs, runs):
    agents(["claude"])(monkeypatch)
    if runs:
        mock_config(project)
        start_run(project)
    result = entry()
    assert result.exit_code == 2
    assert result.stdout == ""  # all of it on stderr, as click's own usage errors
    assert "Usage: " in result.stderr and "Commands:" in result.stderr
    assert result.stderr.splitlines()[-1] == "Run `autohelix` in a terminal to start."
    assert "Ask Claude Code" not in result.output and not execs


def test_interactive_needs_both_streams_to_be_terminals(monkeypatch):
    class Stream:
        def __init__(self, tty):
            self.tty = tty

        def isatty(self):
            return self.tty

    for stdin, stdout, expected in ((True, True, True), (True, False, False), (False, True, False)):
        monkeypatch.setattr(sys, "stdin", Stream(stdin))
        monkeypatch.setattr(sys, "stdout", Stream(stdout))
        assert cli._interactive() is expected


def test_setup_and_init_are_gone(project):
    for name in ("setup", "init"):
        result = CliRunner().invoke(main, [name])
        assert result.exit_code == 2 and f"No such command '{name}'" in result.output
    listed = CliRunner().invoke(main, ["--help"]).output
    assert not re.search(r"^  (setup|init)\b", listed, re.M)
    assert CliRunner().invoke(main, ["skill", "install", "--help"]).exit_code == 0


# --- no runs, no configs: one question ------------------------------------------


def test_nothing_yet_asks_one_question_and_enter_starts_the_session(project, monkeypatch, execs, tty):
    agents(["claude"])(monkeypatch)
    result = entry(input="\n")
    assert result.exit_code == 0, result.output
    shown = lines(result)
    assert shown[1].startswith("  AutoHelix  ") and shown[1].endswith(f" · {project.resolve()}")
    assert "  autohelix --help: all commands" in shown
    assert "run   state" not in result.output  # no table
    assert "Set up an experiment with Claude Code? Enter/q" in flat(result.output)
    assert "  Starting Claude Code with the AutoHelix skill…" in shown
    [(file, argv, cwd)] = execs
    assert file == "claude" and Path(cwd) == project.resolve()
    prompt = argv[-1]
    assert "The project has no runs yet." in prompt and "It has no AutoHelix config yet." in prompt
    assert "This is the project's first config: save it as `autohelix.yaml`" in prompt
    assert "`autohelix run` command (with its budget flags) for me to run" in prompt
    assert files(project) == ["main.py"]  # nothing written


@pytest.mark.parametrize("answer", ["q\n", "Q\n"])
def test_q_quits_with_exit_0_and_starts_nothing(project, monkeypatch, execs, tty, answer):
    agents(["claude"])(monkeypatch)
    result = entry(input=answer)
    assert result.exit_code == 0, result.output
    assert not execs and "Starting" not in result.output


def test_eof_aborts_and_starts_nothing(project, monkeypatch, execs, tty):
    agents(["claude"])(monkeypatch)
    result = entry(input="")
    assert result.exit_code == 1 and "Aborted!" in result.output
    assert not execs


@pytest.mark.parametrize("where", ["question", "loading"])
def test_ctrl_c_exits_130_and_starts_nothing(project, monkeypatch, execs, tty, where):
    """Ctrl-C at the question (a real terminal's is in `test_cli_entry_pty.py`) or while
    the runs load: interrupted (130), not click's "Aborted!" (1)."""
    from autohelix import menu
    from autohelix.viewer import ProjectRuns

    agents(["claude"])(monkeypatch)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    if where == "question":
        monkeypatch.setattr(menu, "confirm", interrupt)
    else:
        monkeypatch.setattr(ProjectRuns, "runs", interrupt)
    result = entry(input="\n")
    assert result.exit_code == 130 and "Aborted!" not in result.output
    assert not execs
    if where == "loading":  # the header is drawn before the runs are read
        assert lines(result)[1].startswith("  AutoHelix  ")


def test_the_project_is_the_checkouts_top(project, monkeypatch, execs, tty):
    """No runs and no config from here up to the checkout's top: the top."""
    agents(["claude"])(monkeypatch)
    (project / "src" / "deep").mkdir(parents=True)
    mock_config(project / "src" / "deep" / "elsewhere", commit=False)  # below: not looked at
    monkeypatch.chdir(project / "src" / "deep")
    result = entry(input="\n")
    assert result.exit_code == 0, result.output
    assert lines(result)[1].endswith(f" · {project.resolve()}")
    [(_, argv, cwd)] = execs
    assert Path(cwd) == project.resolve() and f"in this project ({project.resolve()})" in argv[-1]


@pytest.mark.parametrize("start", ["app", "app/pkg"])
def test_the_project_is_the_nearest_folder_with_a_config(project, monkeypatch, execs, tty, start):
    """No runs: the folder `autohelix run` would take its config from (here or above), not the
    top, even when the top has a config too."""
    agents(["claude"])(monkeypatch)
    app = project / "app"
    (app / "pkg").mkdir(parents=True)
    mock_config(app, "perf", commit=False)
    mock_config(project, commit=False)
    monkeypatch.chdir(project / start)
    result = entry(input="\n")
    assert result.exit_code == 0, result.output
    assert lines(result)[1].endswith(f" · {app.resolve()}")
    assert "autohelix/perf.yaml, no runs yet: check and finish it" in flat(result.output)
    [(_, argv, cwd)] = execs
    assert Path(cwd) == app.resolve() and f"in this project ({app.resolve()})" in argv[-1]


# --- configs without runs ---------------------------------------------------------


def test_a_config_without_runs_is_named_and_offered_to_finish(project, monkeypatch, execs, tty):
    agents(["claude"])(monkeypatch)
    (project / "autohelix.yaml").write_text("goal: half done\n")  # hand-written, uncommitted
    result = entry(input="\n")
    assert result.exit_code == 0, result.output
    assert "  1. autohelix.yaml, no runs yet: check and finish it with Claude Code" in lines(result)
    assert " 2. Set up a new experiment with Claude Code it drafts a config with you" in flat_lines(result)
    prompt = execs[0][1][-1]
    assert "Its configs: autohelix.yaml (no runs yet)." in prompt
    assert 'I picked "autohelix.yaml, no runs yet: check and finish it with Claude Code"' in prompt
    assert 'I may switch to the other option ("Set up a new experiment with Claude Code")' in prompt
    assert "My first request: check and finish `autohelix.yaml`" in prompt
    assert "run --check` from the project root, committed, and the `autohelix run` command" in prompt


def test_new_beside_configs_proposes_a_name(project, monkeypatch, execs, tty):
    agents(["claude"])(monkeypatch)
    mock_config(project, "perf", commit=False)
    mock_config(project, "fast", commit=False)
    result = entry(input="2\n")
    assert result.exit_code == 0, result.output
    assert ("1. autohelix/fast.yaml, autohelix/perf.yaml, no runs yet: check and finish them with Claude Code"
            in flat(result.output))
    prompt = execs[0][1][-1]
    assert "Propose a short name for this goal" in prompt and "`autohelix/<name>.yaml`" in prompt
    assert "run NAME --check` from the project root" in prompt and "`autohelix run NAME` command" in prompt
    assert "Its configs: autohelix/fast.yaml (no runs yet), autohelix/perf.yaml (no runs yet)." in prompt


def flat_lines(result):
    """The output's lines joined with newlines, runs of spaces as one."""
    return "\n".join(re.sub(r" {2,}", " ", line) for line in lines(result))


# --- with runs ------------------------------------------------------------------


def test_with_runs_the_table_the_next_commands_and_ask_first(project, monkeypatch, execs, tty):
    agents(["claude"])(monkeypatch)
    mock_config(project)
    first = start_run(project, "--land", "manual")  # its commit waits for `autohelix land`
    result = entry(input="\n")
    assert result.exit_code == 0, result.output
    shown = lines(result)
    assert any(line.startswith("  run ") and " state " in line for line in shown)  # the dashboard's table
    assert any(re.fullmatch(rf"  {first}  finished · 1 commit to land +next: autohelix land {first}", line)
               for line in shown), shown
    assert " 1. Ask Claude Code about these runs it summarizes them, then answers questions" in flat_lines(result)
    assert " 2. Set up a new experiment with Claude Code it drafts a config with you" in flat_lines(result)
    [(file, argv, _)] = execs
    prompt = argv[-1]
    assert re.search(rf"^- {first} \(\d{{8}}-\d{{4}}-{first}, autohelix.yaml\): finished, last iteration 1, "
                     r"1 commit not landed$", prompt, re.M), prompt
    assert 'I picked "Ask Claude Code about these runs"' in prompt
    assert 'I may switch to the other option ("Set up a new experiment with Claude Code")' in prompt
    assert "Open with a short summary of the live runs plus the most recent one" in prompt
    cli_name = str(cli._launcher() or "autohelix")
    assert f"Send `{cli_name} hint` or `{cli_name} stop`" in prompt and "only when I ask" in prompt
    assert "The project's runs (live ones first, then by last activity;" in prompt


def test_a_crashed_run_reads_stopped_unexpectedly_and_resume_applies(project, monkeypatch, execs, tty):
    from autohelix.runs import project_runs, short_id, write_json

    agents(["claude"])(monkeypatch)
    mock_config(project)
    start_run(project)
    paths = project_runs(project)[0][0]
    status = paths.status
    data = __import__("json").loads(status.read_text())
    write_json(status, {**data, "state": "running", "pid": 999999})  # its lock is free: the process is gone
    result = entry(input="q\n")
    assert result.exit_code == 0, result.output
    short = short_id(paths.run_id)
    assert any(re.fullmatch(rf"  {short}  stopped unexpectedly · iteration 1 +next: autohelix resume {short}", line)
               for line in lines(result)), lines(result)


@pytest.mark.parametrize("state, commits, expected", [
    ("running", 0, ("running · iteration 4", "view")),
    ("running", 2, ("running · iteration 4", "view")),
    ("interrupted", 0, ("interrupted · iteration 3", "resume")),
    ("interrupted", 2, ("interrupted · iteration 3", "resume")),  # resuming is the point; auto landing lands
    ("crashed", 0, ("stopped unexpectedly · iteration 3", "resume")),
    ("crashed", 1, ("stopped unexpectedly · iteration 3", "resume")),
    ("finished", 2, ("finished · 2 commits to land", "land")),
    ("finished", 1, ("finished · 1 commit to land", "land")),
    ("finished", 0, None),
    ("failed", 0, ("failed · iteration 3", "resume")),
    ("failed", 2, ("failed · 2 commits to land", "land")),
    (None, 0, ("not started · iteration 3", "resume")),
])
def test_one_suggested_command_per_run(state, commits, expected):
    from autohelix import run_table

    run = {"id": "20261115-1402-sorting-krp3", "progress": {"iterations": 3},
           "status": {"state": state, "iteration": 4} if state else None,
           "target_branch": "main", "landing": {"commits": commits, "fast_forward": True}}
    found = run_table.suggestion(run)
    assert found == (expected and (expected[0], f"autohelix {expected[1]} krp3"))


def test_archived_runs_are_counted(project, monkeypatch, execs, tty):
    agents(["claude"])(monkeypatch)
    mock_config(project)
    first = start_run(project)
    second = start_run(project)
    assert CliRunner().invoke(main, ["archive", first]).exit_code == 0
    result = entry(input="q\n")
    assert "  1 archived · autohelix --help: all commands" in lines(result)
    assert CliRunner().invoke(main, ["archive", second]).exit_code == 0
    result = entry(input="\n")
    assert result.exit_code == 0, result.output
    assert "  no current runs, 2 archived · autohelix --help: all commands" in lines(result)  # not as none
    # Nothing current to ask about: asked as without runs (its config has runs: no "no runs yet").
    assert "Ask Claude Code" not in result.output and "no runs yet" not in result.output
    assert "Set up an experiment with Claude Code? Enter/q" in flat(result.output)
    prompt = execs[0][1][-1]
    assert "The project has no current runs, 2 archived" in prompt and "Its configs: autohelix.yaml." in prompt
    assert 'I picked "Set up an experiment with Claude Code"; I may ask anything else' in prompt
    assert "save the config as `autohelix/<name>.yaml`" in prompt


def test_runs_in_a_subfolder_are_found(project, monkeypatch, execs, tty):
    """From below a folder with runs, the project is that folder (as for `view`), not the top."""
    agents(["claude"])(monkeypatch)
    sub = project / "app"
    sub.mkdir()
    (sub / "code.py").write_text("x = 1\n")
    mock_config(sub)
    monkeypatch.chdir(sub)
    start_run(sub)
    (sub / "pkg").mkdir()
    monkeypatch.chdir(sub / "pkg")
    result = entry(input="\n")
    assert result.exit_code == 0, result.output
    assert lines(result)[1].endswith(f" · {sub.resolve()}")
    assert Path(execs[0][2]) == sub.resolve()


# --- agents ----------------------------------------------------------------------


def test_several_agents_say_an_agent_then_ask_which(project, monkeypatch, execs, tty):
    agents(["claude", "codex", "opencode", "mock"])(monkeypatch)
    mock_config(project)
    start_run(project)
    result = entry(input="\n2\n")
    assert result.exit_code == 0, result.output
    assert "1. Ask an agent about these runs" in flat(result.output)
    assert "2. Set up a new experiment with an agent" in flat(result.output)
    assert "  Which agent?\n  1. Claude Code\n  2. Codex\n  3. OpenCode\n  Choose (q: quit) [1]:" in "\n".join(lines(result))
    assert "Mock" not in result.output and "mock" not in flat(result.output).lower().replace("autohelix", "")
    assert execs[0][1][0] == "codex" and "`agent.cli: codex`" not in execs[0][1][-1]  # ask: no backend
    result = entry(input="2\nq\n")  # q at "Which agent?" quits too
    assert result.exit_code == 0 and len(execs) == 1


def test_no_agent_says_what_to_install(project, execs, tty):
    result = entry(input="\n")
    assert result.exit_code == 1
    assert "No coding agent found to start: install Claude Code (`claude`), Codex (`codex`) or OpenCode" in flat(
        result.output)
    assert "  ! " in click.unstyle(result.output) and not execs


@pytest.mark.parametrize("agent", ["codex", "opencode"])
def test_codex_and_opencode_argv(project, monkeypatch, execs, tty, agent):
    agents([agent])(monkeypatch)
    assert entry(input="\n").exit_code == 0
    [(file, argv, cwd)] = execs
    assert file == agent and Path(cwd) == project.resolve()
    if agent == "codex":
        assert len(argv) == 2
    else:
        assert argv[:3] == ["opencode", str(project.resolve()), "--prompt"] and len(argv) == 4
    assert f"with `agent.cli: {agent}` unless we decide otherwise" in argv[-1]


def test_claude_gets_the_skill_and_the_docs(project, monkeypatch, execs, tty):
    agents(["claude"])(monkeypatch)
    assert entry(input="\n").exit_code == 0
    [(_, argv, _)] = execs
    assert argv[:2] == ["claude", "--add-dir"] and argv[4] == "--"
    skill_dir, docs = Path(argv[2]), Path(argv[3])
    assert (skill_dir / "SKILL.md").is_file() and (docs / "concepts.md").is_file()
    prompt = argv[5]
    assert prompt.startswith(f"Read `{skill_dir / 'SKILL.md'}` and follow it")
    assert f"its user docs are in `{docs}`: read them when needed." in prompt
    launcher = cli._launcher()
    assert f"AutoHelix's CLI is `{launcher or 'autohelix'}` and its Python `{sys.executable}`" in prompt
    assert f"checked by `{launcher or 'autohelix'} run --check` from the project root" in prompt
    assert 'I picked "Set up an experiment with Claude Code"' in prompt


def test_the_prompt_names_the_launcher_it_was_started_as(project, monkeypatch, execs, tty, tmp_path):
    agents(["claude"])(monkeypatch)
    launcher = tmp_path / "venv" / "bin" / "autohelix"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/usr/bin/env python\n")
    monkeypatch.setattr(sys, "argv", [str(launcher)])
    assert entry(input="\n").exit_code == 0
    assert f"checked by `{launcher.resolve()} run --check` from the project root" in execs[0][1][-1]


def test_an_agent_that_cannot_start(project, monkeypatch, tty):
    agents(["claude"])(monkeypatch)

    def fail(file, argv):
        raise PermissionError(13, "Permission denied", file)

    monkeypatch.setattr(os, "execvp", fail)
    result = entry(input="\n")
    assert result.exit_code == 1
    assert "Could not start claude: [Errno 13] Permission denied" in result.output


def test_a_skill_not_on_disk_is_a_clear_error(project, monkeypatch, execs, tty):
    agents(["claude"])(monkeypatch)
    monkeypatch.setattr(cli, "pkg_files", lambda package: Path("/nonexistent/autohelix"))
    result = entry(input="\n")
    assert result.exit_code == 1
    assert "The bundled setup skill isn't a folder on disk" in result.output
    assert not execs


# --- git ---------------------------------------------------------------------------


def test_outside_git_says_so_and_offers_nothing(tmp_path, monkeypatch, execs, tty):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    agents(["claude"])(monkeypatch)
    result = entry(input="\n")
    assert result.exit_code == 1
    shown = lines(result)
    assert shown[3].startswith("  ! Not a git repository. AutoHelix requires git. A run refuses this folder")
    assert "    to make it a checkout: git init && git add -A && git commit -m 'init'" in shown
    assert "Claude Code" not in result.output and "Enter/q" not in result.output and not execs
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("where", ["bare", "git-dir"])
def test_a_folder_a_run_refuses_is_named_as_run_names_it(tmp_path, monkeypatch, execs, tty, where):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    agents(["claude"])(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", *(["--bare"] if where == "bare" else []))
    folder = repo if where == "bare" else repo / ".git"
    monkeypatch.chdir(folder)
    result = entry(input="\n")
    assert result.exit_code == 1 and not execs
    assert f"! {folder.resolve()} is not in a git working tree" in flat(result.output)
    result = CliRunner().invoke(main, ["run", "-p", str(folder)])
    assert result.exit_code == 1 and f"{folder.resolve()} is not in a git working tree" in flat(result.output)


def test_no_commit_yet_is_in_the_header_and_the_prompt(tmp_path, monkeypatch, execs, tty):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    agents(["claude"])(monkeypatch)
    git(tmp_path, "init", "-q")
    result = entry(input="\n")
    assert result.exit_code == 0, result.output
    assert "  Git        No commits yet; a run starts from a commit." in lines(result)
    prompt = execs[0][1][-1]
    assert ("Git: No commits yet; a run starts from a commit (`git add -A && git commit -m 'init'` fixes it). "
            "`autohelix run` refuses until that is fixed.") in prompt


def test_a_clean_checkout_has_no_git_field(project, monkeypatch, execs, tty):
    agents(["claude"])(monkeypatch)
    result = entry(input="q\n")
    assert not any(line.startswith("  Git ") for line in lines(result))
    assert "Git:" not in result.output


# --- `run` without a config ------------------------------------------------------------


def test_run_without_a_config_says_where_to_look(project):
    result = CliRunner().invoke(main, ["run"])
    assert result.exit_code == 1
    text = flat(result.output)
    assert "Write autohelix.yaml: run `autohelix` in a terminal to set it up with an agent" in text
    assert cli.EXAMPLES in text and "getting-started.md or config.md" in text
    assert str(cli.TEMPLATE) in text and Path(str(cli.TEMPLATE)).is_file()
    assert files(project) == ["main.py"]


def test_run_with_a_missing_name_says_where_to_look(project):
    mock_config(project)
    result = CliRunner().invoke(main, ["run", "perf"])
    assert result.exit_code == 1
    assert "Write autohelix/perf.yaml: run `autohelix` in a terminal" in flat(result.output)


def test_the_template_is_a_valid_config_once_it_has_a_goal(project):
    path = project / "autohelix.yaml"
    text = Path(str(cli.TEMPLATE)).read_text()
    assert not re.search(r"autohelix (setup|init)\b", text)  # removed commands
    for old in ("prompt.md", "-c "):
        assert old not in text
    path.write_text(text)
    config = load_config(project, config_file=path)
    errors = [i.message for i in config.validate() if i.level == "error"]
    assert len(errors) == 1 and "default placeholder" in errors[0]
    path.write_text(text.replace("Describe what you want the agent to optimize.", "Make main.py faster."))
    config = load_config(project, config_file=path)
    assert [i for i in config.validate() if i.level == "error"] == []
    assert config.title == "Make main.py faster."


# --- the docs in the package -------------------------------------------------------------


def test_docs_dir_prefers_the_packaged_copy(monkeypatch, tmp_path):
    assert (cli.docs_dir() / "concepts.md").is_file()  # this checkout's docs/ (an editable install)
    package = tmp_path / "autohelix"
    (package / "docs").mkdir(parents=True)
    monkeypatch.setattr(cli, "pkg_files", lambda name: package)
    assert cli.docs_dir() == (package / "docs").resolve()


def test_the_skill_points_at_the_docs():
    skill = (Path(cli.__file__).parent / "skills" / "autohelix-setup" / "SKILL.md").read_text()
    assert "The user docs are at the path the session's first\nprompt gives; read them when needed." in skill


@pytest.mark.skipif(not shutil.which("uv"), reason="needs uv")
def test_the_wheel_and_the_sdist_ship_the_docs(tmp_path):
    repo = Path(__file__).resolve().parent.parent
    result = subprocess.run(["uv", "build", "--quiet", "--out-dir", str(tmp_path)], cwd=repo,
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    [wheel] = tmp_path.glob("*.whl")  # built from the sdist, as pip and uv do
    names = zipfile.ZipFile(wheel).namelist()
    assert "autohelix/docs/concepts.md" in names and "autohelix/docs/getting-started.md" in names
    [sdist] = tmp_path.glob("*.tar.gz")
    import tarfile

    assert any(name.endswith("/docs/concepts.md") for name in tarfile.open(sdist).getnames())


def test_the_agent_session_uses_the_runs_variables(project, monkeypatch, execs, tty):
    """The entry screen finds and starts each CLI as a run does: `$AUTOHELIX_<CLI>_CMD`,
    else the name on PATH."""
    agents(["/opt/claude-bedrock"])(monkeypatch)
    monkeypatch.setenv("AUTOHELIX_CLAUDE_CODE_CMD", "/opt/claude-bedrock")
    assert entry(input="\n").exit_code == 0
    [(file, argv, _)] = execs
    assert file == argv[0] == "/opt/claude-bedrock" and argv[1] == "--add-dir"


def test_the_old_claude_variable_refuses_the_session(project, monkeypatch, execs, tty):
    agents(["claude"])(monkeypatch)
    monkeypatch.setenv("AUTOHELIX_CLAUDE_CMD", "/opt/claude")
    result = entry(input="\n")
    assert result.exit_code == 1 and not execs
    assert "AUTOHELIX_CLAUDE_CMD is now AUTOHELIX_CLAUDE_CODE_CMD" in flat(result.output)
