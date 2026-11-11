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
