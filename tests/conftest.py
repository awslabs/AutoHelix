"""Shared run layout for file-only observer tests."""

import pytest

from autohelix.runs import RunPaths, write_json


@pytest.fixture
def run_paths(tmp_path):
    paths = RunPaths(tmp_path, "test")
    write_json(paths.identity, {
        "schema": 1, "id": "test", "config_path": "autohelix.yaml",
        "base_commit": "0" * 40, "target_branch": None, "lifecycle": "open",
    })
    (paths.root / "current").write_text("test\n")
    return paths
