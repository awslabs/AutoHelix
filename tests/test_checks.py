"""Tests for autohelix.checks module."""

from unittest.mock import MagicMock, patch

import pytest

from autohelix.checks import (
    CHECK_KILL_GRACE,
    metric_gate_failure,
    metric_reference,
    parse_structured_metrics,
    run_command,
    run_observe_entry,
)
from autohelix.config import MetricGate, ObserveEntry
from autohelix.history import IterationResult


def _observe(tmp_path, script, metrics):
    """Run a real command entry (`sh -c script`) and return its result."""
    import shlex

    entry = ObserveEntry(name="bench", command=f"sh -c {shlex.quote(script)}", metrics=metrics)
    return run_observe_entry(entry, cwd=tmp_path)


class TestOnlyStructuredLinesAreMetrics:
    """A metric is read only from `##autohelix[name=value]` on stdout, by name."""

    @pytest.mark.parametrize("line", [
        "throughput: 1234.5", "Throughput: 500", "throughput=99.9",
        "loss: 1.5e-05",  # used to read 1.5
        "val_throughput: 0.99",  # used to be taken for `throughput`
    ])
    def test_a_name_value_line_is_not_a_metric(self, tmp_path, line):
        result = _observe(tmp_path, f"echo '{line}'", {"throughput": "higher"})
        assert result.ok and result.values == {}
        assert "##autohelix[throughput=<number>]" in result.errors["throughput"]

    def test_a_structured_line_under_another_name_is_not_the_metric(self, tmp_path):
        """A lone line with another name (a typo, a sub-tool's metric) used to be taken
        for the one declared metric; the error names what was reported instead."""
        result = _observe(tmp_path, "echo '##autohelix[sped=5]'; echo '##autohelix[time=3]'",
                          {"speed": "higher"})
        assert result.values == {}
        assert "(it reported sped, time)" in result.errors["speed"]

    def test_stderr_is_not_read(self, tmp_path):
        result = _observe(tmp_path, "echo '##autohelix[speed=7]' >&2", {"speed": "higher"})
        assert result.values == {} and "speed" in result.errors

    def test_full_float_syntax_and_the_last_line_wins(self, tmp_path):
        result = _observe(
            tmp_path,
            "echo 'loss: 9'; echo '##autohelix[loss=1]'; echo '##autohelix[loss=1.5e-05]';"
            " echo '##autohelix[delta=-3]'",
            {"loss": "lower", "delta": "higher"},
        )
        assert result.values == {"loss": 1.5e-05, "delta": -3.0}
        assert result.errors == {}

    def test_a_non_finite_value_reports_nothing(self, tmp_path):
        result = _observe(tmp_path, "echo '##autohelix[speed=1e999]'; echo 'speed: 1'",
                          {"speed": "higher"})
        assert result.values == {} and "speed" in result.errors


class TestParseStructuredMetrics:
    def test_single_metric(self):
        assert parse_structured_metrics("##autohelix[speed=1523.4]") == {"speed": 1523.4}

    def test_multiple_metrics(self):
        output = "##autohelix[speed=1523.4]\n##autohelix[memory=42.1]"
        assert parse_structured_metrics(output) == {"speed": 1523.4, "memory": 42.1}

    def test_embedded_in_noisy_output(self):
        output = "Running benchmark...\nStep 1 of 3\n##autohelix[speed=100]\nDone."
        assert parse_structured_metrics(output) == {"speed": 100.0}

    def test_scientific_notation(self):
        assert parse_structured_metrics("##autohelix[rate=1.5e3]") == {"rate": 1500.0}

    def test_negative_value(self):
        assert parse_structured_metrics("##autohelix[delta=-0.5]") == {"delta": -0.5}

    def test_non_finite_values_are_not_produced(self):
        output = "##autohelix[speed=1e999]\n##autohelix[lat=-1e999]\n##autohelix[ok=2]"
        assert parse_structured_metrics(output) == {"ok": 2.0}

    def test_no_structured_lines(self):
        assert parse_structured_metrics("speed: 42\nno structured output here") == {}

    def test_empty_string(self):
        assert parse_structured_metrics("") == {}

    def test_malformed_line_ignored(self):
        output = "##autohelix[bad]\n##autohelix[speed=100]"
        assert parse_structured_metrics(output) == {"speed": 100.0}


class TestRunObservableErrorMessage:
    """Tests that run_observe_entry gives actionable error messages."""

    @patch("autohelix.checks.subprocess.Popen")
    def test_unparseable_output_includes_format_hint(self, mock_popen):
        proc = MagicMock()
        proc.communicate.return_value = ("Python 3.10.12\nAll tests passed\n", "")
        proc.returncode = 0
        mock_popen.return_value = proc
        obs = ObserveEntry(name="bench", command="python bench.py", metrics={"throughput": "higher"})
        result = run_observe_entry(obs, cwd=MagicMock())
        assert "throughput" not in result.values
        assert "##autohelix[throughput=" in result.errors["throughput"]


