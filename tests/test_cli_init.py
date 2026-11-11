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
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "main.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    return tmp_path


class TestInitCommand:
    def test_creates_autohelix_dir(self, runner, git_project):
        result = runner.invoke(main, ["init", str(git_project)])
        assert result.exit_code == 0, result.output
        assert (git_project / ".autohelix").is_dir()
        assert (git_project / ".autohelix" / "prompt.md").is_file()

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

    def test_locally_excludes_autohelix_and_pycache(self, runner, git_project):
        result = runner.invoke(main, ["init", str(git_project)])
        assert result.exit_code == 0, result.output
        assert not (git_project / ".gitignore").exists()
        content = (git_project / ".git" / "info" / "exclude").read_text()
        assert ".autohelix/" in content
        assert "__pycache__/" in content
        cache_dir = git_project / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "main.pyc").write_bytes(b"cache")
        ignored = subprocess.run(
            ["git", "check-ignore", ".autohelix/prompt.md", "__pycache__/main.pyc"],
            cwd=git_project, capture_output=True, text=True, check=True,
        )
        assert ignored.stdout.splitlines() == [
            ".autohelix/prompt.md", "__pycache__/main.pyc",
        ]

    def test_already_initialized(self, runner, git_project):
        (git_project / ".autohelix").mkdir()
        result = runner.invoke(main, ["init", str(git_project)])
        assert result.exit_code == 0
        assert "Already initialized" in result.output

    @pytest.mark.parametrize("negation", ["!.*", "!*"])
    @pytest.mark.parametrize("already_initialized", [False, True])
    def test_conflicting_ignore_refuses_without_creating_files(
        self, runner, git_project, negation, already_initialized,
    ):
        (git_project / ".gitignore").write_text(negation + "\n")
        subprocess.run(
            ["git", "add", ".gitignore"], cwd=git_project,
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Ignore rules"], cwd=git_project,
            capture_output=True, check=True,
        )
        runtime = git_project / ".autohelix"
        if already_initialized:
            runtime.mkdir()

        result = runner.invoke(main, ["init", str(git_project)])

        assert result.exit_code == 1
        assert "not effectively ignored" in result.output
        assert f".gitignore:1:{negation}" in result.output
        assert "Add .autohelix/ after" in result.output
        assert "Traceback" not in result.output
        assert (git_project / ".gitignore").read_text() == negation + "\n"
        assert not (git_project / "autohelix.yaml").exists()
        if already_initialized:
            assert list(runtime.iterdir()) == []
        else:
            assert not runtime.exists()

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
