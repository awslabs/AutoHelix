"""docs/config.md's "Where configs live": both recipes for a private config history
work. `autohelix/` as its own git repository (ignored by the project) and
`autohelix` as a symlink into a personal repository of configs: a run starts (clean
checkout, no nested-repository rejection), lands automatically, and `resume`
continues it. The folder recipe has no uncommitted-config warning; the symlink
recipe does (its file is outside the project, which `git check-ignore` refuses), and
the docs say it doesn't apply."""

import json
from pathlib import Path

import yaml

from tests.helpers import cli, git, latest_run

CONFIG = {
    "goal": "Make a checked change",
    "agent": {"cli": "mock", "mock": {"change_file": "code.py", "change_content": "# iteration {iteration}"}},
    "gates": ["test -f code.py"],
}


def private_configs(project, folder, exclude):
    """Move the project's config to `folder`/perf.yaml as its own repository's commit,
    take autohelix.yaml out of the project's history and ignore `exclude` there."""
    folder.mkdir(parents=True)
    (folder / "perf.yaml").write_text(yaml.safe_dump(CONFIG))
    git(folder, "init", "-q")
    git(folder, "add", "perf.yaml")
    git(folder, "commit", "-qm", "perf config")
    git(project, "rm", "-q", "autohelix.yaml")
    git(project, "commit", "-qm", "configs are private")
    with open(project / ".git" / "info" / "exclude", "a") as f:
        f.write(f"{exclude}\n")


def run_and_resume(project, config_path, warned=False):
    """`run perf`, then `resume perf`: both succeed, every iteration is accepted and
    landed, and the project's checkout stays clean."""
    first = cli(project, "run", "perf", "-n", "1", "--no-report")
    assert first.exit_code == 0, first.output
    assert ("first: a commit on" in first.output) == warned  # the uncommitted-config warning
    paths = latest_run(project)
    assert paths.read_identity()["config_path"] == config_path
    second = cli(project, "resume", "perf", "-n", "1", "--no-report")
    assert second.exit_code == 0, second.output
    records = [json.loads((folder / "record.json").read_text()) for folder in sorted(paths.iterations.iterdir())]
    assert [(r["iteration"], r["accepted"]) for r in records] == [(0, True), (1, True), (2, True)]
    assert git(project, "rev-parse", "HEAD") == git(project, "rev-parse", f"autohelix/{paths.run_id}")
    assert git(project, "status", "--porcelain") == ""


def test_autohelix_folder_as_its_own_repository(project):
    private_configs(project, project / "autohelix", "/autohelix/")
    run_and_resume(project, "autohelix/perf.yaml")


def test_autohelix_symlink_into_a_personal_repository(project, tmp_path):
    personal = tmp_path / "configs" / "project"
    private_configs(project, personal, "/autohelix")  # a symlink is a file to git: no trailing slash
    (project / "autohelix").symlink_to(personal)
    # run.json names the config by its resolved path, outside the project.
    run_and_resume(project, str((personal / "perf.yaml").resolve()), warned=True)


def test_untracked_root_config(project):
    """The **Private** bullet's other form: a committed `autohelix.yaml` untracked
    (`git rm --cached`) and excluded as `/autohelix.yaml` runs without the warning."""
    (project / "autohelix.yaml").write_text(yaml.safe_dump(CONFIG))
    git(project, "add", "autohelix.yaml")
    git(project, "commit", "-qm", "config")
    git(project, "rm", "-q", "--cached", "autohelix.yaml")
    git(project, "commit", "-qm", "config is private")
    with open(project / ".git" / "info" / "exclude", "a") as f:
        f.write("/autohelix.yaml\n")
    result = cli(project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert "first: a commit on" not in result.output
    assert git(project, "status", "--porcelain") == ""
    assert git(project, "rev-parse", "HEAD") == git(project, "rev-parse", f"autohelix/{latest_run(project).run_id}")


def test_the_page_shows_the_tested_exclude_entries():
    """The page's recipes use the entries these tests use."""

    page = (Path(__file__).resolve().parents[1] / "docs" / "config.md").read_text()
    section = page[page.index("## Where configs live"):page.index("## Full Reference")]
    for line in ("echo /autohelix/ >> .git/info/exclude",
                 "echo /autohelix >> .git/info/exclude",
                 "ln -s ~/configs/myproject autohelix",
                 "`/autohelix.yaml`",
                 "git rm -r --cached autohelix"):
        assert line in section, line
