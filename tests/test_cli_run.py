"""Tests for failure handling in the `autohelix run` CLI command."""

import subprocess

from click.testing import CliRunner

from autohelix.cli import main
from autohelix.harness import AutoHelixRunError


def test_run_returns_nonzero_for_infrastructure_failure(tmp_path, monkeypatch):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "autohelix.yaml").write_text("goal: x\n")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "init", "--allow-empty"],
                   cwd=tmp_path, capture_output=True, check=True)

    class FailingHarness:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, max_iterations=None, report=True):
            raise AutoHelixRunError("Iteration 1 agent failed: credentials expired")

    monkeypatch.setattr("autohelix.cli.Harness", FailingHarness)
    result = CliRunner().invoke(main, ["run", "--path", str(tmp_path)])

    assert result.exit_code == 1
    assert "Iteration 1 agent failed: credentials expired" in result.output
