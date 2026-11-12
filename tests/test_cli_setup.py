"""`autohelix setup [NAME]`: the template, or an agent session with the bundled skill."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from autohelix.cli import CONFIG_TEMPLATE, main
from autohelix.config import load_config


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", *args], cwd=cwd,
                   capture_output=True, check=True)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A git checkout with a commit, the current directory, no agent installed, no TTY."""
    path = tmp_path / "project"
    path.mkdir()
    git(path, "init", "-q")
    (path / "main.py").write_text("x = 1\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    monkeypatch.chdir(path)  # restored afterwards, also after setup's chdir before exec
    agents([])(monkeypatch)
    monkeypatch.setattr("autohelix.cli._interactive", lambda: False)
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
    assert "Commit it." in result.output and "  3. autohelix run\n" in result.output
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
    assert f"autohelix run -p {other}" in result.output


@pytest.mark.parametrize("name", ["Perf", "perf_fast", "a" * 33, "", "perf.yaml", "x/y"])
def test_a_bad_name_is_a_usage_error(project, name):
    result = setup(name)
    assert result.exit_code == 2
    assert "lowercase letters, digits or hyphens, at most 32 characters" in result.output
    assert files(project) == ["main.py"]


@pytest.mark.parametrize("args, relative", [((), "autohelix.yaml"), (("perf",), "autohelix/perf.yaml")])
def test_never_overwrites(project, args, relative):
    (project / relative).parent.mkdir(exist_ok=True)
    (project / relative).write_text("goal: mine\n")
    result = setup(*args)
    assert result.exit_code == 1
    assert f"{relative} already exists" in result.output
    assert (project / relative).read_text() == "goal: mine\n"


def test_bare_setup_beside_folder_configs_is_refused(project):
    (project / "autohelix").mkdir()
    (project / "autohelix" / "perf.yaml").write_text("goal: x\n")
    result = setup()
    assert result.exit_code == 1
    assert "autohelix/ already has perf.yaml" in result.output
    assert "Name this one: `autohelix setup <name>`." in " ".join(result.output.split())
    assert not (project / "autohelix.yaml").exists()
    assert setup("fast").exit_code == 0


def test_outside_git_writes_and_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("autohelix.cli._interactive", lambda: False)
    result = setup()
    assert result.exit_code == 0, result.output
    assert (tmp_path / "autohelix.yaml").read_text() == CONFIG_TEMPLATE
    assert "not in a git repository" in result.output
    assert "git init && git add -A && git commit -m 'init'" in result.output


def test_init_is_a_hidden_alias(project):
    help_text = CliRunner().invoke(main, ["--help"]).output
    assert "setup" in help_text and " init " not in help_text
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
    assert prompt.endswith("Start by asking what I want to achieve.")


def test_agent_codex_and_opencode_argv(project, monkeypatch, execs):
    agents(["codex", "opencode"])(monkeypatch)
    assert setup("--agent", "codex").exit_code == 0
    assert setup("--agent", "opencode").exit_code == 0
    (_, codex, _), (_, opencode, _) = execs
    assert codex[0] == "codex" and len(codex) == 2
    assert "Write the config to `autohelix.yaml`, with `agent.backend: codex`" in codex[1]
    assert opencode[:3] == ["opencode", str(project.resolve()), "--prompt"]
    assert "`agent.backend: opencode`" in opencode[3]


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
    monkeypatch.setattr("autohelix.cli._interactive", lambda: True)


def test_one_agent_asks_once_and_the_default_starts_it(project, monkeypatch, execs, tty):
    agents(["codex"])(monkeypatch)
    result = setup(input="\n")
    assert result.exit_code == 0, result.output
    assert ("Set up the loop with an agent (Codex found), or write an empty config? "
            "(agent, empty) [agent]:") in result.output
    assert "Which agent?" not in result.output
    assert [argv[0] for _, argv, _ in execs] == ["codex"]
    assert files(project) == ["main.py"]


def test_several_agents_ask_which_defaulting_to_the_first_found(project, monkeypatch, execs, tty):
    agents(["codex", "opencode"])(monkeypatch)
    result = setup(input="agent\n\n")
    assert result.exit_code == 0, result.output
    assert "(Codex, OpenCode found)" in result.output
    assert "Which agent? (codex, opencode) [codex]:" in result.output
    assert execs[0][1][0] == "codex"
    result = setup(input="\nopencode\n")
    assert result.exit_code == 0, result.output
    assert execs[1][1][0] == "opencode"


def test_choosing_empty_writes_the_template(project, monkeypatch, execs, tty):
    agents(["claude", "codex"])(monkeypatch)
    result = setup("perf", input="empty\n")
    assert result.exit_code == 0, result.output
    assert "Which agent?" not in result.output
    assert not execs
    assert (project / "autohelix" / "perf.yaml").read_text() == CONFIG_TEMPLATE
    assert "`autohelix setup perf --agent claude`" in result.output


def test_no_agent_writes_the_template_without_asking(project, execs, tty):
    result = setup(input="")
    assert result.exit_code == 0, result.output
    assert "Set up the loop" not in result.output
    assert (project / "autohelix.yaml").read_text() == CONFIG_TEMPLATE
    assert "No coding agent found" in result.output
    assert not execs


def test_agent_flag_skips_the_questions(project, monkeypatch, execs, tty):
    agents(["claude", "codex"])(monkeypatch)
    result = setup("--agent", "codex", input="")
    assert result.exit_code == 0, result.output
    assert "Set up the loop" not in result.output
    assert execs[0][1][0] == "codex"
