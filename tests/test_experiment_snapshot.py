"""An experiment folder's snapshot (`autohelix/NAME/` stored as `runs/<id>/experiment/`):
what goes in, what is refused before anything is written, the modes, and `delete`."""

import os
import shutil
import stat

import pytest

from autohelix import experiment
from autohelix.config import ConfigError, load_config
from autohelix.experiment import build_snapshot, snapshot_files
from autohelix.history import History
from autohelix.runs import RunPaths, git, project_runs, short_id
from tests.helpers import cli


@pytest.fixture
def folder(project):
    """The project's config moved into autohelix/exp/ (untracked, as a private one is)."""
    folder = project / "autohelix" / "exp"
    folder.mkdir(parents=True)
    (folder / "autohelix.yaml").write_text((project / "autohelix.yaml").read_text())
    git(project, "rm", "-q", "autohelix.yaml")
    git(project, "commit", "-qm", "config moves into autohelix/exp/")
    return folder


def names(project, folder):
    config = load_config(project, folder / "autohelix.yaml")
    return [file.name for file in snapshot_files(config, project)]


def test_the_folder_is_the_experiment_and_the_project_config_has_none(project, folder):
    assert load_config(project, folder / "autohelix.yaml").experiment == folder
    (project / "autohelix.yaml").write_text((folder / "autohelix.yaml").read_text())
    config = load_config(project, project / "autohelix.yaml")
    assert config.experiment is None and snapshot_files(config, project) is None


def test_what_goes_in_and_what_is_left_out(project, folder):
    (folder / "autohelix.yaml").write_text((folder / "autohelix.yaml").read_text() + "prompt_template: prompt.md\n")
    (folder / "prompt.md").write_text("{{ goal }}\n")
    (folder / "render.py").write_text("print(1)\n")
    (folder / ".env").write_text("SECRET=1\n")
    (folder / ".cache").mkdir()
    (folder / ".cache" / "x").write_text("x")
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "render.pyc").write_bytes(b"\0")
    (folder / "sketches" / "__pycache__").mkdir(parents=True)
    (folder / "sketches" / "a.md").write_text("a\n")
    lib = project / "autohelix" / "lib"
    lib.mkdir()
    (lib / "metrics.py").write_text("m\n")
    (lib / "util.py").write_text("u\n")
    (project / "notes").mkdir()
    (project / "notes" / "design.md").write_text("d\n")
    (folder / "metrics.py").symlink_to("../lib/metrics.py")  # links inside the checkout: followed
    (folder / "lib").symlink_to("../lib")
    (folder / "design.md").symlink_to(project / "notes" / "design.md")
    assert names(project, folder) == ["design.md", "lib/metrics.py", "lib/util.py", "metrics.py", "render.py",
                                      "sketches/a.md"]


def test_links_outside_the_checkout_and_non_regular_files_are_refused_by_name(project, folder, tmp_path):
    outside = tmp_path / "secret"
    outside.write_text("key\n")
    (folder / "key").symlink_to(outside)
    (folder / "gone").symlink_to("nothing-here")
    os.mkfifo(folder / "pipe")
    with pytest.raises(ConfigError) as caught:
        names(project, folder)
    message = str(caught.value)
    assert message.startswith("autohelix/exp/ can't be stored with the run: ")
    assert f"key (a link to {outside}, outside the checkout)" in message
    assert "gone (a link to nothing-here, which doesn't exist)" in message
    assert "pipe (not a regular file)" in message


def test_a_link_to_a_folder_holding_it_is_refused(project, folder):
    (folder / "sub").mkdir()
    (folder / "sub" / "loop").symlink_to("..")
    with pytest.raises(ConfigError, match=r"sub/loop \(a link to a folder that contains it\)"):
        names(project, folder)


