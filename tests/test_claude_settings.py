"""A user `--settings` in Claude Code's extra_args is merged with AutoHelix's own (the
time-left hooks) into one file: Claude Code uses only the last `--settings`, as a whole."""

import json
import subprocess

import pytest

from autohelix.agents.claudecode import merged_settings, split_settings
from autohelix.config import ConfigError
from tests.helpers import write_config

HOOK = {"matcher": "*", "hooks": [{"type": "command", "command": "/bin/time_left_hook"}]}


@pytest.mark.parametrize(("args", "value", "rest"), [
    (["--settings", "s.json", "--x"], "s.json", ["--x"]),
    (["--x", "--settings={\"model\": \"m\"}"], '{"model": "m"}', ["--x"]),
    (["--settings", "a.json", "--settings", "b.json"], "b.json", []),
    (["--x"], None, ["--x"]),
])
def test_split_settings(args, value, rest):
    assert split_settings(args) == (value, rest)


def test_a_settings_flag_without_a_value_is_a_config_error():
    with pytest.raises(ConfigError, match="--settings has no value"):
        split_settings(["--settings"])


def test_the_users_hooks_and_autohelix_hooks_combine(tmp_path):
    user = {"model": "m", "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": []}], "Stop": [{"hooks": []}]}}
    (tmp_path / "s.json").write_text(json.dumps(user))
    merged = merged_settings("s.json", {"PostToolUse": [HOOK], "UserPromptSubmit": [HOOK]}, tmp_path)
    assert merged["model"] == "m"
    assert merged["hooks"] == {"PostToolUse": [{"matcher": "Bash", "hooks": []}, HOOK],
                               "Stop": [{"hooks": []}], "UserPromptSubmit": [HOOK]}


def test_a_json_string_value(tmp_path):
    assert merged_settings('{"env": {"A": "1"}}', {"PostToolUse": [HOOK]}, tmp_path) == {
        "env": {"A": "1"}, "hooks": {"PostToolUse": [HOOK]}}


@pytest.mark.parametrize("value", ["missing.json", "{not json", "[1]", '{"hooks": []}'])
def test_a_value_that_cant_be_read_is_a_config_error_naming_it(tmp_path, value):
    with pytest.raises(ConfigError, match="--settings " + value.replace("[", r"\[").replace("{", r"\{")):
        merged_settings(value, {"PostToolUse": [HOOK]}, tmp_path)


def _project(tmp_path, extra_args, *, timeout=True):
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True, capture_output=True)
    write_config(project, f"""
goal: test
agent:
  cli: claude-code
  {"timeout: 1h" if timeout else ""}
  extra_args: {json.dumps(extra_args)}
gates:
  - "true"
""")
    (project / "code.py").write_text("print(1)\n")
    for args in (["config", "user.email", "test@example.com"], ["config", "user.name", "Test"],
                 ["add", "code.py"], ["commit", "-qm", "initial code"]):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    return project


@pytest.fixture
def claude(monkeypatch):
    monkeypatch.setenv("AUTOHELIX_CLAUDE_CODE_CMD", "/usr/bin/claude")
    monkeypatch.setattr("autohelix.checks.shutil.which", lambda command: command)


def test_the_agent_gets_one_merged_settings_file(tmp_path, monkeypatch, claude):
    from autohelix.harness import Harness

    user = tmp_path / "user-settings.json"
    user.write_text(json.dumps({"model": "m", "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": []}]}}))
    project = _project(tmp_path, ["--settings", str(user), "--disallowedTools", "WebFetch"])
    harness = Harness(project)
    try:
        settings = json.loads(harness._claude_settings_path.read_text())
        assert settings["model"] == "m"
        post = settings["hooks"]["PostToolUse"]
        assert post[0] == {"matcher": "Bash", "hooks": []} and post[1]["hooks"][0]["command"].endswith(
            "claude_hook_time_left.sh")
        assert "UserPromptSubmit" in settings["hooks"]
        assert harness._claude_settings_path.stat().st_mode & 0o777 == 0o600
        monkeypatch.setenv("AUTOHELIX_CLAUDE_SETTINGS", str(harness._claude_settings_path))
        cmd = harness.agent._build_command("p")
        assert cmd.count("--settings") == 1
        assert cmd[cmd.index("--settings") + 1] == str(harness._claude_settings_path)
        assert cmd[-4:] == ["--disallowedTools", "WebFetch", "--", "p"]
    finally:
        harness.close()


def test_without_a_timeout_the_users_settings_still_come_through_the_one_file(tmp_path, claude):
    from autohelix.harness import Harness

    project = _project(tmp_path, ['--settings={"model": "m"}'], timeout=False)
    harness = Harness(project)
    try:
        assert json.loads(harness._claude_settings_path.read_text()) == {"model": "m"}
        assert harness.agent.config.extra_args == []
    finally:
        harness.close()


def test_an_unreadable_user_settings_file_refuses_the_run(tmp_path, claude):
    from autohelix.harness import Harness

    project = _project(tmp_path, ["--settings", "nowhere.json"])
    with pytest.raises(ConfigError, match="--settings nowhere.json can't be read"):
        Harness(project, check_only=True)
    assert not (project / ".autohelix").exists()
