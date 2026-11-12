"""The run summary is written when a run finishes normally."""

import subprocess

from autohelix.harness import Harness
from autohelix.runs import RunPaths
from autohelix.run_status import read_run_status
from tests.test_integration import integration_project  # noqa: F401  (fixture)


def test_finished_run_writes_report(integration_project):  # noqa: F811
    Harness(integration_project).run(max_iterations=1)
    assert (RunPaths(integration_project).output / "report.md").read_text() == "Mock summary of the run.\n"
    assert read_run_status(integration_project)["state"] == "finished"


def test_no_report_when_disabled_or_nothing_ran(integration_project):  # noqa: F811
    Harness(integration_project).run(max_iterations=1, report=False)
    report = RunPaths(integration_project).output / "report.md"
    assert not report.exists()
    Harness(integration_project).run(max_iterations=1)  # iteration 1 is already done
    assert not report.exists()
    config = integration_project / "autohelix.yaml"
    config.write_text(config.read_text() + "report: false\n")
    subprocess.run(["git", "commit", "-qam", "report off"], cwd=integration_project)
    Harness(integration_project).run(max_iterations=2)
    assert not report.exists()


def test_no_report_after_interrupt(integration_project, monkeypatch):  # noqa: F811
    harness = Harness(integration_project)
    monkeypatch.setattr(harness, "run_iteration", lambda n: (_ for _ in ()).throw(KeyboardInterrupt))
    harness.run(max_iterations=1)
    assert not (RunPaths(integration_project).output / "report.md").exists()


def test_report_generation_cost_remains_separate(tmp_path, run_paths):
    import json

    import pytest

    from autohelix.history import History, IterationResult
    from autohelix.viewer import RunData

    history = History(tmp_path)
    history.append(IterationResult(iteration=0, accepted=True, metrics={},
                                   calls=[{"role": "command", "usage": {"cost_usd": 0.04}}]))
    history.append(IterationResult(iteration=1, accepted=True, metrics={},
                                   calls=[{"role": "agent", "usage": {"cost_usd": 0.1}},
                                          {"role": "command", "usage": {"cost_usd": 0.04}}]))
    output = run_paths.output
    output.mkdir()
    (output / "report.md").write_text("Summary")
    (output / "report.json").write_text(json.dumps({"cost_usd": 0.99}))
    state = RunData(tmp_path).state()
    assert state["cost"]["cost_usd"] == pytest.approx(0.18)
    assert state["report"]["cost_usd"] == 0.99