def test_over_the_cap_is_refused_with_the_largest_files(project, folder, monkeypatch):
    (folder / "data.csv").write_bytes(b"x" * 3 * 1024 * 1024)
    (folder / "small.txt").write_text("s\n")
    monkeypatch.setattr(experiment, "MAX_BYTES", 2 * 1024 * 1024)
    with pytest.raises(ConfigError) as caught:
        names(project, folder)
    message = " ".join(str(caught.value).split())
    assert "autohelix/exp/ is too large to store with the run: 2 files, 3.0 MB (at most 200 files and 2 MB)." in message
    assert "Largest: data.csv (3.0 MB), small.txt (1 KB)." in message
    assert "name them by path" in message
    monkeypatch.setattr(experiment, "MAX_BYTES", 10 * 1024 * 1024)
    monkeypatch.setattr(experiment, "MAX_FILES", 1)
    with pytest.raises(ConfigError, match="2 files"):
        names(project, folder)


def test_the_copy_keeps_exec_bits_drops_write_bits_and_can_be_removed(project, folder, tmp_path):
    (folder / "render.sh").write_text("#!/bin/sh\necho hi\n")
    (folder / "render.sh").chmod(0o755)
    (folder / "sub").mkdir()
    (folder / "sub" / "data.txt").write_text("d\n")
    files = snapshot_files(load_config(project, folder / "autohelix.yaml"), project)
    staging = build_snapshot(files, tmp_path / "beside")
    assert staging.parent == tmp_path / "beside" and staging.name.startswith(".")
    assert stat.S_IMODE((staging / "render.sh").stat().st_mode) == 0o555
    assert stat.S_IMODE((staging / "sub" / "data.txt").stat().st_mode) == 0o444
    assert (staging / "sub").stat().st_mode & stat.S_IWUSR
    assert (staging / "sub" / "data.txt").read_text() == "d\n"


def test_a_failed_copy_leaves_nothing(project, folder, tmp_path):
    (folder / "a.txt").write_text("a\n")
    files = snapshot_files(load_config(project, folder / "autohelix.yaml"), project)
    (folder / "a.txt").unlink()  # gone between the check and the copy
    with pytest.raises(FileNotFoundError):
        build_snapshot(files, tmp_path / "beside")
    assert list((tmp_path / "beside").iterdir()) == []


