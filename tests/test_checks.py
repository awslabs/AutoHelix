"""Tests for autohelix.checks module."""

from unittest.mock import patch, MagicMock

from autohelix.checks import parse_metric_output, parse_structured_metrics, run_observable
from autohelix.config import ObservableCommand


class TestParseMetricOutput:
    def test_metric_name_match(self):
        assert parse_metric_output("throughput: 1234.5", "throughput") == 1234.5

    def test_metric_name_case_insensitive(self):
        assert parse_metric_output("Throughput: 500", "throughput") == 500.0

    def test_metric_name_with_equals(self):
        assert parse_metric_output("throughput=99.9", "throughput") == 99.9

    def test_no_fallback_for_unlabeled_numbers(self):
        """Noisy output with embedded numbers should NOT be parsed."""
        assert parse_metric_output("some text 123 more text 456") is None

    def test_no_fallback_for_version_strings(self):
        """Python version strings should not be grabbed as metric values."""
        assert parse_metric_output("Python 3.10.12\nRunning tests...") is None

    def test_no_numbers(self):
        assert parse_metric_output("no numbers here") is None

    def test_empty_string(self):
        assert parse_metric_output("") is None

    def test_multiline_with_metric_name(self):
        output = "Running benchmark...\nspeed: 999\nDone."
        assert parse_metric_output(output, "speed") == 999.0

    def test_metric_name_preferred_over_common_label(self):
        output = "speed: 100\ncustom_score: 200"
        assert parse_metric_output(output, "custom_score") == 200.0


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

    def test_no_structured_lines(self):
        assert parse_structured_metrics("speed: 42\nno structured output here") == {}

    def test_empty_string(self):
        assert parse_structured_metrics("") == {}

    def test_malformed_line_ignored(self):
        output = "##autohelix[bad]\n##autohelix[speed=100]"
        assert parse_structured_metrics(output) == {"speed": 100.0}


class TestParseMetricOutputStructured:
    """Tests that structured ##autohelix protocol takes priority."""

    def test_structured_preferred_over_heuristic(self):
        output = "speed: 50\n##autohelix[speed=100]"
        assert parse_metric_output(output, "speed") == 100.0

    def test_structured_single_metric_no_name(self):
        """A single structured metric is used even without metric_name."""
        assert parse_metric_output("##autohelix[throughput=999]") == 999.0

    def test_structured_multiple_without_name_returns_none(self):
        """Multiple structured metrics without matching name returns None."""
        output = "##autohelix[a=1]\n##autohelix[b=2]\nspeed: 50"
        assert parse_metric_output(output) is None


class TestRunObservableErrorMessage:
    """Tests that run_observable gives actionable error messages."""

    @patch("autohelix.checks.subprocess.Popen")
    def test_unparseable_output_includes_format_hint(self, mock_popen):
        proc = MagicMock()
        proc.communicate.return_value = ("Python 3.10.12\nAll tests passed\n", "")
        proc.returncode = 0
        mock_popen.return_value = proc
        obs = ObservableCommand(command="python bench.py", values={"throughput": "higher"})
        result = run_observable(obs, cwd=MagicMock())
        assert "throughput" not in result.values
        assert "##autohelix[throughput=" in result.errors["throughput"]


class TestRunObservable:
    """Tests for run_observable output parsing and error reporting."""

    @patch("autohelix.checks.subprocess.Popen")
    def test_multiple_values_from_single_command(self, mock_popen):
        proc = MagicMock()
        proc.communicate.return_value = (
            "##autohelix[throughput=1500]\n##autohelix[latency=0.5]", "",
        )
        proc.returncode = 0
        mock_popen.return_value = proc
        obs = ObservableCommand(
            command="bench.py", values={"throughput": "higher", "latency": "lower"},
        )
        result = run_observable(obs, cwd=MagicMock())
        assert result.values == {"throughput": 1500.0, "latency": 0.5}
        assert result.errors == {}

    @patch("autohelix.checks.subprocess.Popen")
    def test_missing_metric_not_fabricated_when_multiple_declared(self, mock_popen):
        # Two metrics declared but only one emitted: the unnamed-metric fallback
        # must NOT copy throughput's value into latency.
        proc = MagicMock()
        proc.communicate.return_value = ("##autohelix[throughput=1500]", "")
        proc.returncode = 0
        mock_popen.return_value = proc
        obs = ObservableCommand(
            command="bench.py", values={"throughput": "higher", "latency": "lower"},
        )
        result = run_observable(obs, cwd=MagicMock())
        assert result.values == {"throughput": 1500.0}
        assert "latency" in result.errors
