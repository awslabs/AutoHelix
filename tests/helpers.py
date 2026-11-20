"""Helpers shared by test files: `git`, `cli`, `CONFIG`, `config_file`, `private`, `write_config`,
`configure`,
`latest_run` and `with_words`. The committed mock-agent `project` and the file-only `run_paths`
fixtures are in `tests/conftest.py`."""

import os
import subprocess
from pathlib import Path

import yaml
from click.testing import CliRunner

from autohelix.cli import main
from autohelix.runs import Landing, RunPaths, landing_words, project_runs


def git(cwd, *args, check=True, env=None) -> str:
    """Run git in `cwd` with a test identity; its stdout, stripped. A failure raises
    unless `check=False`; `env` adds variables."""
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
                          cwd=cwd, capture_output=True, text=True, check=check,
                          env={**os.environ, **(env or {})}).stdout.strip()


def cli(project, *args):
    """`autohelix ARGS -p PROJECT`, in-process."""
    return CliRunner().invoke(main, [*args, "-p", str(project)])


# The `project` fixture's experiment: its config, project-relative, and its name.
NAME = "demo"
CONFIG = f"autohelix/{NAME}/config.yaml"


def config_file(project) -> Path:
    """The `project` fixture's config file: `project/autohelix/demo/config.yaml`."""
    return Path(project) / CONFIG


def private(project):
    """Make the project's autohelix/ private, as setup does: `/autohelix/` in the
    repository's exclude file (`git rev-parse --git-path info/exclude`)."""
    exclude = Path(project) / git(project, "rev-parse", "--git-path", "info/exclude")
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text() if exclude.exists() else ""
    if "/autohelix/" not in text.splitlines():
        exclude.write_text(text + ("" if not text or text.endswith("\n") else "\n") + "/autohelix/\n")


def write_config(project, text: str) -> Path:
    """Write the project's config (`CONFIG`), private (`private`); its path."""
    private(project)
    path = config_file(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def configure(project, **values):
    """Set top-level keys of the project's config (`CONFIG`, private: not committed)."""
    path = config_file(project)
    data = yaml.safe_load(path.read_text())
    data.update(values)
    path.write_text(yaml.safe_dump(data))


def latest_run(project: Path) -> RunPaths:
    """The project's most recently active run, for tests that started one and read it
    back (there is no current-run pointer to follow); before any run, the project's
    paths without a run (`RunPaths(project)`), as file-only tests use them."""
    runs = project_runs(project)
    return runs[0][0] if runs else RunPaths(project)


def with_words(run: dict) -> dict:
    """A hand-made run payload with the `landing_words` the viewer gives it: the words of
    its `landing` (None: the target branch is missing), `target_branch`, `accepted` (a
    count, as the payload has it) and `land`."""
    landing = run.get("landing")
    found = Landing("tip", run.get("target_branch"), "target-tip" if landing else None,
                    landing and landing["relation"], landing["commits"] if landing else 0,
                    landing["behind"] if landing else 0, bool(run.get("accepted")))
    words = landing_words(found, run["id"], run.get("land"))
    return {**run, "landing_words": {"cell": words.cell, "text": words.sentence, "note": words.note,
                                     "commands": list(words.commands)}}
