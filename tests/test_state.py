"""Tests for autohelix.state — persistent .autohelix/ state helpers."""

import subprocess

import pytest

from autohelix.state import (
    GitExcludeError,
    ensure_git_exclude_entry,
    ensure_runtime_ignored,
)
from autohelix.runs import RunPaths


@pytest.fixture
def git_project(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


class TestEnsureGitExcludeEntry:
    def test_creates_file_when_absent(self, git_project):
        exclude = git_project / ".git" / "info" / "exclude"
        exclude.unlink()
        ensure_git_exclude_entry(git_project, ".autohelix/")
        assert exclude.read_text() == ".autohelix/\n"
        assert not (git_project / ".gitignore").exists()

    def test_appends_when_missing(self, git_project):
        exclude = git_project / ".git" / "info" / "exclude"
        exclude.write_text("node_modules/\n")
        ensure_git_exclude_entry(git_project, ".autohelix/")
        assert exclude.read_text() == "node_modules/\n.autohelix/\n"

    def test_idempotent_when_present(self, git_project):
        exclude = git_project / ".git" / "info" / "exclude"
        exclude.write_text(".autohelix/\n")
        ensure_git_exclude_entry(git_project, ".autohelix/")
        ensure_git_exclude_entry(git_project, ".autohelix/")
        assert exclude.read_text() == ".autohelix/\n"

    def test_adds_newline_before_append_when_missing(self, git_project):
        exclude = git_project / ".git" / "info" / "exclude"
        exclude.write_text("node_modules/")  # no trailing newline
        ensure_git_exclude_entry(git_project, ".autohelix/")
        assert exclude.read_text() == "node_modules/\n.autohelix/\n"

    def test_comment_and_other_directory_do_not_count_as_entry(self, git_project):
        exclude = git_project / ".git" / "info" / "exclude"
        content = "# .autohelix/\nother/.autohelix/\n"
        exclude.write_text(content)
        ensure_git_exclude_entry(git_project, ".autohelix/")
        assert exclude.read_text() == content + ".autohelix/\n"

    @pytest.mark.parametrize("negation", ["!.*", "!*"])
    def test_runtime_rechecks_existing_exclusion(self, git_project, negation):
        ensure_runtime_ignored(git_project)
        exclude = git_project / ".git" / "info" / "exclude"
        before = exclude.read_bytes()
        (git_project / ".gitignore").write_text(negation + "\n")

        with pytest.raises(GitExcludeError, match="not effectively ignored"):
            ensure_runtime_ignored(git_project)

        assert exclude.read_bytes() == before


def test_paths_without_an_id_name_no_run_even_beside_an_old_pointer(tmp_path):
    """There is no current-run pointer: an old `.internal/current` file is never followed
    (it used to be, and a hand-edited one broke every command)."""
    pointer = tmp_path / ".autohelix" / ".internal" / "current"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("My Run\n")
    paths = RunPaths(tmp_path)
    assert paths.run_id is None
    assert paths.config == tmp_path / ".autohelix" / "config.yaml"  # no run's stored config
