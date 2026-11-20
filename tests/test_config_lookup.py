"""Config lookup: autohelix.yaml or autohelix/<name>/autohelix.yaml, and no setup step before `run`."""

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from autohelix.cli import main
from autohelix.config import ConfigNotFound, config_name, find_config, project_configs, run_command
from autohelix.judge import CONFIG_ENV, load_base_config
from autohelix.harness import Harness
from autohelix.history import History
from autohelix.runs import NoRunToResume, RunPaths, git, new_run_name, short_id
from autohelix.viewer import RunData
from tests.helpers import cli, latest_run


def write(path, text="goal: x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def flat(text):
    """Output without the line wraps of a narrow terminal."""
    return " ".join(text.split())


# --- the lookup -------------------------------------------------------------


def test_a_name_finds_its_experiment_folder(tmp_path):
    perf = write(tmp_path / "autohelix" / "perf" / "autohelix.yaml")
    write(tmp_path / "autohelix.yaml")
    assert find_config(tmp_path, "perf") == perf


def test_a_missing_name_lists_the_configs(tmp_path):
    write(tmp_path / "autohelix.yaml")
    write(tmp_path / "autohelix" / "fast" / "autohelix.yaml")
    write(tmp_path / "autohelix" / "perf" / "autohelix.yml")
    with pytest.raises(ConfigNotFound, match=r"^No config autohelix/perf/autohelix\.yaml \(found "
                                             r"autohelix/perf/autohelix\.yml; configs end in \.yaml\)\. "
                                             r"Configs: autohelix\.yaml, autohelix/fast/autohelix\.yaml\.$"):
        find_config(tmp_path, "perf")
    with pytest.raises(ValueError, match=r"not a config name: .*`perf` for autohelix/perf/autohelix\.yaml"):
        find_config(tmp_path, "autohelix/fast/autohelix.yaml")


def test_a_yaml_file_directly_in_the_folder_is_not_a_config(tmp_path):
    write(tmp_path / "autohelix" / "perf.yaml")  # the old form: not found, no special error
    with pytest.raises(ConfigNotFound, match=r"^No config autohelix/perf/autohelix\.yaml\.$"):
        find_config(tmp_path, "perf")
    with pytest.raises(ConfigNotFound, match=r"^No config found in "):
        find_config(tmp_path)
    assert project_configs(tmp_path) == []


def test_folders_without_a_config_and_dot_folders_are_not_experiments(tmp_path):
    only = write(tmp_path / "autohelix" / "perf" / "autohelix.yaml")
    write(tmp_path / "autohelix" / "lib" / "metrics.py")  # shared helpers
    write(tmp_path / "autohelix" / "lib" / "helper.yaml")
    write(tmp_path / "autohelix" / ".old" / "autohelix.yaml")  # dot-folders are never looked at
    write(tmp_path / "autohelix" / "README.md")
    assert project_configs(tmp_path) == ["autohelix/perf/autohelix.yaml"]
    assert find_config(tmp_path) == only
    with pytest.raises(ConfigNotFound):
        find_config(tmp_path, "lib")
    with pytest.raises(ValueError, match="not a config name"):
        find_config(tmp_path, ".old")


def test_bare_lookup_prefers_autohelix_yaml_then_the_only_experiment(tmp_path):
    only = write(tmp_path / "autohelix" / "perf" / "autohelix.yaml")
    write(tmp_path / "autohelix" / "perf" / "prompt.md")  # an experiment's material beside its config
    write(tmp_path / "autohelix" / "notes" / "autohelix.yml")
    assert find_config(tmp_path) == only
    write(tmp_path / "autohelix" / "fast" / "autohelix.yaml")
    top = write(tmp_path / "autohelix.yaml")
    assert find_config(tmp_path) == top


def test_bare_lookup_with_several_or_none_states_what_it_found(tmp_path):
    with pytest.raises(ConfigNotFound, match=r"^No config found in .*\.$") as none:
        find_config(tmp_path)
    assert none.value.configs == []
    write(tmp_path / "autohelix.yml")
    write(tmp_path / "autohelix" / "perf" / "autohelix.yml")
    with pytest.raises(ConfigNotFound, match=r"\(found autohelix\.yml, autohelix/perf/autohelix\.yml; "
                                             r"configs end in \.yaml\)"):
        find_config(tmp_path)
    write(tmp_path / "autohelix" / "perf" / "autohelix.yaml")
    write(tmp_path / "autohelix" / "fast" / "autohelix.yaml")
    with pytest.raises(ConfigNotFound, match=r"^Several configs: autohelix/fast/autohelix\.yaml, "
                                             r"autohelix/perf/autohelix\.yaml\.$") as several:
        find_config(tmp_path)
    assert several.value.configs == ["autohelix/fast/autohelix.yaml", "autohelix/perf/autohelix.yaml"]


def test_the_experiment_name_is_the_folders():
    assert config_name("autohelix/perf/autohelix.yaml") == "perf"
    assert config_name(Path("autohelix/perf/autohelix.yaml")) == "perf"
    assert config_name("autohelix.yaml") is None
    assert config_name("autohelix/perf.yaml") is None
    assert config_name("autohelix/perf/other.yaml") is None
    assert config_name("examples/sorting/autohelix.yaml") is None
    assert run_command("autohelix/perf/autohelix.yaml") == "autohelix run perf"
    assert run_command("autohelix.yaml") == "autohelix run"
    assert new_run_name("autohelix/Fast_Sort/autohelix.yaml", None) == "fast-sort"
    assert new_run_name("autohelix.yaml", None) is None
    assert new_run_name("autohelix.yaml", "x") == "x"


# --- run and resume ---------------------------------------------------------


@pytest.fixture
def folder_project(project):
    """The shared project, its config moved to autohelix/perf/autohelix.yaml and no .autohelix/ yet."""
    (project / "autohelix" / "perf").mkdir(parents=True)
    git(project, "mv", "autohelix.yaml", "autohelix/perf/autohelix.yaml")
    git(project, "commit", "-qm", "config folder")
    shutil.rmtree(project / ".autohelix")
    return project


def commit(project, message="change"):
    git(project, "add", "-A")
    git(project, "commit", "-qm", message)


def test_run_works_without_autohelix_dir_and_names_the_run_after_the_config(folder_project):
    out = folder_project.parent / "config-seen"
    config = folder_project / "autohelix" / "perf" / "autohelix.yaml"
    data = yaml.safe_load(config.read_text())
    data["gates"] = [f'printf %s "$AUTOHELIX_CONFIG" > {out}']
    config.write_text(yaml.safe_dump(data))
    commit(folder_project)
    result = cli(folder_project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    paths = latest_run(folder_project)
    assert re.fullmatch(r"\d{8}-\d{4}-perf-[0-9a-z]{4}", paths.run_id)
    assert paths.read_identity()["config_path"] == "autohelix/perf/autohelix.yaml"
    assert out.read_text() == str(paths.config)  # judges read the run's stored copy
    assert paths.config.read_text() == config.read_text()
    exclude = (folder_project / ".git" / "info" / "exclude").read_text().splitlines()
    assert ".autohelix/" in exclude and "__pycache__/" in exclude


def test_run_by_name(folder_project):
    write(folder_project / "autohelix" / "other" / "autohelix.yaml", (folder_project / "autohelix" / "perf" / "autohelix.yaml").read_text())
    commit(folder_project)
    result = cli(folder_project, "run")
    assert result.exit_code == 1
    assert ("Several configs: autohelix/other/autohelix.yaml, autohelix/perf/autohelix.yaml. "
            "Pick one: `autohelix run <name>`.") in result.output
    result = cli(folder_project, "run", "other", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert latest_run(folder_project).read_identity()["config_path"] == "autohelix/other/autohelix.yaml"
    result = cli(folder_project, "run", "autohelix.yaml")
    assert result.exit_code == 2 and "not a config name" in result.output
    assert "a bare `autohelix run` reads autohelix.yaml" in flat(result.output)


def test_a_yml_file_is_pointed_out(folder_project):
    git(folder_project, "mv", "autohelix/perf/autohelix.yaml", "autohelix/perf/autohelix.yml")
    commit(folder_project)
    result = cli(folder_project, "run")
    assert result.exit_code == 1
    assert "(found autohelix/perf/autohelix.yml; configs end in .yaml)" in result.output


def test_bare_resume_with_several_configs_asks_for_a_run(folder_project):
    write(folder_project / "autohelix" / "other" / "autohelix.yaml")
    result = cli(folder_project, "resume")  # no runs yet: how to start one
    assert result.exit_code == 1 and "Pick one: `autohelix run <name>`." in result.output
    result = cli(folder_project, "run", "perf", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    result = cli(folder_project, "resume")
    assert result.exit_code == 1
    assert ("Several configs: autohelix/other/autohelix.yaml, autohelix/perf/autohelix.yaml. "
            "Name a run or a config: `autohelix resume <id|name>`.") in result.output


def test_bare_resume_without_runs_creates_nothing(folder_project):
    result = cli(folder_project, "resume")
    assert result.exit_code == 1
    assert "No run to resume for autohelix/perf/autohelix.yaml; start one with `autohelix run perf`." in result.output
    assert_untouched(folder_project)
    (folder_project / "autohelix" / "perf" / "autohelix.yaml").unlink()
    result = cli(folder_project, "resume")
    assert result.exit_code == 1 and "Write autohelix.yaml: run `autohelix` in a terminal" in result.output
    assert_untouched(folder_project)


def assert_untouched(project):
    """No run state and no local excludes: a refusal writes nothing."""
    assert not (project / ".autohelix").exists()
    exclude = project / ".git" / "info" / "exclude"
    assert ".autohelix/" not in (exclude.read_text().splitlines() if exclude.exists() else [])


def test_resume_id_keeps_its_config_beside_a_root_one(folder_project):
    result = cli(folder_project, "run", "-n", "1", "--no-report", "--land", "manual")
    assert result.exit_code == 0, result.output
    run_id = latest_run(folder_project).run_id
    write(folder_project / "autohelix.yaml", "goal: ''\nagent: {cli: mock}\n")  # invalid if it were read
    commit(folder_project)
    result = cli(folder_project, "resume", short_id(run_id), "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert latest_run(folder_project).read_identity()["config_path"] == "autohelix/perf/autohelix.yaml"
    # A bare run takes autohelix.yaml and says what else there is.
    result = cli(folder_project, "run")
    assert "Using autohelix.yaml; autohelix/ also has: perf (`autohelix run perf`)" in result.output
    assert "Config validation failed. Edit autohelix.yaml and try again." in result.output
    result = cli(folder_project, "resume")
    assert "Using autohelix.yaml; autohelix/ also has: perf (`autohelix resume perf`)" in result.output
    assert result.exit_code == 1 and "No run to resume for autohelix.yaml" in result.output


def test_a_run_whose_config_is_gone_continues_with_its_stored_copy(folder_project):
    result = cli(folder_project, "run", "-n", "1", "--no-report", "--land", "manual")
    assert result.exit_code == 0, result.output
    run_id = latest_run(folder_project).run_id
    git(folder_project, "mv", "autohelix/perf", "autohelix/fast")  # the only config, found by a lookup
    git(folder_project, "commit", "-qm", "rename")
    result = cli(folder_project, "resume", short_id(run_id), "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert (f"autohelix/perf/autohelix.yaml is gone; run {short_id(run_id)} continues with the config it stored "
            "when it started.") in flat(result.output)
    assert History(folder_project, run_id).get_last_iteration() == 2
    config = RunData(folder_project, latest_run(folder_project).run_id).state()["config"]  # the web page's payload
    assert config["file"] == "autohelix/perf/autohelix.yaml" and "missing" not in config and config["text"]
    listed = json.loads(cli(folder_project, "view", short_id(run_id), "--json").stdout)["run"]
    assert (listed["config_file"], listed["config_invalid"], listed["goal"]) == (
        "autohelix/perf/autohelix.yaml", False, "Make a checked change")
    assert "config_missing" not in listed


def test_a_run_without_a_stored_config_is_never_given_its_file(folder_project):
    result = cli(folder_project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    paths = latest_run(folder_project)
    short = short_id(paths.run_id)
    paths.config.unlink()  # only a development build made such runs: not read, never its source file
    refused = (f"Run {short} has no stored config: it was made by a development version of AutoHelix. "
               f"Delete it (`autohelix delete --force {short}`).")
    result = cli(folder_project, "resume", short)
    assert result.exit_code == 1 and refused in flat(result.output), result.output
    result = cli(folder_project, "report", short)
    assert result.exit_code == 1 and refused in flat(result.output), result.output
    config = RunData(folder_project, latest_run(folder_project).run_id).state()["config"]
    assert config["file"] == "autohelix/perf/autohelix.yaml" and "missing" not in config and "agent" not in config
    listed = json.loads(cli(folder_project, "view", short, "--json").stdout)["run"]
    assert (listed["config_file"], listed["config_invalid"], listed["goal"]) == ("autohelix/perf/autohelix.yaml", True, None)
    assert "autohelix/perf/autohelix.yaml (invalid)" in cli(folder_project, "view", short, "--once").output
    (row,) = json.loads(cli(folder_project, "view", "--json").output)["runs"]
    assert row["config_invalid"] and "config_missing" not in row


def test_an_invalid_config_is_named(folder_project):
    write(folder_project / "autohelix" / "perf" / "autohelix.yaml", "goal: ''\nagent: {cli: mock}\n")
    commit(folder_project)
    result = cli(folder_project, "run", "perf")
    assert result.exit_code == 1
    assert "Config validation failed. Edit autohelix/perf/autohelix.yaml and try again." in result.output
    assert_untouched(folder_project)


def test_a_subfolder_without_a_config_points_at_the_project_above(folder_project):
    write(folder_project / "src" / "lib.py", "x = 1\n")
    commit(folder_project)
    result = cli(folder_project / "src", "run")
    assert result.exit_code == 1
    root = folder_project.resolve()
    assert (f"No config found in {root / 'src'}. {root} has autohelix/perf/autohelix.yaml; "
            f"run from there or pass -p {root}.") in result.output
    assert not (folder_project / "src" / ".autohelix").exists()


def test_a_subproject_with_its_own_config_runs(folder_project):
    sub = folder_project / "examples" / "sub"
    write(sub / "code.py", "print('sub')\n")
    write(sub / "autohelix.yaml", (folder_project / "autohelix" / "perf" / "autohelix.yaml").read_text())
    commit(folder_project)
    result = cli(sub, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert latest_run(sub).read_identity()["config_path"] == "autohelix.yaml"
    assert not (folder_project / ".autohelix").exists()


# --- refusals before anything is created ------------------------------------


def test_run_outside_git_refuses_and_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))  # tmp_path may sit in a repository
    project = tmp_path / "project"
    write(project / "autohelix.yaml", "goal: x\nagent: {cli: mock}\n")
    result = cli(project, "run")
    assert result.exit_code == 1
    assert "Not a git repository. AutoHelix requires git." in result.output
    assert sorted(p.name for p in project.iterdir()) == ["autohelix.yaml"]


def test_run_without_a_commit_refuses_and_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    project = tmp_path / "project"
    write(project / "autohelix.yaml", "goal: x\nagent: {cli: mock}\n")
    git(project, "init", "-q")
    result = cli(project, "run")
    assert result.exit_code == 1
    assert "No commits yet; a run starts from a commit." in result.output
    assert "git add -A && git commit -m 'init'" in result.output
    assert sorted(p.name for p in project.iterdir()) == [".git", "autohelix.yaml"]
    exclude = project / ".git" / "info" / "exclude"
    assert ".autohelix/" not in (exclude.read_text().splitlines() if exclude.exists() else [])


# --- ask and review by hand -------------------------------------------------


def test_ask_and_review_read_the_only_config_in_the_folder(tmp_path, monkeypatch):
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    write(tmp_path / "autohelix" / "perf" / "autohelix.yaml", yaml.safe_dump({"agent": {"cli": "codex", "model": "m"}}))
    assert (load_base_config(tmp_path).cli, load_base_config(tmp_path).model) == ("codex", "m")
    write(tmp_path / "autohelix" / "fast" / "autohelix.yaml", yaml.safe_dump({"agent": {"cli": "codex"}}))
    assert load_base_config(tmp_path).cli == "claude-code"  # ambiguous: the defaults, not an error


def _answers(path, answer):
    return write(path, yaml.safe_dump({"goal": "g", "agent": {"cli": "mock", "mock": {
        "judge_answer": answer, "judge_reason": answer}}}))


def test_ask_prefers_the_harness_config_then_autohelix_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AUTOHELIX_USAGE_FILE", raising=False)
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    _answers(tmp_path / "autohelix" / "perf" / "autohelix.yaml", "no")
    assert CliRunner().invoke(main, ["ask", "Done?"]).exit_code == 1  # the folder's only config
    monkeypatch.setenv(CONFIG_ENV, str(_answers(tmp_path.parent / f"{tmp_path.name}-run.yaml", "yes")))
    assert CliRunner().invoke(main, ["ask", "Done?"]).exit_code == 0
    monkeypatch.delenv(CONFIG_ENV)
    _answers(tmp_path / "autohelix.yaml", "yes")
    assert CliRunner().invoke(main, ["ask", "Done?"]).exit_code == 0


def test_a_dirty_tree_is_refused_before_anything_is_written(folder_project):
    write(folder_project / "code.py", "print('edited')\n")
    result = cli(folder_project, "run", "perf")
    assert result.exit_code == 1 and "Uncommitted changes in tracked files" in result.output
    assert_untouched(folder_project)


def test_a_scope_config_error_is_reported_before_anything_is_written(folder_project):
    config = folder_project / "autohelix" / "perf" / "autohelix.yaml"
    data = yaml.safe_load(config.read_text())
    data["scope"] = {"editable": ["../elsewhere/"]}
    config.write_text(yaml.safe_dump(data))
    commit(folder_project)
    result = cli(folder_project, "run", "perf")
    assert result.exit_code == 1 and "scope.editable: '../elsewhere/' has a `..` component" in flat(result.output)
    assert_untouched(folder_project)


def test_an_editable_pattern_matching_nothing_yet_only_warns(folder_project):
    """[01-2] The agent may be meant to create it: a warning, and the run starts."""
    config = folder_project / "autohelix" / "perf" / "autohelix.yaml"
    data = yaml.safe_load(config.read_text())
    data["scope"] = {"editable": ["results/", "*.py"]}
    config.write_text(yaml.safe_dump(data))
    commit(folder_project)
    result = cli(folder_project, "run", "perf", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert "scope.editable 'results/' matches no committed file" in flat(result.output)
    assert "'*.py' matches" not in flat(result.output)


def test_no_run_to_resume_is_refused_before_anything_is_written(folder_project):
    with pytest.raises(NoRunToResume, match="No run to resume for autohelix/perf/autohelix.yaml"):
        Harness(folder_project, create=False)
    assert_untouched(folder_project)
    write(folder_project / "autohelix" / "perf" / "autohelix.yaml", "goal: ''\nagent: {cli: mock}\n")  # invalid too
    commit(folder_project)
    result = cli(folder_project, "resume")
    assert result.exit_code == 1 and "No run to resume for autohelix/perf/autohelix.yaml" in result.output
    assert_untouched(folder_project)


def test_run_has_no_name_option(folder_project):
    result = cli(folder_project, "run", "perf", "--name", "x")
    assert result.exit_code == 2 and "No such option '--name'." in result.output
    assert_untouched(folder_project)


def test_an_old_name_key_is_an_error(folder_project):
    config = folder_project / "autohelix" / "perf" / "autohelix.yaml"
    config.write_text(config.read_text() + "name: fast\n")
    commit(folder_project)
    result = cli(folder_project, "run", "perf")
    assert result.exit_code == 1
    assert "`name:` is gone: runs are named after the experiment folder; use autohelix/<name>/autohelix.yaml" in flat(result.output)
    assert_untouched(folder_project)


def test_a_config_that_is_not_yaml_is_named(folder_project):
    result = cli(folder_project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    run_id = short_id(latest_run(folder_project).run_id)
    write(folder_project / "autohelix" / "perf" / "autohelix.yaml", "goal: [\n")
    commit(folder_project)
    for args in (("run", "perf"), ("resume", run_id)):
        result = cli(folder_project, *args)
        assert result.exit_code == 1, (args, result.output)
        assert "Cannot parse autohelix/perf/autohelix.yaml: " in result.output, args
        assert "Traceback" not in result.output and result.exception is None or isinstance(
            result.exception, SystemExit), args
    assert cli(folder_project, "view", run_id, "--json").exit_code == 0
    assert cli(folder_project, "view", "--json").exit_code == 0


def test_report_without_runs_creates_nothing(folder_project):
    result = cli(folder_project, "report")
    assert result.exit_code == 1 and "No runs in" in result.output and "start one with `autohelix run perf`" in result.output
    assert_untouched(folder_project)


def test_an_unborn_branch_is_named(folder_project):
    git(folder_project, "checkout", "-q", "--orphan", "fresh")
    result = cli(folder_project, "run")
    assert result.exit_code == 1 and "HEAD has no commit yet (a new or orphan branch)" in flat(result.output)
    assert_untouched(folder_project)


def test_a_bare_repository_and_the_git_folder_are_refused(folder_project, tmp_path):
    bare = tmp_path / "bare.git"
    git(tmp_path, "clone", "-q", "--bare", str(folder_project), str(bare))
    result = cli(bare, "run")
    assert result.exit_code == 1 and "is not in a git working tree" in result.output
    result = cli(folder_project / ".git", "run")
    assert result.exit_code == 1 and "is not in a git working tree" in result.output
    assert not (bare / ".autohelix").exists() and not (folder_project / ".git" / ".autohelix").exists()


def test_inherited_git_variables_are_dropped(folder_project, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "elsewhere")
    git(other, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "--allow-empty", "-m", "other")
    branch = git(folder_project, "symbolic-ref", "--short", "HEAD").stdout.strip()
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))  # as in a git hook of another repository
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    result = cli(folder_project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert ".autohelix/" not in (other / ".git" / "info" / "exclude").read_text().splitlines()
    assert ".autohelix/" in (folder_project / ".git" / "info" / "exclude").read_text().splitlines()
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))  # dropped by the run above: set again
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    assert json.loads(cli(folder_project, "view", "--json").output)["branch"] == branch