class TestRunObservable:
    """Tests for run_observe_entry output parsing and error reporting."""

    @patch("autohelix.checks.subprocess.Popen")
    def test_multiple_values_from_single_command(self, mock_popen):
        proc = MagicMock()
        proc.communicate.return_value = (
            "##autohelix[throughput=1500]\n##autohelix[latency=0.5]", "",
        )
        proc.returncode = 0
        mock_popen.return_value = proc
        obs = ObserveEntry(
            name="bench", command="bench.py", metrics={"throughput": "higher", "latency": "lower"},
        )
        result = run_observe_entry(obs, cwd=MagicMock())
        assert result.values == {"throughput": 1500.0, "latency": 0.5}
        assert result.errors == {}

    @patch("autohelix.checks.subprocess.Popen")
    def test_missing_metric_not_fabricated_when_multiple_declared(self, mock_popen):
        # Two metrics declared but only one emitted: throughput's value is not
        # copied into latency.
        proc = MagicMock()
        proc.communicate.return_value = ("##autohelix[throughput=1500]", "")
        proc.returncode = 0
        mock_popen.return_value = proc
        obs = ObserveEntry(
            name="bench", command="bench.py", metrics={"throughput": "higher", "latency": "lower"},
        )
        result = run_observe_entry(obs, cwd=MagicMock())
        assert result.values == {"throughput": 1500.0}
        assert "latency" in result.errors


class TestInterruptCleanup:
    @patch("autohelix.checks._kill_process_group")
    @patch("autohelix.checks.subprocess.Popen")
    def test_constraint_interrupt_kills_process_group(
        self, mock_popen, mock_kill
    ):
        proc = MagicMock()
        proc.communicate.side_effect = KeyboardInterrupt
        mock_popen.return_value = proc

        with pytest.raises(KeyboardInterrupt):
            run_command("sleep 60", cwd=MagicMock())

        mock_kill.assert_called_once_with(proc, grace=CHECK_KILL_GRACE)

    @patch("autohelix.checks._kill_process_group")
    @patch("autohelix.checks.subprocess.Popen")
    def test_observable_interrupt_kills_process_group(
        self, mock_popen, mock_kill
    ):
        proc = MagicMock()
        proc.communicate.side_effect = KeyboardInterrupt
        mock_popen.return_value = proc
        observable = ObserveEntry(
            name="bench",
            command="sleep 60",
            metrics={"score": "higher"},
        )

        with pytest.raises(KeyboardInterrupt):
            run_observe_entry(observable, cwd=MagicMock())

        mock_kill.assert_called_once_with(proc, grace=CHECK_KILL_GRACE)


class TestNoTerminalInput:
    """Gate, observe and stop commands never get the harness's stdin: run from a terminal,
    that is the user's TTY (Rich in a gate would measure the user's window, and a command
    reading input would wait for the user)."""

    @pytest.fixture
    def terminal_stdin(self):
        """fd 0 is a terminal, as when `autohelix run` is started from a shell."""
        import os
        import pty

        leader, follower = pty.openpty()
        saved = os.dup(0)
        os.dup2(follower, 0)
        try:
            yield
        finally:
            os.dup2(saved, 0)
            for fd in (saved, follower, leader):
                os.close(fd)

    SCRIPT = "if [ -t 0 ]; then echo tty; else echo no-tty; fi; cat; echo read-to-end"

    def test_gate_and_stop_commands(self, terminal_stdin, tmp_path):
        result = run_command(self.SCRIPT, cwd=tmp_path, timeout=10)  # `stop:` runs through it too
        assert result.passed and not result.timed_out
        assert result.stdout.split() == ["no-tty", "read-to-end"]

    def test_observe_commands(self, terminal_stdin, tmp_path):
        entry = ObserveEntry(name="probe", command=self.SCRIPT + "; echo '##autohelix[x=1]'",
                             metrics={"x": "higher"})
        result = run_observe_entry(entry, cwd=tmp_path, timeout=10)
        assert result.values == {"x": 1.0}
        assert result.stdout.split()[:2] == ["no-tty", "read-to-end"]


def _example_metrics():
    """(example, metric) for every command observe entry of the bundled examples'
    configs (templates' `{placeholders}` filled in)."""
    import re as _re
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parent.parent / "examples"
    for path in sorted([*root.glob("*/autohelix/*/config.yaml"), *root.glob("*/templates/config.yaml.tmpl")]):
        text = _re.sub(r"\{(\w+)\}", "x", path.read_text())
        example = path.relative_to(root).parts[0]
        for entry in (yaml.safe_load(text) or {}).get("observe") or []:
            if isinstance(entry, dict) and "command" in entry:
                for metric in entry.get("metrics") or {}:
                    yield example, metric


