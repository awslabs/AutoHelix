"""Tests for the `autohelix init` CLI command."""

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from autohelix.cli import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def git_project(tmp_path):
    """A project with git initialized."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    (tmp_path / "main.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    return tmp_path


class TestInitCommand:
    def test_creates_autohelix_dir(self, runner, git_project):
        result = runner.invoke(main, ["init", str(git_project)])
        assert result.exit_code == 0, result.output
        assert (git_project / ".autohelix").is_dir()
        assert (git_project / ".autohelix" / "notes").is_dir()

    def test_creates_config_template(self, runner, git_project):
        result = runner.invoke(main, ["init", str(git_project)])
        assert result.exit_code == 0, result.output
        config_path = git_project / "autohelix.yaml"
        assert config_path.exists()
        content = config_path.read_text()
        assert "goal:" in content
        assert "Describe what you want" in content

    def test_creates_prompt_template(self, runner, git_project):
        result = runner.invoke(main, ["init", str(git_project)])
        assert result.exit_code == 0, result.output
        prompt_path = git_project / ".autohelix" / "prompt.md"
        assert prompt_path.exists()
        assert "{{ goal }}" in prompt_path.read_text()

    def test_gitignores_autohelix_dir(self, runner, git_project):
        result = runner.invoke(main, ["init", str(git_project)])
        assert result.exit_code == 0, result.output
        gitignore = git_project / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text()
        assert ".autohelix/" in content
        assert "__pycache__/" in content

    def test_already_initialized(self, runner, git_project):
        (git_project / ".autohelix").mkdir()
        result = runner.invoke(main, ["init", str(git_project)])
        assert result.exit_code == 0
        assert "Already initialized" in result.output

    def test_no_git_fails(self, runner, tmp_path):
        result = runner.invoke(main, ["init", str(tmp_path)])
        assert result.exit_code == 1
        assert "git" in result.output.lower()

    def test_preserves_existing_config(self, runner, git_project):
        config_path = git_project / "autohelix.yaml"
        config_path.write_text("goal: existing goal\n")

        result = runner.invoke(main, ["init", str(git_project)])
        assert result.exit_code == 0, result.output
        assert config_path.read_text() == "goal: existing goal\n"

    def test_prints_next_steps(self, runner, git_project):
        result = runner.invoke(main, ["init", str(git_project)])
        assert result.exit_code == 0
        assert "autohelix run" in result.output
