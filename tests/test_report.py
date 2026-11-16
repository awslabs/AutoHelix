"""The run summary: written when a segment finishes normally, its cost counted in the
run's, and its failures kept to itself."""

from click.testing import CliRunner

from autohelix.cli import main
from autohelix.harness import Budget, Harness
from autohelix.runs import RunPaths
from autohelix.run_status import read_run_status
from tests.test_integration import integration_project  # noqa: F401  (fixture)
from tests.helpers import latest_run


def test_finished_run_writes_report(integration_project):  # noqa: F811
    Harness(integration_project).run(Budget(until=1))
    assert (latest_run(integration_project).report).read_text() == "Mock summary of the run.\n"
    assert read_run_status(integration_project, latest_run(integration_project).run_id)["state"] == "finished"


def test_no_report_when_disabled_or_nothing_ran(integration_project):  # noqa: F811
    Harness(integration_project).run(Budget(until=1), report=False)
    report = latest_run(integration_project).report
    assert not report.exists()
    Harness(integration_project).run(Budget(until=1))  # iteration 1 is already done
    assert not report.exists()


def test_no_report_flag_skips_the_summary(integration_project):  # noqa: F811
    result = CliRunner().invoke(main, ["run", "-p", str(integration_project), "-n", "1", "--no-report"])
    assert result.exit_code == 0, result.output
    assert not latest_run(integration_project).report.exists()


def test_no_report_after_interrupt(integration_project, monkeypatch):  # noqa: F811
    harness = Harness(integration_project)
    monkeypatch.setattr(harness, "run_iteration", lambda n: (_ for _ in ()).throw(KeyboardInterrupt))
    harness.run(Budget(until=1))
    assert not (latest_run(integration_project).report).exists()


def test_every_summary_cost_counts_in_the_run_total(project):
    """Two segments, two summaries: both in the run's cost (history + summary calls),
    the end block's total and the viewer's; report.json keeps the latest one's."""
    import pytest

    from autohelix.history import History
    from autohelix.viewer import ProjectRuns, RunData

    first = CliRunner().invoke(main, ["run", "-n", "1", "-p", str(project)])
    assert first.exit_code == 0, first.output
    paths = latest_run(project)
    resumed = CliRunner().invoke(main, ["resume", "-n", "1", "-p", str(project)])
    assert resumed.exit_code == 0, resumed.output
    history = History(project, paths.run_id)
    summaries = history.summary_usages()
    assert [(u["role"], u["status"], u["cost_usd"], u["last_iteration"]) for u in summaries] == [
        ("summary", "ok", 0.1, 1), ("summary", "ok", 0.1, 2)]
    total = 0.1 * 2 + 0.1 * 2  # two iterations, two summaries
    assert sum(u["cost_usd"] for u in history.cost_usages) == pytest.approx(total)
    def run_line(output):
        return next(line for line in output.splitlines() if line.strip().startswith("run ") and "accepted" in line)

    assert run_line(first.output).endswith("$0.20")  # the end block: the iteration and the summary
    assert run_line(resumed.output).endswith("$0.40")
    state = RunData(project, paths.run_id).state()
    assert state["cost"]["cost_usd"] == pytest.approx(total)
    [row] = ProjectRuns(project).runs()["runs"]
    assert row["progress"]["cost_usd"] == pytest.approx(total)


def test_a_summary_that_never_returned_is_an_unknown_cost(project, monkeypatch):
    from autohelix.history import History

    assert CliRunner().invoke(main, ["run", "-n", "1", "--no-report", "-p", str(project)]).exit_code == 0
    paths = latest_run(project)

    def killed(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("autohelix.agents.mock.MockAgent.run", lambda self, **kwargs: killed())
    result = CliRunner().invoke(main, ["report", "-p", str(project)])
    assert result.exit_code != 0
    [call] = History(project, paths.run_id).summary_usages()
    assert call["status"] == "started" and call["cost_usd"] is None
    from autohelix.costs import summarize_costs

    assert summarize_costs(History(project, paths.run_id).cost_usages).incomplete


def test_no_summary_after_the_agent_failed_three_times(project, monkeypatch):
    from tests.test_runs import configure

    configure(project, agent={"backend": "mock", "settings": {"should_fail": True}})
    calls = []
    monkeypatch.setattr("autohelix.report.write_report", lambda *a, **k: calls.append(a) or True)
    result = CliRunner().invoke(main, ["run", "-n", "5", "-p", str(project)])
    assert result.exit_code == 1 and "failed 3 times in a row" in result.output
    assert calls == [] and not latest_run(project).report.exists()


def test_a_failed_summary_says_why_and_names_its_log(project, monkeypatch):
    from autohelix.agents import AgentRunResult

    class Failing:
        def __init__(self, config, **kwargs):
            pass

        def run(self, **kwargs):
            return AgentRunResult(success=False, exit_code=3, error="boom: backend exploded")

    assert CliRunner().invoke(main, ["run", "-n", "1", "--no-report", "-p", str(project)]).exit_code == 0
    monkeypatch.setattr("autohelix.agents.create_agent", lambda config, **kwargs: Failing(config))
    result = CliRunner().invoke(main, ["report", "-p", str(project)])
    assert result.exit_code == 1
    text = " ".join(result.output.split())
    assert "Summary failed: boom: backend exploded" in text and "logs/report.log" in text


def test_the_summary_keeps_the_agent_settings(project, monkeypatch):
    from tests.test_runs import configure

    configure(project, agent={"backend": "mock", "reasoning_effort": "high", "extra_args": ["--x"],
                              "timeout": "2h", "settings": {"change_file": "code.py", "cost_usd": 0.1}})
    seen = []
    import autohelix.agents as agents

    real = agents.create_agent
    monkeypatch.setattr(agents, "create_agent", lambda config, **kwargs: seen.append(config) or real(config, **kwargs))
    assert CliRunner().invoke(main, ["run", "-n", "1", "-p", str(project)]).exit_code == 0
    summary = seen[-1]
    assert (summary.reasoning_effort, summary.extra_args, summary.settings) == (
        "high", ["--x"], {"change_file": "code.py", "cost_usd": 0.1})
    assert summary.timeout == 15 * 60  # capped


import pytest  # noqa: E402


@pytest.mark.parametrize("error", [KeyboardInterrupt, RuntimeError("copy failed")])
def test_a_failed_or_interrupted_summary_leaves_the_segment_finished(project, monkeypatch, error):
    from autohelix.run_status import read_segments

    def report(*args, **kwargs):
        raise error

    monkeypatch.setattr("autohelix.report.write_report", report)
    result = CliRunner().invoke(main, ["run", "-n", "1", "-p", str(project)])
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert ("Summary interrupted" if error is KeyboardInterrupt else "Summary failed: copy failed") in text
    paths = latest_run(project)
    status = read_run_status(project, paths.run_id)
    assert status["state"] == "finished" and status["stop_reason"]["kind"] == "budget"
    [segment] = read_segments(paths.segments)
    assert (segment["state"], segment["error"]) == ("finished", None)
