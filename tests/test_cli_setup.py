"""`autohelix setup [NAME]`: the template, or an agent session with the bundled skill."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import click
import click.testing
import pytest
from click.testing import CliRunner

from autohelix import cli
from autohelix.cli import CONFIG_TEMPLATE, main
from autohelix.config import load_config


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", *args], cwd=cwd,
                   capture_output=True, check=True)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A git checkout with a commit, the current directory, no agent installed, no TTY
    (CliRunner's streams aren't terminals)."""
    path = tmp_path / "project"
    path.mkdir()
    git(path, "init", "-q")
    (path / "main.py").write_text("x = 1\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    monkeypatch.chdir(path)  # restored afterwards, also after setup's chdir before exec
    agents([])(monkeypatch)
    return path


def agents(names):
    def install(monkeypatch):
        real = shutil.which
        monkeypatch.setattr(shutil, "which",
                            lambda cmd, *a, **k: f"/usr/bin/{cmd}" if cmd in names else
                            None if cmd in ("claude", "codex", "opencode") else real(cmd, *a, **k))
    return install


@pytest.fixture
def execs(monkeypatch):
    """os.execvp, recorded instead of replacing the test process."""
    calls = []
    monkeypatch.setattr(os, "execvp", lambda file, argv: calls.append((file, list(argv), os.getcwd())))
    return calls


def setup(*args, input=None):
    return CliRunner().invoke(main, ["setup", *args], input=input)


def flat(text):
    """Output without the line wraps of a narrow terminal."""
    return " ".join(text.split())


def files(project):
    return sorted(str(p.relative_to(project)) for p in project.rglob("*") if ".git" not in p.parts)


# --- the template -----------------------------------------------------------


def test_bare_setup_writes_the_template_and_nothing_else(project):
    result = setup()
    assert result.exit_code == 0, result.output
    assert (project / "autohelix.yaml").read_text() == CONFIG_TEMPLATE
    assert files(project) == ["autohelix.yaml", "main.py"]  # no .autohelix/, no excludes
    exclude = project / ".git" / "info" / "exclude"
    assert ".autohelix/" not in (exclude.read_text() if exclude.exists() else "")
    assert "Wrote autohelix.yaml." in result.output
    assert "  2. Commit it, and any scripts its commands use (runs start from a commit).\n" in result.output
    assert "  3. autohelix run\n" in result.output
    assert "initialized" not in result.output.lower()
    assert "No coding agent found" in result.output


def test_named_setup_writes_into_the_folder(project):
    result = setup("perf")
    assert result.exit_code == 0, result.output
    assert (project / "autohelix" / "perf.yaml").read_text() == CONFIG_TEMPLATE
    assert files(project) == ["autohelix", "autohelix/perf.yaml", "main.py"]
    assert "  3. autohelix run perf\n" in result.output


def test_path_option_names_the_project(project, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    result = setup("-p", str(other))
    assert result.exit_code == 0, result.output
    assert (other / "autohelix.yaml").is_file() and not (project / "autohelix.yaml").exists()
    assert f"Wrote {other.resolve() / 'autohelix.yaml'}." in result.output  # openable from the cwd
    assert f"autohelix run -p {other}" in result.output


def test_relative_and_quoted_paths(project):
    (project / "sub").mkdir()
    result = setup("perf", "-p", "sub")
    assert result.exit_code == 0, result.output
    assert "Wrote sub/autohelix/perf.yaml." in result.output
    assert "autohelix run perf -p sub\n" in result.output
    (project / "my dir").mkdir()
    result = setup("-p", "my dir")
    assert result.exit_code == 0, result.output
    assert "Wrote my dir/autohelix.yaml." in result.output
    assert "autohelix run -p 'my dir'\n" in result.output


@pytest.mark.parametrize("name", ["Perf", "perf_fast", "a" * 33, "", "perf.yaml", "x/y"])
def test_a_bad_name_is_a_usage_error(project, name):
    result = setup(name)
    assert result.exit_code == 2
    assert (f"Error: Invalid value for NAME: {name!r} must start with a lowercase letter or digit and use only "
            "lowercase letters, digits or hyphens, at most 32 characters (runs of this config are named "
            "after it).") in flat(result.output)
    assert files(project) == ["main.py"]


@pytest.mark.parametrize("command", ["init", "setup"])
def test_a_folder_as_name_points_at_path(project, command):
    (project / "My App").mkdir()
    (project / "other").mkdir()  # a valid name, but `init PATH` by habit
    for name, quoted in ((".", "."), ("My App", "'My App'"), ("other", "other")):
        result = CliRunner().invoke(main, [command, name])
        assert result.exit_code == 2
        assert (f"Error: Invalid value for NAME: {name!r} is a folder; to set up there, use `-p {quoted}`."
                in flat(result.output))
    assert files(project) == ["My App", "main.py", "other"]
    # With -p, NAME is a name whatever the current directory holds.
    assert CliRunner().invoke(main, [command, "other", "-p", "."]).exit_code == 0
    assert (project / "autohelix" / "other.yaml").is_file()


def test_the_name_autohelix_is_refused(project):
    (project / "autohelix").mkdir()  # the config folder: reserved, not "a folder"
    result = setup("autohelix")
    assert result.exit_code == 2
    assert ("Error: Invalid value for NAME: 'autohelix' is reserved: runs of autohelix/autohelix.yaml would get "
            "no name; choose another.") in flat(result.output)
    assert files(project) == ["autohelix", "main.py"]


@pytest.mark.parametrize("args, relative", [((), "autohelix.yaml"), (("perf",), "autohelix/perf.yaml")])
def test_never_overwrites(project, args, relative):
    (project / relative).parent.mkdir(exist_ok=True)
    (project / relative).write_text("goal: mine\n")
    result = setup(*args)
    assert result.exit_code == 1
    assert f"{relative} already exists" in result.output
    assert (project / relative).read_text() == "goal: mine\n"


@pytest.mark.parametrize("agent", [None, "claude"])
def test_a_folder_in_place_of_the_config_is_refused(project, monkeypatch, execs, agent):
    agents(["claude"])(monkeypatch)
    (project / "autohelix.yaml").mkdir()
    result = setup(*(["--agent", agent] if agent else []))
    assert result.exit_code == 1
    assert "autohelix.yaml is a folder, not a config file; move it away first." in result.output
    assert not execs


@pytest.mark.parametrize("agent", [None, "claude"])
def test_a_file_in_place_of_the_folder_is_refused(project, monkeypatch, execs, agent):
    agents(["claude"])(monkeypatch)
    (project / "autohelix").write_text("not a folder\n")
    result = setup("perf", *(["--agent", agent] if agent else []))
    assert result.exit_code == 1
    assert "autohelix is a file, not a folder of configs; move it away first." in result.output
    assert "Errno" not in result.output
    assert (project / "autohelix").read_text() == "not a folder\n" and not execs


def test_symlinked_configs(project, monkeypatch, execs):
    agents(["claude"])(monkeypatch)
    (project / "real.yaml").write_text("goal: mine\n")
    (project / "autohelix.yaml").symlink_to("real.yaml")
    result = setup()  # a link to a file is a config: not overwritten
    assert result.exit_code == 1 and "autohelix.yaml already exists" in result.output
    assert setup("--agent", "claude").exit_code == 0
    assert "The config is `autohelix.yaml`, already there" in execs[0][1][4]
    (project / "autohelix").mkdir()
    (project / "autohelix" / "perf.yaml").symlink_to("gone.yaml")
    result = setup("perf", "--agent", "claude")
    assert result.exit_code == 1
    assert "autohelix/perf.yaml is a broken link, not a config file" in result.output
    assert len(execs) == 1 and (project / "real.yaml").read_text() == "goal: mine\n"


def test_bare_setup_beside_folder_configs_is_refused(project):
    (project / "autohelix").mkdir()
    (project / "autohelix" / "perf.yaml").write_text("goal: x\n")
    result = setup()
    assert result.exit_code == 1
    assert "autohelix/ already has perf.yaml" in result.output
    assert "Name this one: `autohelix setup <name>`." in flat(result.output)
    assert not (project / "autohelix.yaml").exists()
    assert setup("fast").exit_code == 0


def test_the_shadowing_hint_keeps_the_path(project, tmp_path):
    other = tmp_path / "my other"
    (other / "autohelix").mkdir(parents=True)
    (other / "autohelix" / "perf.yaml").write_text("goal: x\n")
    result = setup("-p", str(other))
    assert result.exit_code == 1
    assert f"Name this one: `autohelix setup <name> -p '{other}'`." in flat(result.output)


def test_outside_git_writes_and_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    result = setup()
    assert result.exit_code == 0, result.output
    assert (tmp_path / "autohelix.yaml").read_text() == CONFIG_TEMPLATE
    assert ("Warning: Not a git repository. AutoHelix requires git.\n"
            "Run: git init && git add -A && git commit -m 'init'") in result.output


def test_a_repository_without_commits_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    git(tmp_path, "init", "-q")
    result = setup()
    assert result.exit_code == 0, result.output
    assert (tmp_path / "autohelix.yaml").read_text() == CONFIG_TEMPLATE
    assert ("Warning: No commits yet; a run starts from a commit.\n"
            "Run: git add -A && git commit -m 'init'") in result.output


@pytest.mark.parametrize("where", ["bare", "git-dir"])
def test_a_folder_run_refuses_is_named_as_run_names_it(tmp_path, monkeypatch, where):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", *(["--bare"] if where == "bare" else []))
    folder = repo if where == "bare" else repo / ".git"
    result = setup("-p", str(folder))
    assert result.exit_code == 0, result.output
    assert f"Warning: {folder.resolve()} is not in a git working tree" in flat(result.output)
    result = CliRunner().invoke(main, ["run", "-p", str(folder)])
    assert result.exit_code == 1 and f"{folder.resolve()} is not in a git working tree" in flat(result.output)


def test_a_repository_with_a_commit_has_no_warning(project):
    assert "Warning" not in setup().output


def test_init_is_a_hidden_alias(project):
    listed = [line.split()[0] for line in CliRunner().invoke(main, ["--help"]).output.splitlines()
              if line.startswith("  ") and line.strip()]
    assert "setup" in listed and "init" not in listed
    result = CliRunner().invoke(main, ["init", "perf"])
    assert result.exit_code == 0, result.output
    assert (project / "autohelix" / "perf.yaml").read_text() == CONFIG_TEMPLATE


def test_without_a_tty_writes_the_template_even_with_agents(project, monkeypatch, execs):
    agents(["claude", "codex"])(monkeypatch)
    result = setup()
    assert result.exit_code == 0, result.output
    assert (project / "autohelix.yaml").read_text() == CONFIG_TEMPLATE
    assert not execs
    assert "`autohelix setup --agent claude`" in result.output
    assert "`autohelix skill install --agent claude`" in result.output


def test_the_template_is_a_valid_v2_config_once_it_has_a_goal(project):
    assert setup("perf").exit_code == 0
    path = project / "autohelix" / "perf.yaml"
    for text in ("autohelix init", "prompt.md", "-c "):
        assert text not in CONFIG_TEMPLATE
    config, raw = load_config(project, config_file=path)
    errors = [i.message for i in config.validate(raw) if i.level == "error"]
    assert len(errors) == 1 and "default placeholder" in errors[0]
    path.write_text(CONFIG_TEMPLATE.replace("Describe what you want the agent to optimize.", "Make main.py faster."))
    config, raw = load_config(project, config_file=path)
    assert [i for i in config.validate(raw) if i.level == "error"] == []
    assert not [i for i in config.validate(raw) if "Unknown config key" in i.message]
    assert config.title == "Make main.py faster."
    assert config.editable == ["src/"] and config.max_iterations == 5


def test_run_finds_the_named_config(project, monkeypatch):
    assert setup("perf").exit_code == 0
    path = project / "autohelix" / "perf.yaml"
    path.write_text(path.read_text().replace("Describe what you want the agent to optimize.", "Faster."))
    git(project, "add", "-A")
    git(project, "commit", "-qm", "config")
    seen = {}

    class Harness:
        def __init__(self, project_path, config_file=None, **selection):
            seen["config"] = config_file

        def run(self, max_iterations=None, report=True):
            pass

    monkeypatch.setattr("autohelix.cli.Harness", Harness)
    result = CliRunner().invoke(main, ["run", "perf"])
    assert result.exit_code == 0, result.output
    assert seen["config"] == path


def test_run_with_a_missing_name_suggests_setup(project):
    (project / "autohelix.yaml").write_text("goal: x\n")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "config")
    result = CliRunner().invoke(main, ["run", "perf"])
    assert result.exit_code == 1
    assert "Write it with `autohelix setup perf`." in " ".join(result.output.split())


# --- an agent ---------------------------------------------------------------


def test_agent_claude_execs_with_the_skill_and_writes_nothing(project, monkeypatch, execs):
    agents(["claude"])(monkeypatch)
    result = setup("perf", "--agent", "claude")
    assert result.exit_code == 0, result.output
    assert "Starting Claude Code with the AutoHelix setup skill" in result.output
    assert files(project) == ["main.py"]
    [(file, argv, cwd)] = execs
    assert file == "claude" and Path(cwd) == project.resolve()
    assert argv[:2] == ["claude", "--add-dir"] and argv[3] == "--"
    skill_dir = Path(argv[2])
    assert (skill_dir / "SKILL.md").is_file()
    prompt = argv[4]
    assert f"Read `{skill_dir / 'SKILL.md'}` and follow it" in prompt
    assert "Write the config to `autohelix/perf.yaml`" in prompt
    assert "`agent.backend: claude`" in prompt
    assert "Write the file directly (don't run `autohelix setup`)." in prompt
    assert f"To check it, use AutoHelix's own Python, `{sys.executable}`" in prompt
    assert prompt.endswith("Start by asking what I want to achieve.")


def test_the_prompt_names_the_launcher_it_was_started_as(project, monkeypatch, execs, tmp_path):
    agents(["claude"])(monkeypatch)
    launcher = tmp_path / "venv" / "bin" / "autohelix"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/usr/bin/env python\n")
    monkeypatch.setattr(sys, "argv", [str(launcher), "setup"])
    assert setup("--agent", "claude").exit_code == 0
    assert f"`{sys.executable}` (its CLI is `{launcher.resolve()}`)." in execs[0][1][4]


@pytest.mark.parametrize("agent", ["codex", "opencode"])
def test_agent_codex_and_opencode_argv(project, monkeypatch, execs, agent):
    agents(["codex", "opencode"])(monkeypatch)
    result = setup("--agent", agent)
    assert result.exit_code == 0, result.output
    [(file, argv, cwd)] = execs
    assert file == agent and Path(cwd) == project.resolve()
    assert files(project) == ["main.py"]
    if agent == "codex":
        assert len(argv) == 2
    else:
        assert argv[:3] == ["opencode", str(project.resolve()), "--prompt"] and len(argv) == 4
    assert f"Write the config to `autohelix.yaml`, with `agent.backend: {agent}`" in argv[-1]


def test_agent_with_path_starts_in_the_project(project, monkeypatch, execs, tmp_path):
    agents(["claude"])(monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    result = setup("perf", "-p", str(other), "--agent", "claude")
    assert result.exit_code == 0, result.output
    [(_, argv, cwd)] = execs
    assert Path(cwd) == other.resolve()
    assert "Write the config to `autohelix/perf.yaml`" in argv[4]  # relative to the agent's cwd
    assert files(other) == [] and files(project) == ["main.py"]


def test_agent_that_cannot_start(project, monkeypatch):
    agents(["claude"])(monkeypatch)

    def fail(file, argv):
        raise PermissionError(13, "Permission denied", file)

    monkeypatch.setattr(os, "execvp", fail)
    result = setup("--agent", "claude")
    assert result.exit_code == 1
    assert "Could not start claude: [Errno 13] Permission denied" in result.output
    assert files(project) == ["main.py"]


def test_a_skill_not_on_disk_is_a_clear_error(project, monkeypatch, execs):
    agents(["claude"])(monkeypatch)
    monkeypatch.setattr(cli, "pkg_files", lambda package: Path("/nonexistent/autohelix"))
    result = setup("--agent", "claude")
    assert result.exit_code == 1
    assert "The bundled setup skill isn't a folder on disk" in result.output
    assert not execs and files(project) == ["main.py"]


def test_agent_on_an_existing_config_completes_it(project, monkeypatch, execs):
    agents(["claude"])(monkeypatch)
    assert setup().exit_code == 0  # the template
    result = setup("--agent", "claude")
    assert result.exit_code == 0, result.output
    assert "The config is `autohelix.yaml`, already there" in execs[0][1][4]
    assert (project / "autohelix.yaml").read_text() == CONFIG_TEMPLATE


def test_agent_not_on_path(project, monkeypatch, execs):
    agents(["claude"])(monkeypatch)
    result = setup("--agent", "codex")
    assert result.exit_code == 1
    assert "--agent codex: Codex (`codex`) is not on PATH. Found: claude." in result.output
    assert not execs and files(project) == ["main.py"]


# --- the front door ---------------------------------------------------------


@pytest.fixture
def tty(monkeypatch):
    """CliRunner's stdin and stdout report a terminal, so the real `_interactive` says yes."""
    monkeypatch.setattr(click.testing._NamedTextIOWrapper, "isatty", lambda self: True, raising=False)


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


@pytest.mark.parametrize("several, at", [(False, "first"), (True, "first"), (True, "second")])
@pytest.mark.parametrize("how", ["eof", "ctrl-c"])
def test_leaving_a_question_writes_nothing(project, monkeypatch, execs, tty, several, at, how):
    agents(["claude", "codex"] if several else ["claude"])(monkeypatch)
    answers = [1] if at == "second" else []
    if how == "eof":
        result = setup(input="".join(f"{a}\n" for a in answers))
    else:
        def prompt(*args, **kwargs):
            if answers:
                return answers.pop(0)
            raise KeyboardInterrupt

        monkeypatch.setattr(click, "prompt", prompt)
        result = setup()
    assert result.exit_code == 1
    assert "Aborted!" in result.output
    assert not execs and files(project) == ["main.py"]


def test_one_agent_asks_once_and_the_default_starts_it(project, monkeypatch, execs, tty):
    agents(["codex"])(monkeypatch)
    result = setup(input="\n")
    assert result.exit_code == 0, result.output
    assert "AutoHelix setup · autohelix.yaml" in result.output
    assert "How do you want to write it?\n  1. With Codex  it asks what you want" in result.output
    assert "  2. Empty config  the commented template, to edit by hand\nChoose [1]:" in result.output
    assert "Which agent?" not in result.output
    assert [argv[0] for _, argv, _ in execs] == ["codex"]
    assert files(project) == ["main.py"]


def test_several_agents_ask_which_defaulting_to_the_first_found(project, monkeypatch, execs, tty):
    agents(["codex", "opencode"])(monkeypatch)
    result = setup(input="1\n\n")
    assert result.exit_code == 0, result.output
    assert "  1. With an agent" in result.output
    assert "Which agent?\n  1. Codex\n  2. OpenCode\nChoose [1]:" in result.output
    assert execs[0][1][0] == "codex"
    result = setup(input="\n2\n")
    assert result.exit_code == 0, result.output
    assert execs[1][1][0] == "opencode"


def test_choosing_empty_writes_the_template(project, monkeypatch, execs, tty):
    agents(["claude", "codex"])(monkeypatch)
    result = setup("perf", input="2\n")
    assert result.exit_code == 0, result.output
    assert "Which agent?" not in result.output
    assert not execs
    assert (project / "autohelix" / "perf.yaml").read_text() == CONFIG_TEMPLATE
    assert "`autohelix setup perf --agent claude`" in result.output


def test_no_agent_writes_the_template_without_asking(project, execs, tty):
    result = setup(input="")
    assert result.exit_code == 0, result.output
    assert "How do you want" not in result.output
    assert (project / "autohelix.yaml").read_text() == CONFIG_TEMPLATE
    assert "No coding agent found" in result.output
    assert not execs


def test_agent_flag_skips_the_questions(project, monkeypatch, execs, tty):
    agents(["claude", "codex"])(monkeypatch)
    result = setup("--agent", "codex", input="")
    assert result.exit_code == 0, result.output
    assert "How do you want" not in result.output
    assert execs[0][1][0] == "codex"
