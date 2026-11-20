"""docs/concepts.md's "Where configs live": `autohelix/` as its own git repository
(excluded by the project) runs (clean checkout, no nested-repository rejection),
lands automatically, and `resume` continues it, without the uncommitted-config
warning. Also `autohelix` as a symlink into a personal repository of configs, which
the docs don't describe (run.json names its config as named, through the link): it runs,
with the warning (its file is outside the project, which `git check-ignore`
refuses)."""

import json
import shutil
from pathlib import Path

import yaml

from tests.helpers import config_file, cli, git, latest_run

CONFIG = {
    "goal": "Make a checked change",
    "agent": {"cli": "mock", "mock": {"change_file": "code.py", "change_content": "# iteration {iteration}"}},
    "gates": ["test -f code.py"],
}


def private_configs(project, folder, exclude):
    """Replace the project's experiments with `folder`/perf/config.yaml as its own repository's
    commit, and ignore `exclude` in the project."""
    shutil.rmtree(project / "autohelix")
    (folder / "perf").mkdir(parents=True)
    (folder / "perf" / "config.yaml").write_text(yaml.safe_dump(CONFIG))
    git(folder, "init", "-q")
    git(folder, "add", "perf/config.yaml")
    git(folder, "commit", "-qm", "perf config")
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
    run_and_resume(project, "autohelix/perf/config.yaml")


def test_autohelix_symlink_into_a_personal_repository(project, tmp_path):
    personal = tmp_path / "configs" / "project"
    private_configs(project, personal, "/autohelix")  # a symlink is a file to git: no trailing slash
    (project / "autohelix").symlink_to(personal)
    # run.json names the config as named, through the link, and the run is named after its folder.
    run_and_resume(project, "autohelix/perf/config.yaml", warned=True)
    assert latest_run(project).read_identity()["name"] == "perf"


def test_a_committed_experiment_made_private(project):
    """The **Private** bullet's other form: a committed `autohelix/` untracked
    (`git rm -r --cached autohelix`) and excluded as `/autohelix/` runs without the warning."""
    config_file(project).write_text(yaml.safe_dump(CONFIG))
    git(project, "add", "-f", "autohelix")
    git(project, "commit", "-qm", "config")
    git(project, "rm", "-r", "-q", "--cached", "autohelix")
    git(project, "commit", "-qm", "config is private")
    assert "/autohelix/" in (project / ".git" / "info" / "exclude").read_text().splitlines()  # as setup makes it
    result = cli(project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert "first: a commit on" not in result.output
    assert git(project, "status", "--porcelain") == ""
    assert git(project, "rev-parse", "HEAD") == git(project, "rev-parse", f"autohelix/{latest_run(project).run_id}")


def test_the_page_shows_the_tested_exclude_entries():
    """The page's recipes use the entries these tests use."""

    page = (Path(__file__).resolve().parents[1] / "docs" / "concepts.md").read_text()
    section = " ".join(page[page.index("### Where configs live"):page.index("## Runs and run state")].split())
    for line in ('echo /autohelix/ >> "$(git rev-parse --git-path info/exclude)"',
                 "`/<subfolder>/autohelix/`",
                 "git rm -r --cached autohelix",
                 "git add -f autohelix/bench/",
                 "`/autohelix/*` and `!/autohelix/bench/`"):
        assert line in section, line


def test_sharing_one_experiment_out_of_a_private_folder(project):
    """The page's sharing rules: an excluded folder's experiment is staged with `git add -f`,
    except when `autohelix/` has its own repository (exit 0, nothing staged); with
    `.gitignore`, `/autohelix/*` + `!/autohelix/bench/` leaves just that experiment visible."""
    bench = project / "autohelix" / "bench"
    bench.mkdir(parents=True)
    (bench / "config.yaml").write_text(yaml.safe_dump(CONFIG))
    (project / "autohelix" / "other.txt").write_text("private\n")
    with open(project / ".git" / "info" / "exclude", "a") as f:
        f.write("/autohelix/\n")
    git(project / "autohelix", "init", "-q")
    git(project, "add", "-f", "autohelix/bench/")
    assert git(project, "diff", "--cached", "--name-only") == ""  # the inner repository wins
    shutil.rmtree(project / "autohelix" / ".git")
    git(project, "add", "-f", "autohelix/bench/")
    assert git(project, "diff", "--cached", "--name-only") == "autohelix/bench/config.yaml"
    git(project, "reset", "-q")
    (project / ".git" / "info" / "exclude").write_text("")
    (project / ".gitignore").write_text("/autohelix/*\n!/autohelix/bench/\n")
    untracked = git(project, "ls-files", "--others", "--exclude-standard", "autohelix")
    assert untracked == "autohelix/bench/config.yaml"
