"""Helpers shared by test files: `git`, `cli`, `configure` and `latest_run`. The
committed mock-agent `project` and the file-only `run_paths` fixtures are in
`tests/conftest.py`."""

import os
import subprocess
from pathlib import Path

import yaml
from click.testing import CliRunner

from autohelix.cli import main
from autohelix.runs import RunPaths, project_runs


def git(cwd, *args, check=True, env=None) -> str:
    """Run git in `cwd` with a test identity; its stdout, stripped. A failure raises
    unless `check=False`; `env` adds variables."""
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
                          cwd=cwd, capture_output=True, text=True, check=check,
                          env={**os.environ, **(env or {})}).stdout.strip()


def cli(project, *args):
    """`autohelix ARGS -p PROJECT`, in-process."""
    return CliRunner().invoke(main, [*args, "-p", str(project)])


def configure(project, **values):
    """Set top-level keys of the project's `autohelix.yaml` and commit it."""
    path = project / "autohelix.yaml"
    data = yaml.safe_load(path.read_text())
    data.update(values)
    path.write_text(yaml.safe_dump(data))
    git(project, "add", "autohelix.yaml")
    git(project, "commit", "-qm", "config")


def latest_run(project: Path) -> RunPaths:
    """The project's most recently active run, for tests that started one and read it
    back (there is no current-run pointer to follow); before any run, the project's
    paths without a run (`RunPaths(project)`), as file-only tests use them."""
    runs = project_runs(project)
    return runs[0][0] if runs else RunPaths(project)
