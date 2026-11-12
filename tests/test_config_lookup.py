"""Config lookup: autohelix.yaml or autohelix/<name>.yaml, and no setup step before `run`."""

import re
import shutil

import pytest
import yaml
from click.testing import CliRunner

from autohelix.cli import main
from autohelix.config import ConfigNotFound, find_config
from autohelix.judge import CONFIG_ENV, load_base_config
from autohelix.runs import RunPaths, git
from tests.test_runs import project  # noqa: F401 - fixture


def write(path, text="goal: x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def cli(project, *args):
    return CliRunner().invoke(main, [*args, "-p", str(project)])


def test_a_name_finds_its_file_in_the_folder(tmp_path):
    perf = write(tmp_path / "autohelix" / "perf.yaml")
    write(tmp_path / "autohelix.yaml")
    assert find_config(tmp_path, "perf") == perf


def test_a_missing_name_lists_the_configs(tmp_path):
    write(tmp_path / "autohelix.yaml")
    write(tmp_path / "autohelix" / "fast.yaml")
    with pytest.raises(ConfigNotFound, match=r"No config autohelix/perf\.yaml\. "
                                             r"Configs: autohelix\.yaml, autohelix/fast\.yaml\."):
        find_config(tmp_path, "perf")
    with pytest.raises(ConfigNotFound, match="not a config name"):
        find_config(tmp_path, "autohelix/fast.yaml")


def test_bare_lookup_prefers_autohelix_yaml_then_the_only_file(tmp_path):
    only = write(tmp_path / "autohelix" / "perf.yaml")
    write(tmp_path / "autohelix" / "prompts" / "helper.yaml")  # subfolders hold templates and scripts
    write(tmp_path / "autohelix" / "notes.yml")
    assert find_config(tmp_path) == only
    write(tmp_path / "autohelix" / "fast.yaml")
    top = write(tmp_path / "autohelix.yaml")
    assert find_config(tmp_path) == top


def test_bare_lookup_with_several_or_none_is_an_error(tmp_path):
    with pytest.raises(ConfigNotFound, match="No config found .*Write one with `autohelix setup`") as none:
        find_config(tmp_path)
    assert none.value.configs == []
    write(tmp_path / "autohelix" / "perf.yaml")
    write(tmp_path / "autohelix" / "fast.yaml")
    with pytest.raises(ConfigNotFound, match=r"Several configs: autohelix/fast\.yaml, autohelix/perf\.yaml\. "
                                             r"Pick one: `autohelix run <name>`") as several:
        find_config(tmp_path)
    assert several.value.configs == ["autohelix/fast.yaml", "autohelix/perf.yaml"]


@pytest.fixture
def folder_project(project):  # noqa: F811
    """The shared project, its config moved to autohelix/perf.yaml and no .autohelix/ yet."""
    (project / "autohelix").mkdir()
    git(project, "mv", "autohelix.yaml", "autohelix/perf.yaml")
    git(project, "commit", "-qm", "config folder")
    shutil.rmtree(project / ".autohelix")
    return project


def test_run_works_without_autohelix_dir_and_names_the_run_after_the_config(folder_project):
    result = cli(folder_project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    paths = RunPaths(folder_project)
    assert re.fullmatch(r"\d{8}-\d{4}-perf-[0-9a-z]{4}", paths.run_id)
    assert paths.read_identity()["config_path"] == "autohelix/perf.yaml"
    exclude = (folder_project / ".git" / "info" / "exclude").read_text().splitlines()
    assert ".autohelix/" in exclude and "__pycache__/" in exclude


def test_run_by_name_and_config_together_is_a_usage_error(folder_project):
    result = cli(folder_project, "run", "perf", "-c", str(folder_project / "autohelix" / "perf.yaml"))
    assert result.exit_code == 2 and "not both" in result.output
    assert not (folder_project / ".autohelix").exists()


def test_run_by_name(folder_project):
    write(folder_project / "autohelix" / "other.yaml", (folder_project / "autohelix" / "perf.yaml").read_text())
    git(folder_project, "add", ".")
    git(folder_project, "commit", "-qm", "second config")
    assert "Several configs" in cli(folder_project, "run").output
    result = cli(folder_project, "run", "other", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert RunPaths(folder_project).read_identity()["config_path"] == "autohelix/other.yaml"


def test_bare_resume_with_several_configs_names_them(folder_project):
    write(folder_project / "autohelix" / "other.yaml")
    result = cli(folder_project, "resume")
    assert result.exit_code == 1
    assert ("Several configs: autohelix/other.yaml, autohelix/perf.yaml. "
            "Name a run (`autohelix resume <id>`) or a config (-c PATH).") in result.output


def test_run_outside_git_refuses_and_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))  # tmp_path may sit in a repository
    project = tmp_path / "project"
    write(project / "autohelix.yaml", "goal: x\nagent: {backend: mock}\n")
    result = cli(project, "run")
    assert result.exit_code == 1
    assert "Not a git repository. AutoHelix requires git." in result.output
    assert sorted(p.name for p in project.iterdir()) == ["autohelix.yaml"]


def test_ask_and_review_read_the_only_config_in_the_folder(tmp_path, monkeypatch):
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    write(tmp_path / "autohelix" / "perf.yaml", yaml.safe_dump({"agent": {"backend": "codex", "model": "m"}}))
    assert (load_base_config(tmp_path).backend, load_base_config(tmp_path).model) == ("codex", "m")
    write(tmp_path / "autohelix" / "fast.yaml", yaml.safe_dump({"agent": {"backend": "codex"}}))
    assert load_base_config(tmp_path).backend == "claude"  # ambiguous: the defaults, not an error


def test_an_invalid_config_is_named(folder_project):
    write(folder_project / "autohelix" / "perf.yaml", "goal: ''\nagent: {backend: mock}\n")
    git(folder_project, "commit", "-qam", "empty goal")
    result = cli(folder_project, "run", "perf")
    assert result.exit_code == 1
    assert "Config validation failed. Edit autohelix/perf.yaml and try again." in result.output
