"""Tests for `autohelix parallel` resume (bare invocation reads the marker)."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from autohelix.cli import main, _parallel_marker_path


@pytest.fixture
def runner():
    return CliRunner()


def _write_marker(repo: Path, configs, sync_every=1, run_args=None):
    marker = _parallel_marker_path(repo)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "configs": [str(c) for c in configs],
        "sync_every": sync_every,
        "run_args": run_args or [],
        "workers": [],
    }))


class TestParallelResume:
    def test_bare_resume_reuses_recorded_configs(self, runner, tmp_path):
        a = tmp_path / "a.yaml"; a.write_text("goal: x\n")
        b = tmp_path / "b.yaml"; b.write_text("goal: y\n")
        _write_marker(tmp_path, [a, b], sync_every=3, run_args=["--model", "opus"])

        with patch("autohelix.parallel.run_parallel", return_value=[]) as mock_run:
            result = runner.invoke(main, ["parallel", "-p", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "Resuming previous run (2 workers)" in result.output
        # Recorded configs, sync interval, and forwarded args are reused.
        call = mock_run.call_args
        assert [Path(c).name for c in call.args[1]] == ["a.yaml", "b.yaml"]
        assert call.kwargs["sync_every"] == 3
        assert call.kwargs["run_args"] == ["--model", "opus"]

    def test_bare_resume_without_marker_errors(self, runner, tmp_path):
        result = runner.invoke(main, ["parallel", "-p", str(tmp_path)])
        assert result.exit_code != 0
        assert "no previous parallel run" in result.output.lower()

    def test_bare_resume_with_missing_config_errors(self, runner, tmp_path):
        gone = tmp_path / "gone.yaml"  # never created
        _write_marker(tmp_path, [gone])

        result = runner.invoke(main, ["parallel", "-p", str(tmp_path)])
        assert result.exit_code != 0
        assert "no longer exist" in result.output.lower()

    def test_explicit_worker_overrides_marker_sync_default(self, runner, tmp_path):
        a = tmp_path / "a.yaml"; a.write_text("goal: x\n")
        # A marker exists, but an explicit --worker starts a fresh run and should
        # not inherit the marker's sync_every.
        _write_marker(tmp_path, [a], sync_every=9)

        with patch("autohelix.parallel.run_parallel", return_value=[]) as mock_run:
            result = runner.invoke(main, ["parallel", "-p", str(tmp_path), "--worker", str(a)])

        assert result.exit_code == 0, result.output
        assert "Resuming previous run" not in result.output
        assert mock_run.call_args.kwargs["sync_every"] == 1
