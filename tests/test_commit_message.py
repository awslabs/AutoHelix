"""Tests for commit message building with metric deltas."""

from pathlib import Path
from unittest.mock import patch, MagicMock

from autohelix.config import Config, ConstraintCommand, ObservableCommand
from autohelix.harness import Harness
from autohelix.history import History


def _make_harness(tmp_path, observables=None):
    """Create a Harness with minimal config for testing _build_commit_message."""
    config = Config(
        goal="test goal",
        constraints=[ConstraintCommand(command="true")],
        observables=observables or [],
    )
    harness = object.__new__(Harness)
    harness.config = config
    harness.history = History(tmp_path)
    return harness


class TestBuildCommitMessage:
    def test_no_metrics(self, tmp_path):
        harness = _make_harness(tmp_path)
        msg = harness._build_commit_message(1, {})
        assert msg == "AutoHelix iteration 1"

    def test_single_metric_no_history(self, tmp_path):
        harness = _make_harness(tmp_path, observables=[
            ObservableCommand(command="bench.sh", values={"speed": "higher"}),
        ])
        msg = harness._build_commit_message(1, {"speed": 1500})
        assert "AutoHelix iteration 1" in msg
        assert "speed: 1500" in msg

    def test_metric_with_improvement(self, tmp_path):
        harness = _make_harness(tmp_path, observables=[
            ObservableCommand(command="bench.sh", values={"speed": "higher"}),
        ])
        from autohelix.history import IterationResult
        harness.history.append(IterationResult(
            iteration=0, accepted=True, metrics={"speed": 1000},
        ))

        msg = harness._build_commit_message(1, {"speed": 1500})
        assert "speed: 1000 -> 1500 (+50.0%)" in msg

    def test_metric_with_regression(self, tmp_path):
        harness = _make_harness(tmp_path, observables=[
            ObservableCommand(command="bench.sh", values={"speed": "higher"}),
        ])
        from autohelix.history import IterationResult
        harness.history.append(IterationResult(
            iteration=0, accepted=True, metrics={"speed": 1000},
        ))

        msg = harness._build_commit_message(1, {"speed": 800})
        assert "speed: 1000 -> 800 (-20.0%)" in msg

    def test_lower_is_better_metric(self, tmp_path):
        harness = _make_harness(tmp_path, observables=[
            ObservableCommand(command="bench.sh", values={"latency": "lower"}),
        ])
        from autohelix.history import IterationResult
        harness.history.append(IterationResult(
            iteration=0, accepted=True, metrics={"latency": 200},
        ))

        msg = harness._build_commit_message(1, {"latency": 150})
        assert "latency: 200 -> 150 (+25.0%)" in msg

    def test_multiple_metrics(self, tmp_path):
        harness = _make_harness(tmp_path, observables=[
            ObservableCommand(command="bench.sh", values={"speed": "higher", "memory": "lower"}),
        ])
        from autohelix.history import IterationResult
        harness.history.append(IterationResult(
            iteration=0, accepted=True, metrics={"speed": 1000, "memory": 500},
        ))

        msg = harness._build_commit_message(2, {"speed": 1200, "memory": 400})
        assert "speed: 1000 -> 1200 (+20.0%)" in msg
        assert "memory: 500 -> 400 (+20.0%)" in msg

    def test_zero_baseline_skips_delta(self, tmp_path):
        harness = _make_harness(tmp_path, observables=[
            ObservableCommand(command="bench.sh", values={"speed": "higher"}),
        ])
        from autohelix.history import IterationResult
        harness.history.append(IterationResult(
            iteration=0, accepted=True, metrics={"speed": 0},
        ))

        msg = harness._build_commit_message(1, {"speed": 100})
        assert "speed: 100" in msg
        assert "%" not in msg

    def test_agent_commit_summary(self, tmp_path):
        harness = _make_harness(tmp_path, observables=[
            ObservableCommand(command="bench.sh", values={"speed": "higher"}),
        ])
        ah_dir = tmp_path / ".autohelix"
        ah_dir.mkdir(exist_ok=True)
        (ah_dir / "commit_summary.txt").write_text("Replace bubble sort with quicksort\n")

        msg = harness._build_commit_message(1, {"speed": 1500}, worktree_path=tmp_path)
        assert msg.startswith("Replace bubble sort with quicksort")
        assert "AutoHelix iteration 1" in msg
        assert "speed: 1500" in msg

    def test_no_commit_summary_file(self, tmp_path):
        harness = _make_harness(tmp_path)
        msg = harness._build_commit_message(1, {}, worktree_path=tmp_path)
        assert msg == "AutoHelix iteration 1"

    def test_multiline_commit_summary_uses_first_line(self, tmp_path):
        harness = _make_harness(tmp_path)
        ah_dir = tmp_path / ".autohelix"
        ah_dir.mkdir(exist_ok=True)
        (ah_dir / "commit_summary.txt").write_text("First line\nSecond line\nThird line\n")

        msg = harness._build_commit_message(1, {}, worktree_path=tmp_path)
        assert msg.startswith("First line")
        assert "Second line" not in msg
