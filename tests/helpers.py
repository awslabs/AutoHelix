"""Small helpers shared by test files."""

from pathlib import Path

from autohelix.runs import RunPaths, project_runs


def latest_run(project: Path) -> RunPaths:
    """The project's most recently active run, for tests that started one and read it
    back (there is no current-run pointer to follow); before any run, the project's
    paths without a run (`RunPaths(project)`), as file-only tests use them."""
    runs = project_runs(project)
    return runs[0][0] if runs else RunPaths(project)
