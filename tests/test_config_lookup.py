"""Config lookup: autohelix.yaml or autohelix/<name>.yaml, and no setup step before `run`."""

import json
import re
import shutil

import pytest
import yaml
from click.testing import CliRunner

from autohelix.cli import main
from autohelix.config import ConfigNotFound, find_config
from autohelix.judge import CONFIG_ENV, load_base_config
from autohelix.harness import Harness
from autohelix.runs import NoRunToResume, RunPaths, git, short_id
from autohelix.viewer import RunData


def write(path, text="goal: x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def cli(project, *args):
    return CliRunner().invoke(main, [*args, "-p", str(project)])


def flat(text):
    """Output without the line wraps of a narrow terminal."""
    return " ".join(text.split())


# --- the lookup -------------------------------------------------------------


def test_a_name_finds_its_file_in_the_folder(tmp_path):
    perf = write(tmp_path / "autohelix" / "perf.yaml")
    write(tmp_path / "autohelix.yaml")
    assert find_config(tmp_path, "perf") == perf


def test_a_missing_name_lists_the_configs(tmp_path):
    write(tmp_path / "autohelix.yaml")
    write(tmp_path / "autohelix" / "fast.yaml")
    write(tmp_path / "autohelix" / "perf.yml")
    with pytest.raises(ConfigNotFound, match=r"^No config autohelix/perf\.yaml \(found autohelix/perf\.yml; "
                                             r"configs end in \.yaml\)\. "
                                             r"Configs: autohelix\.yaml, autohelix/fast\.yaml\.$"):
        find_config(tmp_path, "perf")
    with pytest.raises(ValueError, match="not a config name"):
        find_config(tmp_path, "autohelix/fast.yaml")


def test_bare_lookup_prefers_autohelix_yaml_then_the_only_file(tmp_path):
    only = write(tmp_path / "autohelix" / "perf.yaml")
    write(tmp_path / "autohelix" / "prompts" / "helper.yaml")  # subfolders hold templates and scripts
    write(tmp_path / "autohelix" / "notes.yml")
    write(tmp_path / "autohelix" / ".perf.yaml")  # an editor's backup
    assert find_config(tmp_path) == only
    write(tmp_path / "autohelix" / "fast.yaml")
    top = write(tmp_path / "autohelix.yaml")
    assert find_config(tmp_path) == top


def test_bare_lookup_with_several_or_none_states_what_it_found(tmp_path):
    with pytest.raises(ConfigNotFound, match=r"^No config found in .*\.$") as none:
        find_config(tmp_path)
    assert none.value.configs == []
    write(tmp_path / "autohelix.yml")
    write(tmp_path / "autohelix" / "perf.yml")
    with pytest.raises(ConfigNotFound, match=r"\(found autohelix\.yml, autohelix/perf\.yml; configs end in \.yaml\)"):
        find_config(tmp_path)
    write(tmp_path / "autohelix" / "perf.yaml")
    write(tmp_path / "autohelix" / "fast.yaml")
    with pytest.raises(ConfigNotFound, match=r"^Several configs: autohelix/fast\.yaml, autohelix/perf\.yaml\.$") as several:
        find_config(tmp_path)
    assert several.value.configs == ["autohelix/fast.yaml", "autohelix/perf.yaml"]


# --- run and resume ---------------------------------------------------------


@pytest.fixture
def folder_project(project):
    """The shared project, its config moved to autohelix/perf.yaml and no .autohelix/ yet."""
    (project / "autohelix").mkdir()
    git(project, "mv", "autohelix.yaml", "autohelix/perf.yaml")
    git(project, "commit", "-qm", "config folder")
    shutil.rmtree(project / ".autohelix")
    return project


def commit(project, message="change"):
    git(project, "add", "-A")
    git(project, "commit", "-qm", message)


def test_run_works_without_autohelix_dir_and_names_the_run_after_the_config(folder_project):
    out = folder_project.parent / "config-seen"
    config = folder_project / "autohelix" / "perf.yaml"
    data = yaml.safe_load(config.read_text())
    data["gates"] = [f'printf %s "$AUTOHELIX_CONFIG" > {out}']
    config.write_text(yaml.safe_dump(data))
    commit(folder_project)
    result = cli(folder_project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    paths = RunPaths(folder_project)
    assert re.fullmatch(r"\d{8}-\d{4}-perf-[0-9a-z]{4}", paths.run_id)
    assert paths.read_identity()["config_path"] == "autohelix/perf.yaml"
    assert out.read_text() == str(config.resolve())
    exclude = (folder_project / ".git" / "info" / "exclude").read_text().splitlines()
    assert ".autohelix/" in exclude and "__pycache__/" in exclude


def test_run_by_name(folder_project):
    write(folder_project / "autohelix" / "other.yaml", (folder_project / "autohelix" / "perf.yaml").read_text())
    commit(folder_project)
    result = cli(folder_project, "run")
    assert result.exit_code == 1
    assert ("Several configs: autohelix/other.yaml, autohelix/perf.yaml. "
            "Pick one: `autohelix run <name>`.") in result.output
    result = cli(folder_project, "run", "other", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert RunPaths(folder_project).read_identity()["config_path"] == "autohelix/other.yaml"
    result = cli(folder_project, "run", "autohelix.yaml")
    assert result.exit_code == 2 and "not a config name" in result.output
    assert "a bare `autohelix run` reads autohelix.yaml" in flat(result.output)


def test_a_yml_file_is_pointed_out(folder_project):
    git(folder_project, "mv", "autohelix/perf.yaml", "autohelix/perf.yml")
    commit(folder_project)
    result = cli(folder_project, "run")
    assert result.exit_code == 1
    assert "(found autohelix/perf.yml; configs end in .yaml)" in result.output


def test_bare_resume_with_several_configs_asks_for_a_run(folder_project):
    write(folder_project / "autohelix" / "other.yaml")
    result = cli(folder_project, "resume")  # no runs yet: how to start one
    assert result.exit_code == 1 and "Pick one: `autohelix run <name>`." in result.output
    result = cli(folder_project, "run", "perf", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    result = cli(folder_project, "resume")
    assert result.exit_code == 1
    assert ("Several configs: autohelix/other.yaml, autohelix/perf.yaml. "
            "Name a run: `autohelix resume <id>`.") in result.output


def test_bare_resume_without_runs_creates_nothing(folder_project):
    result = cli(folder_project, "resume")
    assert result.exit_code == 3
    assert "No run to resume for autohelix/perf.yaml; start one with `autohelix run perf`." in result.output
    assert_untouched(folder_project)
    (folder_project / "autohelix" / "perf.yaml").unlink()
    result = cli(folder_project, "resume")
    assert result.exit_code == 1 and "Write one with `autohelix setup`." in result.output
    assert_untouched(folder_project)


def assert_untouched(project):
    """No run state and no local excludes: a refusal writes nothing."""
    assert not (project / ".autohelix").exists()
    exclude = project / ".git" / "info" / "exclude"
    assert ".autohelix/" not in (exclude.read_text().splitlines() if exclude.exists() else [])


def test_resume_id_keeps_its_config_beside_a_root_one(folder_project):
    result = cli(folder_project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    run_id = RunPaths(folder_project).run_id
    write(folder_project / "autohelix.yaml", "goal: ''\nagent: {backend: mock}\n")  # invalid if it were read
    commit(folder_project)
    result = cli(folder_project, "resume", short_id(run_id), "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert RunPaths(folder_project).read_identity()["config_path"] == "autohelix/perf.yaml"
    # A bare run takes autohelix.yaml and says what else there is.
    result = cli(folder_project, "run")
    assert "Using autohelix.yaml; autohelix/ also has: perf (`autohelix run perf`)" in result.output
    assert "Config validation failed. Edit autohelix.yaml and try again." in result.output
    result = cli(folder_project, "resume")
    assert "Using autohelix.yaml; autohelix/ also has: perf (resume their runs by id)" in result.output
    assert result.exit_code == 3 and "No run to resume for autohelix.yaml" in result.output


def test_a_run_whose_config_is_gone_is_never_given_another(folder_project):
    result = cli(folder_project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    run_id = RunPaths(folder_project).run_id
    git(folder_project, "mv", "autohelix/perf.yaml", "autohelix/fast.yaml")  # the only config, found by a lookup
    git(folder_project, "commit", "-qm", "rename")
    base = RunPaths(folder_project).read_identity()["base_commit"][:12]
    gone = (f"Run {short_id(run_id)} was started with autohelix/perf.yaml, which is gone; restore it "
            f"(`git checkout {base} -- autohelix/perf.yaml`), or start a new run of another config "
            "(configs: autohelix/fast.yaml).")
    result = cli(folder_project, "resume", short_id(run_id))
    assert result.exit_code == 1 and gone in flat(result.output), result.output
    result = cli(folder_project, "report")
    assert result.exit_code == 1 and gone in flat(result.output), result.output
    config = RunData(folder_project).state()["config"]  # the web page's payload
    assert (config["file"], config.get("missing")) == ("autohelix/perf.yaml", True)
    listed = json.loads(cli(folder_project, "view", "--json").output)["run"]
    assert (listed["config_file"], listed["config_missing"], listed["goal"]) == ("autohelix/perf.yaml", True, None)
    assert "autohelix/perf.yaml (missing)" in cli(folder_project, "view", "--once").output
    (row,) = json.loads(cli(folder_project, "dashboard", "--json").output)["runs"]
    assert row["config_missing"] and not row["config_invalid"]
    assert "autohelix/perf.yaml (missing)" in cli(folder_project, "dashboard", "--once").output


def test_an_invalid_config_is_named(folder_project):
    write(folder_project / "autohelix" / "perf.yaml", "goal: ''\nagent: {backend: mock}\n")
    commit(folder_project)
    result = cli(folder_project, "run", "perf")
    assert result.exit_code == 1
    assert "Config validation failed. Edit autohelix/perf.yaml and try again." in result.output
    assert_untouched(folder_project)


def test_a_subfolder_without_a_config_points_at_the_project_above(folder_project):
    write(folder_project / "src" / "lib.py", "x = 1\n")
    commit(folder_project)
    result = cli(folder_project / "src", "run")
    assert result.exit_code == 1
    root = folder_project.resolve()
    assert (f"No config found in {root / 'src'}. {root} has autohelix/perf.yaml; "
            f"run from there or pass -p {root}.") in result.output
    assert not (folder_project / "src" / ".autohelix").exists()


def test_a_subproject_with_its_own_config_runs(folder_project):
    sub = folder_project / "examples" / "sub"
    write(sub / "code.py", "print('sub')\n")
    write(sub / "autohelix.yaml", (folder_project / "autohelix" / "perf.yaml").read_text())
    commit(folder_project)
    result = cli(sub, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert RunPaths(sub).read_identity()["config_path"] == "autohelix.yaml"
    assert not (folder_project / ".autohelix").exists()


# --- refusals before anything is created ------------------------------------


def test_run_outside_git_refuses_and_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))  # tmp_path may sit in a repository
    project = tmp_path / "project"
    write(project / "autohelix.yaml", "goal: x\nagent: {backend: mock}\n")
    result = cli(project, "run")
    assert result.exit_code == 1
    assert "Not a git repository. AutoHelix requires git." in result.output
    assert sorted(p.name for p in project.iterdir()) == ["autohelix.yaml"]


def test_run_without_a_commit_refuses_and_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    project = tmp_path / "project"
    write(project / "autohelix.yaml", "goal: x\nagent: {backend: mock}\n")
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
    write(tmp_path / "autohelix" / "perf.yaml", yaml.safe_dump({"agent": {"backend": "codex", "model": "m"}}))
    assert (load_base_config(tmp_path).backend, load_base_config(tmp_path).model) == ("codex", "m")
    write(tmp_path / "autohelix" / "fast.yaml", yaml.safe_dump({"agent": {"backend": "codex"}}))
    assert load_base_config(tmp_path).backend == "claude"  # ambiguous: the defaults, not an error


def _answers(path, answer):
    return write(path, yaml.safe_dump({"goal": "g", "agent": {"backend": "mock", "settings": {
        "judge_answer": answer, "judge_reason": answer}}}))


def test_ask_prefers_the_harness_config_then_autohelix_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AUTOHELIX_USAGE_FILE", raising=False)
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    _answers(tmp_path / "autohelix" / "perf.yaml", "no")
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


def test_readiness_is_checked_before_anything_is_written(folder_project):
    config = folder_project / "autohelix" / "perf.yaml"
    data = yaml.safe_load(config.read_text())
    data["scope"] = {"editable": ["missing/"]}
    config.write_text(yaml.safe_dump(data))
    commit(folder_project)
    result = cli(folder_project, "run", "perf")
    assert result.exit_code == 1 and "No files found matching editable pattern 'missing/'" in result.output
    assert_untouched(folder_project)


def test_no_run_to_resume_is_refused_before_anything_is_written(folder_project):
    with pytest.raises(NoRunToResume, match="No run to resume for autohelix/perf.yaml"):
        Harness(folder_project, create=False)
    assert_untouched(folder_project)
    write(folder_project / "autohelix" / "perf.yaml", "goal: ''\nagent: {backend: mock}\n")  # invalid too
    commit(folder_project)
    result = cli(folder_project, "resume")
    assert result.exit_code == 3 and "No run to resume for autohelix/perf.yaml" in result.output
    assert_untouched(folder_project)


def test_run_has_no_name_option(folder_project):
    result = cli(folder_project, "run", "perf", "--name", "x")
    assert result.exit_code == 2 and "No such option '--name'." in result.output
    assert_untouched(folder_project)


def test_an_old_name_key_is_an_error(folder_project):
    config = folder_project / "autohelix" / "perf.yaml"
    config.write_text(config.read_text() + "name: fast\n")
    commit(folder_project)
    result = cli(folder_project, "run", "perf")
    assert result.exit_code == 1
    assert "`name:` is gone: runs are named after the config file; use autohelix/<name>.yaml" in flat(result.output)
    assert_untouched(folder_project)


def test_a_config_that_is_not_yaml_is_named(folder_project):
    result = cli(folder_project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    run_id = short_id(RunPaths(folder_project).run_id)
    write(folder_project / "autohelix" / "perf.yaml", "goal: [\n")
    commit(folder_project)
    for args in (("run", "perf"), ("resume", run_id), ("report", run_id)):
        result = cli(folder_project, *args)
        assert result.exit_code == 1, (args, result.output)
        assert "Error: Cannot parse autohelix/perf.yaml: " in result.output, args
        assert "Traceback" not in result.output and result.exception is None or isinstance(
            result.exception, SystemExit), args
    assert cli(folder_project, "view", run_id, "--json").exit_code == 0
    assert cli(folder_project, "dashboard", "--json").exit_code == 0


def test_report_without_runs_creates_nothing(folder_project):
    result = cli(folder_project, "report")
    assert result.exit_code == 1 and "No iterations run yet" in result.output
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
    assert json.loads(cli(folder_project, "dashboard", "--json").output)["branch"] == branch
