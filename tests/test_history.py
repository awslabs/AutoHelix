"""Tests for autohelix.history module."""

import json
import time

import pytest

from autohelix.history import History, IterationResult


@pytest.fixture
def history(tmp_path):
    return History(tmp_path)


def _write_results(history, results):
    """Helper to write IterationResult objects to history."""
    for r in results:
        history.append(r)


class TestIterationResult:
    def test_defaults(self):
        r = IterationResult(iteration=1, accepted=True, metrics={"speed": 10.0})
        assert r.commit is None
        assert r.reason is None


class TestHistoryAppendAndLoad:
    def test_empty_history(self, history):
        assert history.load() == []

    def test_append_and_load(self, history):
        r = IterationResult(iteration=1, accepted=True, metrics={"speed": 100.0})
        history.append(r)
        loaded = history.load()
        assert len(loaded) == 1
        assert loaded[0].iteration == 1
        assert loaded[0].metrics == {"speed": 100.0}

    def test_append_multiple(self, history):
        _write_results(history, [
            IterationResult(iteration=1, accepted=True, metrics={"speed": 100.0}),
            IterationResult(iteration=2, accepted=False, metrics={"speed": 90.0}, reason="regressed"),
        ])
        loaded = history.load()
        assert len(loaded) == 2
        assert loaded[1].accepted is False
        assert loaded[1].reason == "regressed"

    def test_creates_parent_dirs(self, tmp_path):
        h = History(tmp_path / "nested" / "deep")
        h.append(IterationResult(iteration=1, accepted=True, metrics={}))
        assert h.history_path.exists()

    def test_tolerates_malformed_and_unknown_lines(self, history):
        # A good row, a corrupt/partial line, a forward-compat unknown field,
        # then another good row. Load should skip the bad ones, not crash.
        history.append(IterationResult(iteration=1, accepted=True, metrics={"speed": 100.0}))
        with open(history.history_path, "a") as f:
            f.write('{"iteration": 2, "accepted"\n')  # truncated (crash mid-write)
            f.write(json.dumps({"iteration": 3, "accepted": True, "metrics": {},
                                "future_field": "x"}) + "\n")  # newer schema
        history.append(IterationResult(iteration=4, accepted=True, metrics={"speed": 120.0}))

        loaded = history.load()
        assert [r.iteration for r in loaded] == [1, 3, 4]


class TestGetLastIteration:
    def test_empty(self, history):
        assert history.get_last_iteration() == 0

    def test_returns_max(self, history):
        _write_results(history, [
            IterationResult(iteration=1, accepted=True, metrics={}),
            IterationResult(iteration=3, accepted=True, metrics={}),
            IterationResult(iteration=2, accepted=True, metrics={}),
        ])
        assert history.get_last_iteration() == 3


class TestGetBestMetrics:
    def test_empty(self, history):
        assert history.get_best_metrics() == {}

    def test_higher_is_better_default(self, history):
        _write_results(history, [
            IterationResult(iteration=1, accepted=True, metrics={"speed": 100.0}),
            IterationResult(iteration=2, accepted=True, metrics={"speed": 200.0}),
            IterationResult(iteration=3, accepted=True, metrics={"speed": 150.0}),
        ])
        best = history.get_best_metrics()
        assert best["speed"] == (200.0, 2)

    def test_lower_is_better(self, history):
        _write_results(history, [
            IterationResult(iteration=1, accepted=True, metrics={"time": 5.0}),
            IterationResult(iteration=2, accepted=True, metrics={"time": 3.0}),
            IterationResult(iteration=3, accepted=True, metrics={"time": 4.0}),
        ])
        best = history.get_best_metrics(directions={"time": "lower"})
        assert best["time"] == (3.0, 2)

    def test_lower_is_better_without_directions_picks_highest(self, history):
        """Without directions, defaults to higher-is-better (the old buggy behavior)."""
        _write_results(history, [
            IterationResult(iteration=1, accepted=True, metrics={"time": 5.0}),
            IterationResult(iteration=2, accepted=True, metrics={"time": 3.0}),
        ])
        best = history.get_best_metrics()
        # Without direction info, picks highest (5.0)
        assert best["time"] == (5.0, 1)

    def test_skips_rejected(self, history):
        _write_results(history, [
            IterationResult(iteration=1, accepted=True, metrics={"speed": 100.0}),
            IterationResult(iteration=2, accepted=False, metrics={"speed": 999.0}),
        ])
        best = history.get_best_metrics()
        assert best["speed"] == (100.0, 1)

    def test_multiple_metrics_mixed_directions(self, history):
        _write_results(history, [
            IterationResult(iteration=1, accepted=True, metrics={"speed": 100.0, "time": 5.0}),
            IterationResult(iteration=2, accepted=True, metrics={"speed": 80.0, "time": 3.0}),
        ])
        best = history.get_best_metrics(directions={"speed": "higher", "time": "lower"})
        assert best["speed"] == (100.0, 1)
        assert best["time"] == (3.0, 2)

    def test_single_accepted_result(self, history):
        _write_results(history, [
            IterationResult(iteration=1, accepted=True, metrics={"score": 42.0}),
        ])
        best = history.get_best_metrics()
        assert best["score"] == (42.0, 1)