def test_a_killed_runs_staging_folder_is_removed_by_the_next_run(project, folder, tmp_path):
    runs = RunPaths(project).runs
    leftover = build_snapshot(snapshot_files(load_config(project, folder / "autohelix.yaml"), project), runs)
    (tmp_path / "elsewhere").mkdir()
    (runs / ".experiment-link").symlink_to(tmp_path / "elsewhere")  # removed only by a real build
    result = cli(project, "run", "exp", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert not leftover.exists()
    assert (runs / ".experiment-link").is_symlink() and (tmp_path / "elsewhere").is_dir()


def test_the_staging_sweep_runs_when_the_copies_sweep_fails(project, folder, monkeypatch):
    from autohelix import runs as runs_module

    def broken(project):
        raise OSError("copies base unreadable")

    monkeypatch.setattr(runs_module, "sweep_copies", broken)
    leftover = build_snapshot(
        snapshot_files(load_config(project, folder / "autohelix.yaml"), project), RunPaths(project).runs)
    result = cli(project, "run", "exp", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert not leftover.exists()


def test_a_new_run_stores_the_folder_and_delete_removes_it(project, folder):
    (folder / "render.sh").write_text("#!/bin/sh\n")
    (folder / "render.sh").chmod(0o755)
    (folder / "lib").mkdir()
    (folder / "lib" / "m.py").write_text("m\n")
    result = cli(project, "run", "exp", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    [(paths, _)] = project_runs(project)
    stored = paths.experiment
    assert sorted(p.relative_to(stored).as_posix() for p in stored.rglob("*")) == ["lib", "lib/m.py", "render.sh"]
    assert stat.S_IMODE((stored / "render.sh").stat().st_mode) == 0o555
    assert not [p for p in paths.runs.iterdir() if p.name.startswith(".")]  # the staging folder was renamed
    result = cli(project, "delete", "--force", short_id(paths.run_id))
    assert result.exit_code == 0, result.output
    assert not paths.run_dir.exists()


def test_a_folder_with_only_its_config_stores_an_empty_folder(project, folder):
    assert cli(project, "run", "exp", "-n", "1", "--no-report").exit_code == 0
    [(paths, _)] = project_runs(project)
    assert paths.experiment.is_dir() and not list(paths.experiment.iterdir())


def test_the_project_config_stores_no_folder(project):
    assert cli(project, "run", "-n", "1", "--no-report").exit_code == 0
    [(paths, _)] = project_runs(project)
    assert not paths.experiment.exists()


@pytest.mark.parametrize("args", [["run", "exp", "--check"], ["run", "exp", "-n", "1", "--no-report"]])
def test_a_refused_folder_leaves_no_run(project, folder, tmp_path, args):
    (tmp_path / "secret").write_text("key\n")
    (folder / "key").symlink_to(tmp_path / "secret")
    result = cli(project, *args)
    assert result.exit_code == 1
    assert "autohelix/exp/ can't be stored with the run: key (a link to" in " ".join(result.output.split())
    assert not list((project / ".autohelix").iterdir())  # no run folder, no lock, no excludes' marks
    assert git(project, "for-each-ref", "refs/autohelix/").stdout == ""
    assert project_runs(project) == []
    assert not RunPaths(project).runs.exists()


# --- resume compares the live folder with the snapshot ---


@pytest.fixture
def run_of_folder(project, folder):
    """One finished run of autohelix/exp/ (render.sh, data.txt): its paths."""
    (folder / "render.sh").write_text("#!/bin/sh\necho hi\n")
    (folder / "render.sh").chmod(0o755)
    (folder / "data.txt").write_text("d\n")
    result = cli(project, "run", "exp", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    [(paths, _)] = project_runs(project)
    return paths


def resumed(project, paths):
    return cli(project, "resume", short_id(paths.run_id), "-n", "1", "--no-report")


def test_an_unchanged_folder_resumes(project, folder, run_of_folder):
    (folder / "__pycache__").mkdir()  # left out, so not a change
    (folder / ".notes").write_text("n\n")
    result = resumed(project, run_of_folder)
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(("edit", "words"), [
    (lambda f: (f / "data.txt").write_text("e\n"), "changed: data.txt"),
    (lambda f: (f / "render.sh").chmod(0o644), "changed: render.sh"),
    (lambda f: (f / "new.py").write_text("n\n"), "added: new.py"),
    (lambda f: (f / "data.txt").unlink(), "removed: data.txt"),
])
def test_a_changed_folder_refuses(project, folder, run_of_folder, edit, words):
    edit(folder)
    result = resumed(project, run_of_folder)
    assert result.exit_code == 1
    short = short_id(run_of_folder.run_id)
    message = " ".join(result.output.split())
    assert f"autohelix/exp/ changed since run {short} started ({words}); the run keeps the experiment folder" in message
    assert f"`autohelix run exp --import {short}`" in message
    assert "restore the files." in message
    assert History(project, run_of_folder.run_id).get_last_iteration() == 1  # nothing ran


def test_several_changes_are_named_together(project, folder, run_of_folder):
    (folder / "data.txt").write_text("e\n")
    (folder / "render.sh").unlink()
    (folder / "b.txt").write_text("b\n")
    result = resumed(project, run_of_folder)
    assert result.exit_code == 1
    assert "(changed: data.txt; added: b.txt; removed: render.sh)" in " ".join(result.output.split())


def test_a_folder_that_can_no_longer_be_stored_refuses(project, folder, run_of_folder, tmp_path):
    (tmp_path / "secret").write_text("s\n")
    (folder / "key").symlink_to(tmp_path / "secret")
    result = resumed(project, run_of_folder)
    assert result.exit_code == 1
    assert "(it can no longer be stored: autohelix/exp/ can't be stored with the run: key (a link to" in " ".join(
        result.output.split())


def test_a_deleted_folder_continues(project, folder, run_of_folder):
    shutil.rmtree(folder)
    result = resumed(project, run_of_folder)
    assert result.exit_code == 0, result.output
    message = " ".join(result.output.split())
    assert f"autohelix/exp/ is gone; run {short_id(run_of_folder.run_id)} continues with the files it stored" in message


def test_a_run_without_a_snapshot_resumes(project, folder, run_of_folder):
    shutil.rmtree(run_of_folder.experiment)  # as a run created before snapshots
    (folder / "new.py").write_text("n\n")
    result = resumed(project, run_of_folder)
    assert result.exit_code == 0, result.output


# --- who sees it: commands, the agent, the report ---


def with_commands(folder, log, **extra):
    """The experiment's config with a gate, an observe and a stop command that each log
    `$AUTOHELIX_CALL` and what they read of `$AUTOHELIX_EXPERIMENT`, then remove a file
    from their copy of it."""
    import yaml

    seen = (f'echo "$AUTOHELIX_ITERATION $AUTOHELIX_CALL $(cat "$AUTOHELIX_EXPERIMENT/data.txt") '
            f'$(test -x "$AUTOHELIX_EXPERIMENT/render.sh" && echo x)" >> {log}; '
            'rm -f "$AUTOHELIX_EXPERIMENT/data.txt"')
    data = yaml.safe_load((folder / "autohelix.yaml").read_text())
    data.update(gates=[seen], observe=[{"name": "look", "command": seen}], stop=[f"{seen}; exit 1"], **extra)
    (folder / "autohelix.yaml").write_text(yaml.safe_dump(data))


def test_commands_get_their_own_copy_of_the_snapshot(project, folder, tmp_path):
    (folder / "data.txt").write_text("stored\n")
    (folder / "render.sh").write_text("#!/bin/sh\n")
    (folder / "render.sh").chmod(0o755)
    log = tmp_path / "seen.log"
    with_commands(folder, log)
    result = cli(project, "run", "exp", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    # The baseline's gate, observe entry and stop command, then iteration 1's: each read
    # the stored file (exec bit kept), though every one removed it from its copy.
    assert log.read_text().splitlines() == [
        "0 gates/1 stored x", "0 observe/look stored x", "0 stop/1 stored x",
        "1 gates/1 stored x", "1 observe/look stored x", "1 stop/1 stored x"]
    [(paths, _)] = project_runs(project)
    assert (paths.experiment / "data.txt").read_text() == "stored\n"


def test_without_a_folder_commands_get_no_experiment(project, tmp_path, monkeypatch):
    import yaml

    monkeypatch.setenv("AUTOHELIX_EXPERIMENT", str(tmp_path))  # not passed through from the user's shell
    log = tmp_path / "seen.log"
    unset = f'[ -n "${{AUTOHELIX_EXPERIMENT+set}}" ] && echo set >> {log} || echo unset >> {log}'
    data = yaml.safe_load((project / "autohelix.yaml").read_text())
    data.update(gates=[unset], observe=[{"name": "look", "command": unset}], stop=[f"{unset}; exit 1"])
    (project / "autohelix.yaml").write_text(yaml.safe_dump(data))
    git(project, "commit", "-qam", "commands")
    result = cli(project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert log.read_text().splitlines() == ["unset"] * 6


def test_the_agent_copy_has_the_snapshot_and_the_prompt_names_it(project, folder, tmp_path):
    from types import SimpleNamespace

    from autohelix.iteration import prepare_state

    (folder / "data.txt").write_text("stored\n")
    result = cli(project, "run", "exp", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    [(paths, _)] = project_runs(project)
    prompt = paths.iteration_dir(1).joinpath("prompt.md").read_text()
    assert "- .autohelix-iteration/experiment/ — the experiment's folder as the run stored it" in prompt
    copy = tmp_path / "copy"
    prepare_state(paths, SimpleNamespace(working_dir=copy))
    assert (copy / ".autohelix-iteration" / "experiment" / "data.txt").read_text() == "stored\n"


def with_ask_gate(config_file):
    """An `ask:` gate whose mock judge answers no unless its own copy has experiment/data.txt."""
    import yaml

    data = yaml.safe_load(config_file.read_text())
    data["agent"]["mock"]["judge_requires"] = ".autohelix-iteration/experiment/data.txt"
    data["gates"] = [{"ask": "Does it use the stored data?"}]
    config_file.write_text(yaml.safe_dump(data))


def test_an_ask_gate_in_a_run_reads_the_experiment_folder(project, folder):
    (folder / "data.txt").write_text("stored\n")
    with_ask_gate(folder / "autohelix.yaml")
    result = cli(project, "run", "exp", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    [(paths, _)] = project_runs(project)
    [baseline, first] = History(project, paths.run_id).load()
    assert first.accepted and first.gates[0]["passed"] is True
    log = (paths.iteration_dir(1) / "gates" / "1" / "ask.log").read_text()
    assert "The experiment's folder, as the run stored it" in log


def test_an_ask_gate_in_a_run_without_a_folder_has_no_experiment(project):
    with_ask_gate(project / "autohelix.yaml")
    git(project, "commit", "-qam", "ask gate")
    result = cli(project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    [(paths, _)] = project_runs(project)
    first = History(project, paths.run_id).load()[-1]
    assert not first.accepted and first.gates[0]["passed"] is False
    assert "experiment's folder" not in (paths.iteration_dir(1) / "gates" / "1" / "ask.log").read_text()


def test_a_run_without_a_folder_has_none_in_the_agent_copy_or_prompt(project, tmp_path):
    from types import SimpleNamespace

    from autohelix.iteration import prepare_state

    assert cli(project, "run", "-n", "1", "--no-report").exit_code == 0
    [(paths, _)] = project_runs(project)
    assert ".autohelix-iteration/experiment/" not in paths.iteration_dir(1).joinpath("prompt.md").read_text()
    prepare_state(paths, SimpleNamespace(working_dir=tmp_path / "copy"))
    assert not (tmp_path / "copy" / ".autohelix-iteration" / "experiment").exists()


def test_the_report_prompt_names_the_stored_folder(project, folder):
    from autohelix.report import build_report_prompt
    from autohelix.runs import load_run_config

    assert cli(project, "run", "exp", "-n", "1", "--no-report").exit_code == 0
    [(paths, _)] = project_runs(project)
    config = load_run_config(paths)
    assert "- experiment/: the experiment's folder" in build_report_prompt(config, project, project / "d.md", paths.run_id)
    shutil.rmtree(paths.experiment)
    assert "- experiment/:" not in build_report_prompt(config, project, project / "d.md", paths.run_id)


# --- a committed folder: frozen, not hidden; uncommitted files warned about ---


def mock_settings(folder, **settings):
    import yaml

    data = yaml.safe_load((folder / "autohelix.yaml").read_text())
    data["agent"]["mock"].update(settings)
    (folder / "autohelix.yaml").write_text(yaml.safe_dump(data))


def test_the_folder_is_frozen_but_not_hidden(project, folder):
    config = load_config(project, folder / "autohelix.yaml")
    assert config.frozen_names() == ["autohelix/exp/autohelix.yaml", "autohelix/exp/"]
    assert config.hidden == ["autohelix/exp/autohelix.yaml"]


def test_an_agent_edit_to_a_committed_folder_is_reset_and_resume_continues(project, folder):
    (folder / "render.py").write_text("print('stored')\n")
    mock_settings(folder, change_file="autohelix/exp/render.py", change_content="print('edited')")
    git(project, "add", "autohelix")
    git(project, "commit", "-qm", "commit the experiment")
    result = cli(project, "run", "exp", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    [(paths, _)] = project_runs(project)
    record = History(project, paths.run_id).load()[-1]
    assert record.discarded_frozen == ["autohelix/exp/render.py"]
    # In the agent's copy (it appended to the committed file), never in the run's code.
    assert git(project, "show", f"autohelix/{paths.run_id}:autohelix/exp/render.py").stdout == "print('stored')\n"
    result = resumed(project, paths)  # the folder still matches its snapshot
    assert result.exit_code == 0, result.output


def test_uncommitted_folder_files_warn_under_auto_landing(project, folder):
    from tests.test_run_notices import before_header

    git(project, "add", "autohelix")
    git(project, "commit", "-qm", "commit the config")
    for name in ("render.py", "b.py", ".env", "__pycache__/x.pyc"):
        (folder / name).parent.mkdir(exist_ok=True)
        (folder / name).write_text("x\n")
    (project / "autohelix" / "lib").mkdir()
    (project / "autohelix" / "lib" / "m.py").write_text("m\n")
    (folder / "lib").symlink_to("../lib")  # stored as lib/m.py; git lists the link
    result = cli(project, "run", "exp", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    said = before_header(result)
    assert ("commit autohelix/exp/ first (b.py, lib, render.py): a commit on main after this run "
            "lands makes `resume` refuse.") in said
    assert "commit autohelix/exp/autohelix.yaml" not in said
    (project / ".git" / "info" / "exclude").write_text("/autohelix/\n")
    result = cli(project, "run", "exp", "-n", "1", "--no-report")
    assert "commit autohelix/exp/" not in result.output  # ignored files never move the branch


# --- run --check: commands naming autohelix/ paths the commit lacks ---


def test_check_warns_on_a_command_naming_an_uncommitted_autohelix_path(project, folder):
    import yaml

    (project / "autohelix" / "lib").mkdir()
    for path in ("autohelix/exp/render.py", "autohelix/lib/metrics.py", "autohelix/shared/kept.py"):
        (project / path).parent.mkdir(exist_ok=True)
        (project / path).write_text("print(1)\n")
    git(project, "add", "autohelix/shared/kept.py")
    git(project, "commit", "-qm", "a committed helper")
    data = yaml.safe_load((folder / "autohelix.yaml").read_text())
    data.update(gates=["python autohelix/exp/render.py", "python autohelix/shared/kept.py",
                       'python "$AUTOHELIX_EXPERIMENT/render.py"', "ls src/autohelix/x .autohelix/runs"],
                observe=[{"name": "m", "command": "python autohelix/lib/metrics.py"}],
                stop=["test -f 'autohelix/exp/render.py'"])
    (folder / "autohelix.yaml").write_text(yaml.safe_dump(data))
    result = cli(project, "run", "exp", "--check")
    assert result.exit_code == 0, result.output
    said = " ".join(result.output.split())
    assert ("autohelix/exp/render.py (named by a command) isn't in the commit, so not in the copy: use "
            "`$AUTOHELIX_EXPERIMENT/render.py`.") in said
    assert said.count("autohelix/exp/render.py (named by a command)") == 1  # once per path
    assert ("autohelix/lib/metrics.py (named by a command) isn't in the commit, so not in the copy: commit it, "
            "or link it into autohelix/exp/ and use `$AUTOHELIX_EXPERIMENT/...`.") in said
    for quiet in ("kept.py", "src/autohelix/x", ".autohelix/runs", "$AUTOHELIX_EXPERIMENT/render.py (named"):
        assert f"{quiet} (named by a command)" not in said


def test_the_project_config_is_told_to_commit_the_path(project):
    from autohelix.checks import uncommitted_command_paths
    from autohelix.config import CommandSpec

    config = load_config(project)
    config.gates = [CommandSpec(command="sh autohelix/run.sh")]
    assert uncommitted_command_paths(config, project) == [
        "autohelix/run.sh (named by a command) isn't in the commit, so not in the copy: commit it."]
    (project / "autohelix").mkdir()
    (project / "autohelix" / "run.sh").write_text("true\n")
    git(project, "add", "autohelix")
    git(project, "commit", "-qm", "commit it")
    assert uncommitted_command_paths(config, project) == []


def test_dot_slash_paths_warn_and_judge_entries_stay_quiet(project):
    """`./autohelix/...` is the same path; an `ask:` question or a `review:` prompt naming
    autohelix/ is read by a judge (which has `experiment/`), not run, so it isn't checked."""
    from autohelix.checks import uncommitted_command_paths
    from autohelix.config import CommandSpec, ObserveEntry

    config = load_config(project)
    config.gates = [CommandSpec(command="sh ./autohelix/run.sh"),
                    CommandSpec(command="autohelix ask -- 'Does it follow autohelix/spec.md'",
                                label="ask: Does it follow autohelix/spec.md")]
    config.observe = [ObserveEntry(name="r", command="autohelix review -- 'Compare with autohelix/spec.md'",
                                   review="Compare with autohelix/spec.md")]
    assert uncommitted_command_paths(config, project) == [
        "autohelix/run.sh (named by a command) isn't in the commit, so not in the copy: commit it."]