@pytest.mark.parametrize(("example", "metric"), list(_example_metrics()))
def test_every_example_prints_its_metrics_as_structured_lines(example, metric):
    """Only `##autohelix[name=value]` lines are metrics: each example's scripts print one
    per declared metric (algotune's benchmark used to rely on a `speedup:` line)."""
    from pathlib import Path

    folder = Path(__file__).resolve().parent.parent / "examples" / example
    sources = "".join(p.read_text(errors="replace") for p in folder.rglob("*")
                      if p.is_file() and p.suffix in {".py", ".sh", ".tmpl"})
    assert f"##autohelix[{metric}=" in sources or (
        example == "workflow-optimization" and "##autohelix[{name}=" in sources)


# --- metric gates (moved from test_config.py, beside the code they test) ---

def _record(iteration, metrics, accepted=True, commit="c"):
    return IterationResult(iteration=iteration, accepted=accepted, metrics=metrics,
                           commit=None if iteration == 0 else commit)


class TestMetricGateReference:
    def test_reference_is_the_tip_commit_record(self):
        records = [_record(0, {"s": 10}), _record(1, {"s": 12}), _record(2, {"s": 30}, accepted=False),
                   _record(3, {"s": 11})]
        assert metric_reference(records, "s") == 11

    def test_baseline_until_a_commit(self):
        assert metric_reference([_record(0, {"s": 10}), _record(1, {"s": 9}, accepted=False)], "s") == 10

    def test_accepted_no_op_does_not_re_measure(self):
        records = [_record(0, {"s": 10}), _record(1, {"s": 12}), _record(2, {"s": 15}, commit=None)]
        assert metric_reference(records, "s") == 12

    def test_no_fallback_when_the_tip_record_lacks_the_metric(self):
        records = [_record(0, {"s": 10, "t": 1}), _record(1, {"s": 12, "t": 2}), _record(2, {"s": 13})]
        assert metric_reference(records, "t") is None
        assert metric_reference(records, "u") is None


class TestMetricGateFailure:
    def regress(self, pct):
        return MetricGate("s", "regress", pct)

    def improve(self, pct):
        return MetricGate("s", "improve", pct)

    def test_regress(self):
        assert metric_gate_failure(self.regress(5), "higher", 96, 100) is None
        assert metric_gate_failure(self.regress(5), "higher", 95, 100) is None  # exactly at the bound
        assert "5.1% worse than 100" in metric_gate_failure(self.regress(5), "higher", 94.9, 100)
        assert metric_gate_failure(self.regress(5), "lower", 105, 100) is None
        assert metric_gate_failure(self.regress(5), "lower", 106, 100)
        assert metric_gate_failure(self.regress(0), "higher", 99.99, 100)
        assert metric_gate_failure(self.regress(0), "higher", 100, 100) is None

    def test_improve(self):
        assert metric_gate_failure(self.improve(0), "higher", 100.01, 100) is None
        assert "does not improve" in metric_gate_failure(self.improve(0), "higher", 100, 100)
        assert metric_gate_failure(self.improve(5), "higher", 105, 100) is None
        assert metric_gate_failure(self.improve(5), "higher", 1.05, 1.0) is None  # float noise at the bound
        assert "by only 4.0%" in metric_gate_failure(self.improve(5), "higher", 104, 100)
        assert metric_gate_failure(self.improve(10), "lower", 0.9, 1.0) is None
        assert metric_gate_failure(self.improve(10), "lower", 0.95, 1.0)

    def test_negative_reference_uses_its_absolute_value(self):
        assert metric_gate_failure(self.regress(10), "higher", -1.05, -1.0) is None
        assert metric_gate_failure(self.regress(10), "higher", -1.2, -1.0)
        assert metric_gate_failure(self.improve(10), "higher", -0.9, -1.0) is None

    def test_near_zero(self):
        assert metric_gate_failure(self.regress(50), "lower", 0, 0) is None
        assert metric_gate_failure(self.regress(50), "lower", 0.001, 0)  # no worse than 0
        assert metric_gate_failure(self.improve(50), "lower", -0.001, 0) is None  # any strict improvement
        assert metric_gate_failure(self.improve(50), "lower", 0, 0)
        assert metric_gate_failure(self.improve(0), "lower", 0, 1) is None  # count 1 -> 0

    def test_non_finite_values_fail(self):
        for value in (float("inf"), float("-inf"), float("nan")):
            assert "not a finite number" in metric_gate_failure(self.regress(5), "higher", value, 100)
            assert "not a finite number" in metric_gate_failure(self.improve(0), "lower", value, 100)

    def test_non_finite_references_are_none(self):
        assert metric_reference([_record(0, {"s": float("inf")})], "s") is None
        records = [_record(0, {"s": 10}), _record(1, {"s": float("nan")})]
        assert metric_reference(records, "s") is None

    def test_missing_value_fails_and_missing_reference_passes(self):
        assert "was not measured" in metric_gate_failure(self.regress(5), "higher", None, 100)
        assert metric_gate_failure(self.improve(5), "higher", 1, None) is None
