# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The CI workflow runs what CLAUDE.md says to run, where users run it (16-5)."""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "test.yml"


def _job():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["test"]


def test_ci_runs_the_fast_tests_in_parallel():
    [command] = [step["run"] for step in _job()["steps"] if step.get("name") == "Run fast tests"]
    assert command == 'pytest -n auto -m "not slow"'


def test_ci_runs_on_linux_and_macos_from_the_oldest_to_the_newest_python():
    job = _job()
    matrix = job["strategy"]["matrix"]
    assert job["runs-on"] == "${{ matrix.os }}"
    assert set(matrix["os"]) == {"ubuntu-latest", "macos-latest"}
    pyproject = (WORKFLOW.parents[2] / "pyproject.toml").read_text()
    assert 'requires-python = ">=3.11"' in pyproject and "3.11" in matrix["python-version"]
    assert "3.x" in matrix["python-version"]  # setup-python's newest stable release
