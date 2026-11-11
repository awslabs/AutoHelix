"""The old clear command now aliases retained run abandonment."""

import pytest
from click.testing import CliRunner

from autohelix.cli import main
from autohelix.harness import Harness
from autohelix.runs import RunPaths, git


@pytest.fixture
def finished_run(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "code.py").write_text("baseline\n")
    (tmp_path / "autohelix.yaml").write_text(
        "goal: improve\nreport: false\nagent:\n  type: mock\n"
        "  settings:\n    change_file: code.py\n    change_content: accepted\n"
    )
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "baseline")
    harness = Harness(tmp_path)
    harness.run(max_iterations=1, report=False)
    return harness.paths


@pytest.mark.parametrize("command", ["clear", "abandon"])
def test_abandon_retains_artifacts_and_current_without_archive(finished_run, command):
    paths = finished_run
    paths.notes.mkdir(exist_ok=True)
    (paths.notes / "iter-1.md").write_text("learning")
    before = paths.history.read_text()
    tip = git(paths.project, "rev-parse", "HEAD").stdout.strip()
    claudeignore = paths.project / ".claudeignore"
    claudeignore.write_text("existing/\n")
    args = ["clear", str(paths.project)] if command == "clear" else ["abandon", "--path", str(paths.project)]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert paths.read_identity()["lifecycle"] == "abandoned"
    assert paths.history.read_text() == before
    assert (paths.notes / "iter-1.md").read_text() == "learning"
    assert RunPaths(paths.project).run_id == paths.run_id
    assert git(paths.project, "rev-parse", "HEAD").stdout.strip() == tip
    assert git(paths.project, "rev-parse", "--verify", paths.ref, check=False).returncode != 0
    assert not (paths.root / "archive").exists()
    assert claudeignore.read_text() == "existing/\n"


def test_abandon_purge_deletes_run_but_keeps_lock_inode(finished_run):
    paths = finished_run
    inode = paths.lock.stat().st_ino
    result = CliRunner().invoke(main, ["abandon", paths.run_id, "--path", str(paths.project), "--purge"])
    assert result.exit_code == 0, result.output
    assert not paths.run_dir.exists()
    assert paths.lock.stat().st_ino == inode
    assert (paths.root / "current").read_text().strip() == paths.run_id


def test_clear_without_current_reports_error(tmp_path):
    result = CliRunner().invoke(main, ["clear", str(tmp_path)])
    assert result.exit_code != 0
    assert "No current run" in result.output
