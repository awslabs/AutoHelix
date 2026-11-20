"""Shared fixtures: a run layout for file-only observer tests, and a committed
project with a mock-agent config for run tests: autohelix/demo/config.yaml, private
as setup makes it (`tests.helpers.configure` edits it)."""

import pytest
import yaml

from autohelix.runs import SCHEMA, RunPaths, git, write_json, write_text
from tests.helpers import CONFIG, config_file, private


@pytest.fixture
def run_paths(tmp_path):
    paths = RunPaths(tmp_path, "test")
    write_json(paths.identity, {
        "schema": SCHEMA, "id": "test", "config_path": CONFIG,
        "base_commit": "0" * 40, "target_branch": None,
    })
    return paths


@pytest.fixture
def project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-q")
    git(project, "config", "user.email", "test@example.com")
    git(project, "config", "user.name", "Test")
    # No background `git maintenance` after commits: it leaves lock files in .git
    # that tests comparing the repository's files before and after would see.
    git(project, "config", "maintenance.auto", "false")
    git(project, "config", "gc.auto", "0")
    (project / "code.py").write_text("print('baseline')\n")
    private(project)
    config_file(project).parent.mkdir(parents=True)
    config_file(project).write_text(yaml.safe_dump({
        "goal": "Make a checked change",
        "agent": {"cli": "mock", "mock": {
            "change_file": "code.py", "change_content": "# iteration {iteration}", "cost_usd": 0.1,
        }},
    }))
    git(project, "add", ".")
    git(project, "commit", "-qm", "baseline")
    (project / ".autohelix").mkdir()
    return project