class TestGetRecent:
    def test_empty(self, history):
        assert history.get_recent() == []

    def test_fewer_than_n(self, history):
        _write_results(history, [
            IterationResult(iteration=1, accepted=True, metrics={}),
        ])
        assert len(history.get_recent(5)) == 1

    def test_returns_last_n(self, history):
        for i in range(10):
            history.append(IterationResult(iteration=i, accepted=True, metrics={}))
        recent = history.get_recent(3)
        assert len(recent) == 3
        assert [r.iteration for r in recent] == [7, 8, 9]


class TestHistoryCaching:
    def test_load_returns_cached_on_second_call(self, history):
        history.append(IterationResult(iteration=1, accepted=True, metrics={"speed": 10.0}))
        first = history.load()
        second = history.load()
        assert first is second  # Same object means cache hit

    def test_cache_invalidated_by_append(self, history):
        history.append(IterationResult(iteration=1, accepted=True, metrics={"speed": 10.0}))
        first = history.load()
        assert len(first) == 1
        history.append(IterationResult(iteration=2, accepted=True, metrics={"speed": 20.0}))
        second = history.load()
        assert len(second) == 2
        assert first is not second

    def test_cache_invalidated_by_external_write(self, history):
        history.append(IterationResult(iteration=1, accepted=True, metrics={}))
        first = history.load()
        assert len(first) == 1
        # Simulate external write by directly writing to the file
        # Need to ensure mtime changes (some filesystems have 1s granularity)
        import os
        mtime = os.path.getmtime(history.history_path)
        os.utime(history.history_path, (mtime + 1, mtime + 1))
        with open(history.history_path, "a") as f:
            f.write(json.dumps({"iteration": 2, "accepted": True, "metrics": {}}) + "\n")
        os.utime(history.history_path, (mtime + 2, mtime + 2))
        second = history.load()
        assert len(second) == 2

    def test_empty_history_caching(self, history):
        first = history.load()
        assert first == []
        # Creating the file should be detected
        history.append(IterationResult(iteration=1, accepted=True, metrics={}))
        second = history.load()
        assert len(second) == 1

    def test_multiple_methods_use_cache(self, history):
        """get_last_iteration, get_best_metrics, get_recent, format_summary all call load()."""
        for i in range(5):
            history.append(IterationResult(iteration=i + 1, accepted=True, metrics={"s": float(i)}))
        # First load populates cache
        history.load()
        # These should all use cached data
        assert history.get_last_iteration() == 5
        assert "s" in history.get_best_metrics()
        assert len(history.get_recent(3)) == 3
        assert "iter 5" in history.format_summary()


class TestFormatSummary:
    def test_empty(self, history):
        assert history.format_summary() == "No previous iterations."

    def test_accepted_and_rejected(self, history):
        _write_results(history, [
            IterationResult(iteration=1, accepted=True, metrics={"speed": 100.0}),
            IterationResult(iteration=2, accepted=False, metrics={}, reason="failed constraints"),
        ])
        summary = history.format_summary()
        assert "iter 1" in summary
        assert "speed=100.0" in summary
        assert "accepted" in summary
        assert "iter 2" in summary
        assert "rejected" in summary
        assert "failed constraints" in summary

    def test_failure_output_included(self, history):
        """Failure output should be included so agent can understand what went wrong."""
        _write_results(history, [
            IterationResult(
                iteration=1,
                accepted=False,
                metrics={},
                reason="constraint failed: pytest",
                failure_output="FAILED test_foo.py::test_bar\nAssertionError: expected 1, got 2",
            ),
        ])
        summary = history.format_summary()
        assert "constraint failed: pytest" in summary
        assert "FAILED test_foo.py::test_bar" in summary
        assert "AssertionError" in summary


class TestUsageField:
    def test_usage_defaults_to_empty(self):
        r = IterationResult(iteration=1, accepted=True, metrics={})
        assert r.usage == {}

    def test_usage_persisted(self, history):
        r = IterationResult(
            iteration=1,
            accepted=True,
            metrics={"speed": 100.0},
            usage={"input_tokens": 5000, "output_tokens": 1000, "cost_usd": 0.05},
        )
        history.append(r)
        loaded = history.load()
        assert loaded[0].usage["input_tokens"] == 5000
        assert loaded[0].usage["cost_usd"] == 0.05

    def test_backwards_compat_missing_usage(self, history):
        """Old history entries without usage field should load with empty usage."""
        history.history_path.parent.mkdir(parents=True, exist_ok=True)
        old_entry = json.dumps({
            "iteration": 1,
            "accepted": True,
            "metrics": {"speed": 50.0},
            "commit": None,
            "reason": None,
            "failure_output": None,
        })
        history.history_path.write_text(old_entry + "\n")
        loaded = history.load()
        assert len(loaded) == 1
        assert loaded[0].usage == {}
