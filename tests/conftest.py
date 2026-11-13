"""Shared fixtures: a run layout for file-only observer tests, and a committed
project with a mock-agent config for run tests (`tests.test_runs.configure` edits it)."""

import pytest
import yaml

from autohelix.runs import SCHEMA, RunPaths, git, write_json, write_text


@pytest.fixture
def run_paths(tmp_path):
    paths = RunPaths(tmp_path, "test")
    write_json(paths.identity, {
        "schema": SCHEMA, "id": "test", "config_path": "autohelix.yaml",
        "base_commit": "0" * 40, "target_branch": None,
    })
    write_text(paths.current, "test\n")
    return paths


@pytest.fixture
def project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-q")
    git(project, "config", "user.email", "test@example.com")
    git(project, "config", "user.name", "Test")
    (project / "code.py").write_text("print('baseline')\n")
    (project / "autohelix.yaml").write_text(yaml.safe_dump({
        "goal": "Make a checked change", "report": False,
        "agent": {"backend": "mock", "settings": {
            "change_file": "code.py", "change_content": "# iteration {iteration}", "cost_usd": 0.1,
        }},
    }))
    git(project, "add", ".")
    git(project, "commit", "-qm", "baseline")
    (project / ".autohelix").mkdir()
    return project
