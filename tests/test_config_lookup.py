"""Config lookup: autohelix/<name>/config.yaml only, and no setup step before `run`."""

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
from autohelix.runs import NoRunToResume, RunPaths, git, load_run_config, new_run_name, select_run, short_id
from autohelix.viewer import RunData
from tests.helpers import CONFIG, cli, latest_run


def write(path, text="goal: x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def flat(text):
    """Output without the line wraps of a narrow terminal."""
    return " ".join(text.split())


# --- the lookup -------------------------------------------------------------


def test_a_name_finds_its_experiment_folder(tmp_path):
    perf = write(tmp_path / "autohelix" / "perf" / "config.yaml")
    write(tmp_path / "autohelix" / "fast" / "config.yaml")
    assert find_config(tmp_path, "perf") == perf


def test_the_old_file_names_are_not_configs(tmp_path):
    write(tmp_path / "autohelix.yaml")  # the old top-level form
    write(tmp_path / "autohelix" / "perf" / "autohelix.yaml")  # the old file name in a folder
    write(tmp_path / "autohelix" / "fast.yaml")
    assert project_configs(tmp_path) == []
    with pytest.raises(ConfigNotFound, match=r"^No config found in [^(]*\.$"):
        find_config(tmp_path)
    with pytest.raises(ConfigNotFound, match=r"^No config autohelix/perf/config\.yaml\.$"):
        find_config(tmp_path, "perf")


def test_a_missing_name_lists_the_configs(tmp_path):
    write(tmp_path / "autohelix" / "fast" / "config.yaml")
    write(tmp_path / "autohelix" / "gc" / "config.yaml")
    write(tmp_path / "autohelix" / "perf" / "config.yml")
    with pytest.raises(ConfigNotFound, match=r"^No config autohelix/perf/config\.yaml \(found "
                                             r"autohelix/perf/config\.yml; configs end in \.yaml\)\. "
                                             r"Configs: autohelix/fast/config\.yaml, autohelix/gc/config\.yaml\.$"):
        find_config(tmp_path, "perf")
    with pytest.raises(ValueError, match=r"not a config name: .*`perf` for autohelix/perf/config\.yaml\)\.$"):
        find_config(tmp_path, "autohelix/fast/config.yaml")


def test_a_yaml_file_directly_in_the_folder_is_not_a_config(tmp_path):
    write(tmp_path / "autohelix" / "perf.yaml")  # the old form: not found, no special error
    with pytest.raises(ConfigNotFound, match=r"^No config autohelix/perf/config\.yaml\.$"):
        find_config(tmp_path, "perf")
    with pytest.raises(ConfigNotFound, match=r"^No config found in "):
        find_config(tmp_path)
    assert project_configs(tmp_path) == []


def test_folders_without_a_config_and_dot_folders_are_not_experiments(tmp_path):
    only = write(tmp_path / "autohelix" / "perf" / "config.yaml")
    write(tmp_path / "autohelix" / "lib" / "metrics.py")  # shared helpers
    write(tmp_path / "autohelix" / "lib" / "helper.yaml")
    write(tmp_path / "autohelix" / ".old" / "config.yaml")  # dot-folders are never looked at
    write(tmp_path / "autohelix" / "README.md")
    assert project_configs(tmp_path) == ["autohelix/perf/config.yaml"]
    assert find_config(tmp_path) == only
    with pytest.raises(ConfigNotFound):
        find_config(tmp_path, "lib")
    with pytest.raises(ValueError, match="not a config name"):
        find_config(tmp_path, ".old")


def test_bare_lookup_takes_the_only_experiment(tmp_path):
    only = write(tmp_path / "autohelix" / "perf" / "config.yaml")
    write(tmp_path / "autohelix" / "perf" / "prompt.md")  # an experiment's material beside its config
    write(tmp_path / "autohelix" / "notes" / "config.yml")
    write(tmp_path / "autohelix.yaml")  # not a config: it doesn't compete
    assert find_config(tmp_path) == only
    write(tmp_path / "autohelix" / "fast" / "config.yaml")
    with pytest.raises(ConfigNotFound, match=r"^Several configs: "):
        find_config(tmp_path)


def test_bare_lookup_with_several_or_none_states_what_it_found(tmp_path):
    with pytest.raises(ConfigNotFound, match=r"^No config found in .*\.$") as none:
        find_config(tmp_path)
    assert none.value.configs == []
    write(tmp_path / "autohelix" / "fast" / "config.yml")
    write(tmp_path / "autohelix" / "perf" / "config.yml")
    with pytest.raises(ConfigNotFound, match=r"\(found autohelix/fast/config\.yml, autohelix/perf/config\.yml; "
                                             r"configs end in \.yaml\)"):
        find_config(tmp_path)
    write(tmp_path / "autohelix" / "perf" / "config.yaml")
    write(tmp_path / "autohelix" / "fast" / "config.yaml")
    with pytest.raises(ConfigNotFound, match=r"^Several configs: autohelix/fast/config\.yaml, "
                                             r"autohelix/perf/config\.yaml\.$") as several:
        find_config(tmp_path)
    assert several.value.configs == ["autohelix/fast/config.yaml", "autohelix/perf/config.yaml"]


def test_the_experiment_name_is_the_folders():
    assert config_name("autohelix/perf/config.yaml") == "perf"
    assert config_name(Path("autohelix/perf/config.yaml")) == "perf"
    assert config_name("autohelix.yaml") is None
    assert config_name("autohelix/perf/autohelix.yaml") is None
    assert config_name("autohelix/perf.yaml") is None
    assert config_name("autohelix/perf/other.yaml") is None
    assert config_name("examples/sorting/autohelix/sorting/config.yaml") is None
    assert run_command("autohelix/perf/config.yaml") == "autohelix run perf"
    assert new_run_name("autohelix/Fast_Sort/config.yaml", None) == "fast-sort"
    assert new_run_name("autohelix/perf/config.yaml", "x") == "x"


# --- run and resume ---------------------------------------------------------


@pytest.fixture
def folder_project(project):
    """The shared project, its experiment folder renamed to autohelix/perf/ and no .autohelix/ yet."""
    (project / CONFIG).parent.rename(project / "autohelix" / "perf")
    shutil.rmtree(project / ".autohelix")
    return project


def commit(project, message="change"):
    git(project, "add", "-A")
    git(project, "commit", "-qm", message)


def test_run_works_without_autohelix_dir_and_names_the_run_after_the_config(folder_project):
    out = folder_project.parent / "config-seen"
    config = folder_project / "autohelix" / "perf" / "config.yaml"
    data = yaml.safe_load(config.read_text())
    data["gates"] = [f'printf %s "$AUTOHELIX_CONFIG" > {out}']
    config.write_text(yaml.safe_dump(data))
    result = cli(folder_project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    paths = latest_run(folder_project)
    assert re.fullmatch(r"\d{8}-\d{4}-perf-[0-9a-z]{4}", paths.run_id)
    assert paths.read_identity()["config_path"] == "autohelix/perf/config.yaml"
    assert out.read_text() == str(paths.config)  # judges read the run's stored copy
    assert paths.config.read_text() == config.read_text()
    exclude = (folder_project / ".git" / "info" / "exclude").read_text().splitlines()
    assert ".autohelix/" in exclude and "__pycache__/" in exclude


def test_run_by_name(folder_project):
    write(folder_project / "autohelix" / "other" / "config.yaml", (folder_project / "autohelix" / "perf" / "config.yaml").read_text())
    result = cli(folder_project, "run")
    assert result.exit_code == 1
    assert ("Several configs: autohelix/other/config.yaml, autohelix/perf/config.yaml. "
            "Pick one: `autohelix run <name>`.") in result.output
    result = cli(folder_project, "run", "other", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert latest_run(folder_project).read_identity()["config_path"] == "autohelix/other/config.yaml"
    result = cli(folder_project, "run", "autohelix.yaml")
    assert result.exit_code == 2 and "not a config name" in result.output
    assert "(`perf` for autohelix/perf/config.yaml)." in flat(result.output)


def test_a_yml_file_is_pointed_out(folder_project):
    (folder_project / "autohelix" / "perf" / "config.yaml").rename(folder_project / "autohelix" / "perf" / "config.yml")
    result = cli(folder_project, "run")
    assert result.exit_code == 1
    assert "(found autohelix/perf/config.yml; configs end in .yaml)" in result.output


def test_bare_resume_with_several_configs_asks_for_a_run(folder_project):
    write(folder_project / "autohelix" / "other" / "config.yaml")
    result = cli(folder_project, "resume")  # no runs yet: how to start one
    assert result.exit_code == 1 and "Pick one: `autohelix run <name>`." in result.output
    result = cli(folder_project, "run", "perf", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    result = cli(folder_project, "resume")
    assert result.exit_code == 1
    assert ("Several configs: autohelix/other/config.yaml, autohelix/perf/config.yaml. "
            "Name a run or a config: `autohelix resume <id|name>`.") in result.output


def test_bare_resume_without_runs_creates_nothing(folder_project):
    result = cli(folder_project, "resume")
    assert result.exit_code == 1
    assert "No run to resume for autohelix/perf/config.yaml; start one with `autohelix run perf`." in result.output
    assert_untouched(folder_project)
    (folder_project / "autohelix" / "perf" / "config.yaml").unlink()
    result = cli(folder_project, "resume")
    assert result.exit_code == 1 and "Write autohelix/NAME/config.yaml: run `autohelix` in a terminal" in result.output
    assert_untouched(folder_project)


def assert_untouched(project):
    """No run state and no local excludes: a refusal writes nothing."""
    assert not (project / ".autohelix").exists()
    exclude = project / ".git" / "info" / "exclude"
    assert ".autohelix/" not in (exclude.read_text().splitlines() if exclude.exists() else [])


def test_a_top_level_autohelix_yaml_is_never_read(folder_project):
    result = cli(folder_project, "run", "-n", "1", "--no-report", "--land", "manual")
    assert result.exit_code == 0, result.output
    run_id = latest_run(folder_project).run_id
    write(folder_project / "autohelix.yaml", "goal: ''\nagent: {cli: mock}\n")  # invalid if it were read
    commit(folder_project)
    result = cli(folder_project, "resume", short_id(run_id), "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert latest_run(folder_project).read_identity()["config_path"] == "autohelix/perf/config.yaml"
    # A bare run and a bare resume take the only experiment, saying nothing of the file.
    result = cli(folder_project, "resume", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert "autohelix.yaml" not in result.output
    assert History(folder_project, run_id).get_last_iteration() == 3
    result = cli(folder_project, "run", "-n", "1", "--no-report", "--land", "manual")
    assert result.exit_code == 0, result.output
    assert "Config validation failed" not in result.output
    assert latest_run(folder_project).read_identity()["config_path"] == "autohelix/perf/config.yaml"


def test_a_run_whose_config_is_gone_continues_with_its_stored_copy(folder_project):
    result = cli(folder_project, "run", "-n", "1", "--no-report", "--land", "manual")
    assert result.exit_code == 0, result.output
    run_id = latest_run(folder_project).run_id
    (folder_project / "autohelix" / "perf").rename(folder_project / "autohelix" / "fast")  # the only config
    result = cli(folder_project, "resume", short_id(run_id), "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert (f"autohelix/perf/config.yaml is gone; run {short_id(run_id)} continues with the config it stored "
            "when it started.") in flat(result.output)
    assert History(folder_project, run_id).get_last_iteration() == 2
    config = RunData(folder_project, latest_run(folder_project).run_id).state()["config"]  # the web page's payload
    assert config["file"] == "autohelix/perf/config.yaml" and "missing" not in config and config["text"]
    listed = json.loads(cli(folder_project, "view", short_id(run_id), "--json").stdout)["run"]
    assert (listed["config_file"], listed["config_invalid"], listed["goal"]) == (
        "autohelix/perf/config.yaml", False, "Make a checked change")
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
    assert config["file"] == "autohelix/perf/config.yaml" and "missing" not in config and "agent" not in config
    listed = json.loads(cli(folder_project, "view", short, "--json").stdout)["run"]
    assert (listed["config_file"], listed["config_invalid"], listed["goal"]) == ("autohelix/perf/config.yaml", True, None)
    assert "autohelix/perf/config.yaml (invalid)" in cli(folder_project, "view", short, "--once").output
    (row,) = json.loads(cli(folder_project, "view", "--json").output)["runs"]
    assert row["config_invalid"] and "config_missing" not in row


def test_an_invalid_config_is_named(folder_project):
    write(folder_project / "autohelix" / "perf" / "config.yaml", "goal: ''\nagent: {cli: mock}\n")
    result = cli(folder_project, "run", "perf")
    assert result.exit_code == 1
    assert "Config validation failed. Edit autohelix/perf/config.yaml and try again." in result.output
    assert_untouched(folder_project)


def test_a_subfolder_without_a_config_points_at_the_project_above(folder_project):
    write(folder_project / "src" / "lib.py", "x = 1\n")
    commit(folder_project)
    result = cli(folder_project / "src", "run")
    assert result.exit_code == 1
    root = folder_project.resolve()
    assert (f"No config found in {root / 'src'}. {root} has autohelix/perf/config.yaml; "
            f"run from there or pass -p {root}.") in result.output
    assert not (folder_project / "src" / ".autohelix").exists()


def test_a_subproject_with_its_own_config_runs(folder_project):
    sub = folder_project / "examples" / "sub"
    write(sub / "code.py", "print('sub')\n")
    write(sub / "autohelix" / "sub" / "config.yaml", (folder_project / "autohelix" / "perf" / "config.yaml").read_text())
    commit(folder_project)  # committed: only the top-level autohelix/ is private
    result = cli(sub, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert latest_run(sub).read_identity()["config_path"] == "autohelix/sub/config.yaml"
    assert not (folder_project / ".autohelix").exists()


# --- refusals before anything is created ------------------------------------


def test_a_run_needs_an_experiment_config(folder_project):
    other = write(folder_project / "other.yaml", (folder_project / "autohelix" / "perf" / "config.yaml").read_text())
    commit(folder_project)
    with pytest.raises(RuntimeError, match=r"^other\.yaml isn't an experiment's config: a run's config is "
                                           r"autohelix/NAME/config\.yaml\.$"):
        Harness(folder_project, config_file=other)  # the API only: the CLI takes a name
    assert_untouched(folder_project)


def test_select_run_refuses_a_config_that_isnt_an_experiments(folder_project):
    """The run state's own check, for library callers that skip the Harness."""
    other = write(folder_project / "other.yaml", (folder_project / "autohelix" / "perf" / "config.yaml").read_text())
    commit(folder_project)
    with pytest.raises(RuntimeError, match=r"^other\.yaml isn't an experiment's config"):
        select_run(folder_project, other, new=True)
    assert not RunPaths(folder_project).runs.exists() or not any(RunPaths(folder_project).runs.iterdir())


def test_a_run_whose_config_isnt_an_experiments_is_not_read(folder_project):
    """A run's config is always an experiment's: one that names another file (an older
    version's autohelix.yaml) is refused by every reader of its stored config."""
    assert cli(folder_project, "run", "--no-report").exit_code == 0
    paths = latest_run(folder_project)
    data = paths.read_identity()
    data["config_path"] = "autohelix.yaml"
    paths.identity.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match=rf"^Run {short_id(paths.run_id)}: autohelix\.yaml isn't an "
                                           r"experiment's config: a run's config is autohelix/NAME/config\.yaml\.$"):
        load_run_config(paths)
    result = cli(folder_project, "resume", short_id(paths.run_id), "-n", "1", "--no-report")
    assert result.exit_code == 1 and "isn't an experiment's config" in flat(result.output)
    result = cli(folder_project, "run", "perf", "--import", short_id(paths.run_id), "--no-report")
    assert result.exit_code == 0, result.output  # continued from in a new run (the CHANGELOG's advice)


def test_run_outside_git_refuses_and_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))  # tmp_path may sit in a repository
    project = tmp_path / "project"
    write(project / "autohelix" / "perf" / "config.yaml", "goal: x\nagent: {cli: mock}\n")
    result = cli(project, "run")
    assert result.exit_code == 1
    assert "Not a git repository. AutoHelix requires git." in result.output
    assert sorted(p.name for p in project.iterdir()) == ["autohelix"]


def test_run_without_a_commit_refuses_and_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    project = tmp_path / "project"
    write(project / "autohelix" / "perf" / "config.yaml", "goal: x\nagent: {cli: mock}\n")
    git(project, "init", "-q")
    result = cli(project, "run")
    assert result.exit_code == 1
    assert "No commits yet; a run starts from a commit." in result.output
    assert "git add -A && git commit -m 'init'" in result.output
    assert sorted(p.name for p in project.iterdir()) == [".git", "autohelix"]
    exclude = project / ".git" / "info" / "exclude"
    assert ".autohelix/" not in (exclude.read_text().splitlines() if exclude.exists() else [])


# --- ask and review by hand -------------------------------------------------


def test_ask_and_review_read_the_only_config_in_the_folder(tmp_path, monkeypatch):
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    write(tmp_path / "autohelix" / "perf" / "config.yaml", yaml.safe_dump({"agent": {"cli": "codex", "model": "m"}}))
    assert (load_base_config(tmp_path).cli, load_base_config(tmp_path).model) == ("codex", "m")
    write(tmp_path / "autohelix" / "fast" / "config.yaml", yaml.safe_dump({"agent": {"cli": "codex"}}))
    assert load_base_config(tmp_path).cli == "claude-code"  # ambiguous: the defaults, not an error


def _answers(path, answer):
    return write(path, yaml.safe_dump({"goal": "g", "agent": {"cli": "mock", "mock": {
        "judge_answer": answer, "judge_reason": answer}}}))


def test_ask_prefers_the_harness_config_then_the_folders(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AUTOHELIX_USAGE_FILE", raising=False)
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    _answers(tmp_path / "autohelix" / "perf" / "config.yaml", "no")
    assert CliRunner().invoke(main, ["ask", "Done?"]).exit_code == 1  # the folder's only config
    monkeypatch.setenv(CONFIG_ENV, str(_answers(tmp_path.parent / f"{tmp_path.name}-run.yaml", "yes")))
    assert CliRunner().invoke(main, ["ask", "Done?"]).exit_code == 0
    monkeypatch.delenv(CONFIG_ENV)
    _answers(tmp_path / "autohelix.yaml", "yes")  # not a config: the folder's still answers
    assert CliRunner().invoke(main, ["ask", "Done?"]).exit_code == 1


def test_a_dirty_tree_is_refused_before_anything_is_written(folder_project):
    write(folder_project / "code.py", "print('edited')\n")
    result = cli(folder_project, "run", "perf")
    assert result.exit_code == 1 and "Uncommitted changes in tracked files" in result.output
    assert_untouched(folder_project)


def test_a_scope_config_error_is_reported_before_anything_is_written(folder_project):
    config = folder_project / "autohelix" / "perf" / "config.yaml"
    data = yaml.safe_load(config.read_text())
    data["scope"] = {"editable": ["../elsewhere/"]}
    config.write_text(yaml.safe_dump(data))
    result = cli(folder_project, "run", "perf")
    assert result.exit_code == 1 and "scope.editable: '../elsewhere/' has a `..` component" in flat(result.output)
    assert_untouched(folder_project)


def test_an_editable_pattern_matching_nothing_yet_only_warns(folder_project):
    """[01-2] The agent may be meant to create it: a warning, and the run starts."""
    config = folder_project / "autohelix" / "perf" / "config.yaml"
    data = yaml.safe_load(config.read_text())
    data["scope"] = {"editable": ["results/", "*.py"]}
    config.write_text(yaml.safe_dump(data))
    result = cli(folder_project, "run", "perf", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert "scope.editable 'results/' matches no committed file" in flat(result.output)
    assert "'*.py' matches" not in flat(result.output)


def test_no_run_to_resume_is_refused_before_anything_is_written(folder_project):
    with pytest.raises(NoRunToResume, match="No run to resume for autohelix/perf/config.yaml"):
        Harness(folder_project, create=False)
    assert_untouched(folder_project)
    write(folder_project / "autohelix" / "perf" / "config.yaml", "goal: ''\nagent: {cli: mock}\n")  # invalid too
    result = cli(folder_project, "resume")
    assert result.exit_code == 1 and "No run to resume for autohelix/perf/config.yaml" in result.output
    assert_untouched(folder_project)


def test_run_has_no_name_option(folder_project):
    result = cli(folder_project, "run", "perf", "--name", "x")
    assert result.exit_code == 2 and "No such option '--name'." in result.output
    assert_untouched(folder_project)


def test_an_old_name_key_is_an_error(folder_project):
    config = folder_project / "autohelix" / "perf" / "config.yaml"
    config.write_text(config.read_text() + "name: fast\n")
    result = cli(folder_project, "run", "perf")
    assert result.exit_code == 1
    assert "`name:` is gone: runs are named after the experiment folder; use autohelix/<name>/config.yaml" in flat(result.output)
    assert_untouched(folder_project)


def test_a_config_that_is_not_yaml_is_named(folder_project):
    result = cli(folder_project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    run_id = short_id(latest_run(folder_project).run_id)
    write(folder_project / "autohelix" / "perf" / "config.yaml", "goal: [\n")
    for args in (("run", "perf"), ("resume", run_id)):
        result = cli(folder_project, *args)
        assert result.exit_code == 1, (args, result.output)
        assert "Cannot parse autohelix/perf/config.yaml: " in result.output, args
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
