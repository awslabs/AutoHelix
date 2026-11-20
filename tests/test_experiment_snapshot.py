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
from autohelix.runs import RunPaths, git, project_runs, select_run, short_id
from tests.helpers import config_file, cli


@pytest.fixture
def folder(project):
    """The project's experiment folder renamed to autohelix/exp/ (private: ignored, untracked)."""
    folder = project / "autohelix" / "exp"
    config_file(project).parent.rename(folder)
    return folder


def names(project, folder):
    config = load_config(project, folder / "config.yaml")
    return [file.name for file in snapshot_files(config, project)]


def snapshot_of(project, folder):
    return snapshot_files(load_config(project, folder / "config.yaml"), project)


def test_the_folder_is_the_experiment_and_a_file_elsewhere_has_none(project, folder):
    assert load_config(project, folder / "config.yaml").experiment == folder
    (project / "other.yaml").write_text((folder / "config.yaml").read_text())  # a library caller's file
    config = load_config(project, project / "other.yaml")
    assert config.experiment is None
    with pytest.raises(RuntimeError, match="other.yaml isn't an experiment's config"):  # no run of it
        select_run(project, project / "other.yaml", new=True)


def test_what_goes_in_and_what_is_left_out(project, folder):
    (folder / "config.yaml").write_text((folder / "config.yaml").read_text() + "prompt_template: prompt.md\n")
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
    (project / "autohelix" / "spec.md").write_text("s\n")
    (folder / "metrics.py").symlink_to("../lib/metrics.py")  # links inside autohelix/: followed
    (folder / "lib").symlink_to("../lib")
    (folder / "spec.md").symlink_to(project / "autohelix" / "spec.md")
    assert names(project, folder) == ["lib/metrics.py", "lib/util.py", "metrics.py", "render.py",
                                      "sketches/a.md", "spec.md"]


def test_links_into_autohelix_are_followed_when_it_is_ignored(project, folder):
    (project / "autohelix" / "lib").mkdir()
    (project / "autohelix" / "lib" / "m.py").write_text("m\n")
    (folder / "lib").symlink_to("../lib")
    (project / ".git" / "info" / "exclude").write_text("/autohelix/\n")
    assert names(project, folder) == ["lib/m.py"]


def test_links_out_of_autohelix_are_refused_even_to_committed_files(project, folder):
    """A committed project file is in every copy: a command names it by its path."""
    (project / "scripts").mkdir()
    (project / "scripts" / "bench.py").write_text("b\n")
    git(project, "add", "scripts")
    git(project, "commit", "-qm", "a committed script")
    (folder / "bench.py").symlink_to("../../scripts/bench.py")
    (folder / "code.py").symlink_to(project / "code.py")
    with pytest.raises(ConfigError) as caught:
        names(project, folder)
    message = str(caught.value)
    assert "bench.py (a link to ../../scripts/bench.py, outside autohelix/)" in message
    assert f"code.py (a link to {project / 'code.py'}, outside autohelix/)" in message
    assert "name a project file by its path in a command instead" in message


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
    assert f"key (a link to {outside}, outside autohelix/)" in message
    assert "gone (a link to nothing-here, which doesn't exist)" in message
    assert "pipe (not a regular file)" in message


def test_links_into_dot_names_inside_autohelix_are_refused(project, folder):
    """`autohelix/.git/config` (its own repository's) may hold credentials; a dot-name is
    left out when it sits in the folder, so a link can't bring it in either."""
    git(project / "autohelix", "init", "-q")
    (project / "autohelix" / ".env").write_text("TOKEN=x\n")
    (project / "autohelix" / "notes").mkdir()
    (project / "autohelix" / "notes" / "ok.md").write_text("fine\n")
    (project / "autohelix" / "notes" / ".cache").mkdir()
    (folder / "gitconfig").symlink_to(project / "autohelix" / ".git" / "config")
    (folder / "state").symlink_to("../notes/.cache")
    (folder / "env").symlink_to("../.env")
    (folder / "ok.md").symlink_to("../notes/ok.md")
    with pytest.raises(ConfigError) as caught:
        names(project, folder)
    message = str(caught.value)
    assert f"gitconfig (a link to {project / 'autohelix' / '.git' / 'config'}, into .git)" in message
    assert "env (a link to ../.env, into .env)" in message
    assert "state (a link to ../notes/.cache, into .cache)" in message
    assert "ok.md" not in message
    (folder / "gitconfig").unlink()
    (folder / "env").unlink()
    (folder / "state").unlink()
    assert names(project, folder) == ["ok.md"]


def test_a_link_to_a_folder_holding_it_is_refused(project, folder):
    (folder / "sub").mkdir()
    (folder / "sub" / "loop").symlink_to("..")
    with pytest.raises(ConfigError, match=r"sub/loop \(a link to \.\., a folder holding autohelix/exp/\)"):
        names(project, folder)


def test_a_link_back_to_a_linked_folder_is_refused(project, folder):
    (project / "autohelix" / "lib" / "sub").mkdir(parents=True)
    (project / "autohelix" / "lib" / "sub" / "loop").symlink_to("..")
    (folder / "lib").symlink_to("../lib")
    with pytest.raises(ConfigError, match=r"lib/sub/loop \(a link to a folder that contains it\)"):
        names(project, folder)


def test_a_second_way_to_a_folder_is_refused_without_walking_each(project, folder, monkeypatch):
    """22 folders, each with two links to the next: 2^22 paths if each were walked."""
    monkeypatch.setattr(experiment, "MAX_REFUSED", 100)
    chain = project / "autohelix" / "chain"
    for level in range(1, 23):
        (chain / f"d{level}").mkdir(parents=True)
        if level > 1:
            for link in "ab":
                (chain / f"d{level - 1}" / link).symlink_to(f"../d{level}")
    (chain / "d22" / "f.txt").write_text("f\n")
    (folder / "a").symlink_to("../chain/d1")
    (folder / "b").symlink_to("../chain/d1")
    with pytest.raises(ConfigError) as caught:
        names(project, folder)
    message = str(caught.value)
    assert "; b (the same folder as a/, stored already)" in message
    assert "a/b (the same folder as a/a/, stored already)" in message
    assert message.count("stored already") == 22


def test_the_walk_stops_at_the_cap(project, folder, monkeypatch):
    nested = folder
    for level in range(5):  # no files: folders count too
        nested = nested / f"d{level}"
        nested.mkdir()
    monkeypatch.setattr(experiment, "MAX_FOLDERS", 3)
    with pytest.raises(ConfigError, match=r"autohelix/exp/ is too large to store with the run: over 3 folders"):
        names(project, folder)


def test_over_the_cap_is_refused_with_the_largest_files(project, folder, monkeypatch):
    (folder / "data.csv").write_bytes(b"x" * 3 * 1024 * 1024)
    (folder / "small.txt").write_text("s\n")
    monkeypatch.setattr(experiment, "MAX_BYTES", 2 * 1024 * 1024)
    with pytest.raises(ConfigError) as caught:
        names(project, folder)
    message = " ".join(str(caught.value).split())
    assert ("autohelix/exp/ is too large to store with the run: over 2 MB (at most 200 files, 200 folders and "
            "2 MB).") in message
    assert "Largest: data.csv (3.0 MB)." in message  # the walk stopped there
    assert "name them by path" in message
    monkeypatch.setattr(experiment, "MAX_BYTES", 10 * 1024 * 1024)
    monkeypatch.setattr(experiment, "MAX_FILES", 1)
    with pytest.raises(ConfigError, match="over 1 files"):
        names(project, folder)


def test_the_copy_keeps_exec_bits_drops_write_bits_and_can_be_removed(project, folder, tmp_path):
    (folder / "render.sh").write_text("#!/bin/sh\necho hi\n")
    (folder / "render.sh").chmod(0o755)
    (folder / "sub").mkdir()
    (folder / "sub" / "data.txt").write_text("d\n")
    (folder / "private.txt").write_text("p\n")
    (folder / "private.txt").chmod(0o600)
    files = snapshot_of(project, folder)
    staging = build_snapshot(files, tmp_path / "beside")
    assert staging.parent == tmp_path / "beside" and staging.name.startswith(".")
    assert stat.S_IMODE((staging / "render.sh").stat().st_mode) == 0o555
    assert stat.S_IMODE((staging / "sub" / "data.txt").stat().st_mode) == 0o444
    assert stat.S_IMODE((staging / "private.txt").stat().st_mode) == 0o400  # still private
    assert (staging / "sub").stat().st_mode & stat.S_IWUSR
    assert (staging / "sub" / "data.txt").read_text() == "d\n"


def test_a_failed_copy_leaves_nothing_and_names_the_file(project, folder, tmp_path):
    (folder / "a.txt").write_text("a\n")
    files = snapshot_of(project, folder)
    (folder / "a.txt").unlink()  # gone between the check and the copy
    with pytest.raises(ConfigError, match=r"The experiment folder's a.txt can't be stored with the run: "
                                          r"No such file or directory\."):
        build_snapshot(files, tmp_path / "beside")
    assert list((tmp_path / "beside").iterdir()) == []


def test_a_file_changed_during_the_copy_is_refused(project, folder, tmp_path):
    (folder / "a.txt").write_text("a\n")
    files = snapshot_of(project, folder)
    (folder / "a.txt").write_text("longer now\n")
    with pytest.raises(ConfigError, match="The experiment folder's a.txt changed while being stored"):
        build_snapshot(files, tmp_path / "beside")
    assert list((tmp_path / "beside").iterdir()) == []


def test_a_linked_experiment_folder_follows_the_link_rule(project, tmp_path):
    """`autohelix/exp` itself a link: stored only when it leads inside autohelix/."""
    text = config_file(project).read_text()
    shutil.rmtree(config_file(project).parent)  # autohelix/ holds only what this test makes
    for where in (tmp_path / "outside", project / "autohelix" / ".hidden", project / "autohelix" / "real"):
        where.mkdir()
        (where / "config.yaml").write_text(text)
        (where / "data.txt").write_text("d\n")
    link = project / "autohelix" / "exp"
    link.symlink_to(tmp_path / "outside")
    with pytest.raises(ConfigError) as caught:
        names(project, link)
    assert str(caught.value) == (f"autohelix/exp/ can't be stored with the run: it is a link to {tmp_path / 'outside'} "
                                 "(outside autohelix/). An experiment must be a folder inside autohelix/, not a link "
                                 "out of it.")
    link.unlink()
    link.symlink_to(".hidden")
    with pytest.raises(ConfigError, match=r"it is a link to \.hidden \(into \.hidden\)\. An experiment"):
        names(project, link)
    link.unlink()
    link.symlink_to(".")  # autohelix/ itself
    (project / "autohelix" / "config.yaml").write_text(text)
    with pytest.raises(ConfigError, match=r"it is a link to \. \(autohelix/ itself\)\. An experiment"):
        snapshot_files(load_config(project, project / "autohelix" / "exp" / "config.yaml"), project)
    link.unlink()
    link.symlink_to("real")
    assert names(project, link) == ["data.txt"]


def test_a_link_to_autohelix_or_above_is_refused_by_its_name(project, folder):
    """`up -> ..` would walk the other experiments and lib/."""
    (project / "autohelix" / "other").mkdir()
    (project / "autohelix" / "other" / "data.csv").write_text("x\n")
    (folder / "up").symlink_to("..")
    (folder / "self").symlink_to(".")
    with pytest.raises(ConfigError) as caught:
        names(project, folder)
    message = str(caught.value)
    assert ("self (a link to ., a folder holding autohelix/exp/); up (a link to .., a folder holding "
            "autohelix/exp/).") in message
    assert "other" not in message


def test_many_refusals_are_listed_up_to_the_cap(project, folder, monkeypatch):
    for n in range(4):
        os.mkfifo(folder / f"pipe{n}")
    monkeypatch.setattr(experiment, "MAX_REFUSED", 2)
    with pytest.raises(ConfigError, match=r"pipe0 \(not a regular file\); pipe1 \(not a regular file\) and 2 more\. "):
        names(project, folder)


def test_the_walk_stops_after_many_refusals(project, folder, monkeypatch):
    for n in range(6):
        (folder / f"data{n}.csv").symlink_to(f"../../../dataset/{n}.csv")  # outside autohelix/
    monkeypatch.setattr(experiment, "MAX_FILES", 3)
    monkeypatch.setattr(experiment, "MAX_REFUSED", 2)
    with pytest.raises(ConfigError) as caught:
        names(project, folder)
    message = str(caught.value)
    assert "data0.csv (a link to ../../../dataset/0.csv, outside autohelix/); data1.csv (" in message
    assert ", outside autohelix/) and more. Move these" in message
    assert "data2.csv" not in message


def test_a_killed_runs_staging_folder_is_removed_by_the_next_run(project, folder, tmp_path):
    runs = RunPaths(project).runs
    leftover = build_snapshot(snapshot_of(project, folder), runs)
    (tmp_path / "elsewhere").mkdir()
    (runs / ".experiment-link").symlink_to(tmp_path / "elsewhere")  # removed only by a real build
    result = cli(project, "run", "exp", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert not leftover.exists()
    assert (runs / ".experiment-link").is_symlink() and (tmp_path / "elsewhere").is_dir()


def test_the_staging_sweep_runs_when_the_copies_sweep_fails(project, folder, monkeypatch):
    from autohelix import runs as runs_module

    calls = []

    def broken(project):
        calls.append(project)
        raise OSError("copies base unreadable")

    monkeypatch.setattr(runs_module, "sweep_copies", broken)
    (folder / "lib").mkdir()
    (folder / "lib" / "m.py").write_text("m\n")  # a leftover with a read-only file in a subfolder
    leftover = build_snapshot(snapshot_of(project, folder), RunPaths(project).runs)
    assert (leftover / "lib" / "m.py").is_file()
    result = cli(project, "run", "exp", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert calls and not leftover.exists()


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


def test_a_folder_with_only_its_config_stores_an_empty_folder_and_shows_none(project, folder, tmp_path):
    """Stored for `resume` to compare; otherwise as no folder: no copy, no prompt line,
    `$AUTOHELIX_EXPERIMENT` unset."""
    import yaml
    from types import SimpleNamespace

    from autohelix.iteration import prepare_state

    log = tmp_path / "seen.log"
    unset = f'[ -n "${{AUTOHELIX_EXPERIMENT+set}}" ] && echo set >> {log} || echo unset >> {log}'
    data = yaml.safe_load((folder / "config.yaml").read_text())
    data.update(gates=[unset], stop=[f"{unset}; exit 1"])
    (folder / "config.yaml").write_text(yaml.safe_dump(data))
    assert cli(project, "run", "exp", "-n", "1", "--no-report").exit_code == 0
    [(paths, _)] = project_runs(project)
    assert paths.experiment.is_dir() and not list(paths.experiment.iterdir())
    assert log.read_text().splitlines() == ["unset"] * 4
    assert ".autohelix-iteration/experiment/" not in paths.iteration_dir(1).joinpath("prompt.md").read_text()
    prepare_state(paths, SimpleNamespace(working_dir=tmp_path / "copy"))
    assert not (tmp_path / "copy" / ".autohelix-iteration" / "experiment").exists()
    assert resumed(project, paths).exit_code == 0


def test_a_folder_with_only_its_config_stores_an_empty_one(project):
    assert cli(project, "run", "-n", "1", "--no-report").exit_code == 0
    [(paths, _)] = project_runs(project)
    assert paths.experiment.is_dir() and not experiment.has_files(paths.experiment)


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


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_file_is_refused_by_name(project, folder, run_of_folder):
    (folder / "data.txt").chmod(0)
    try:
        with pytest.raises(ConfigError, match=r"autohelix/exp/ can't be stored with the run: data.txt \(can't be read"):
            names(project, folder)
        result = resumed(project, run_of_folder)
        assert result.exit_code == 1 and not isinstance(result.exception, OSError), result.output  # no traceback
        assert "(it can no longer be stored: autohelix/exp/ can't be stored with the run: data.txt (can't be read" in (
            " ".join(result.output.split()))
    finally:
        (folder / "data.txt").chmod(0o644)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_folder_is_refused_by_name(project, folder):
    (folder / "sub").mkdir()
    (folder / "sub" / "a.txt").write_text("a\n")
    (folder / "sub").chmod(0)
    try:
        with pytest.raises(ConfigError, match=r"sub/ \(can't be read: Permission denied\)"):
            names(project, folder)
    finally:
        (folder / "sub").chmod(0o755)


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
    data = yaml.safe_load((folder / "config.yaml").read_text())
    data.update(gates=[seen], observe=[{"name": "look", "command": seen}], stop=[f"{seen}; exit 1"], **extra)
    (folder / "config.yaml").write_text(yaml.safe_dump(data))


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


def test_a_folder_with_only_its_config_gives_commands_no_experiment(project, tmp_path, monkeypatch):
    import yaml

    monkeypatch.setenv("AUTOHELIX_EXPERIMENT", str(tmp_path))  # not passed through from the user's shell
    log = tmp_path / "seen.log"
    unset = f'[ -n "${{AUTOHELIX_EXPERIMENT+set}}" ] && echo set >> {log} || echo unset >> {log}'
    data = yaml.safe_load(config_file(project).read_text())
    data.update(gates=[unset], observe=[{"name": "look", "command": unset}], stop=[f"{unset}; exit 1"])
    config_file(project).write_text(yaml.safe_dump(data))
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


def test_the_agent_and_judge_copies_keep_the_snapshots_modes(project, folder, tmp_path):
    """Read-only files, exec bits kept (the agent's `render.sh` runs); folders writable,
    so the copy can be removed. The judge's copy is made the same way (`copy_dir`)."""
    import subprocess
    from types import SimpleNamespace

    from autohelix.iteration import prepare_state
    from autohelix.safe_open import copy_dir

    (folder / "render.sh").write_text("#!/bin/sh\necho rendered\n")
    (folder / "render.sh").chmod(0o755)
    (folder / "sub").mkdir()
    (folder / "sub" / "data.txt").write_text("d\n")
    assert cli(project, "run", "exp", "-n", "1", "--no-report").exit_code == 0
    [(paths, _)] = project_runs(project)
    prepare_state(paths, SimpleNamespace(working_dir=tmp_path / "copy"))
    agent = tmp_path / "copy" / ".autohelix-iteration" / "experiment"
    assert copy_dir(paths.experiment, [], tmp_path / "judge", read_only=True)
    for copy in (agent, tmp_path / "judge"):
        assert stat.S_IMODE((copy / "render.sh").stat().st_mode) == 0o555
        assert stat.S_IMODE((copy / "sub" / "data.txt").stat().st_mode) == 0o444
        assert (copy / "sub").stat().st_mode & stat.S_IWUSR
        assert subprocess.run([copy / "render.sh"], capture_output=True, text=True).stdout == "rendered\n"
    shutil.rmtree(tmp_path / "copy")
    shutil.rmtree(tmp_path / "judge")


def with_ask_gate(path):
    """An `ask:` gate whose mock judge answers no unless its own copy has experiment/data.txt."""
    import yaml

    data = yaml.safe_load(path.read_text())
    data["agent"]["mock"]["judge_requires"] = ".autohelix-iteration/experiment/data.txt"
    data["gates"] = [{"ask": "Does it use the stored data?"}]
    path.write_text(yaml.safe_dump(data))


def test_an_ask_gate_in_a_run_reads_the_experiment_folder(project, folder):
    (folder / "data.txt").write_text("stored\n")
    with_ask_gate(folder / "config.yaml")
    result = cli(project, "run", "exp", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    [(paths, _)] = project_runs(project)
    [baseline, first] = History(project, paths.run_id).load()
    assert first.accepted and first.gates[0]["passed"] is True
    log = (paths.iteration_dir(1) / "gates" / "1" / "ask.log").read_text()
    assert "The experiment's folder, as the run stored it" in log


def test_an_ask_gate_in_a_run_of_a_folder_with_only_its_config_has_no_experiment(project):
    with_ask_gate(config_file(project))
    result = cli(project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    [(paths, _)] = project_runs(project)
    first = History(project, paths.run_id).load()[-1]
    assert not first.accepted and first.gates[0]["passed"] is False
    assert "experiment's folder" not in (paths.iteration_dir(1) / "gates" / "1" / "ask.log").read_text()


def test_a_folder_with_only_its_config_has_none_in_the_agent_copy_or_prompt(project, tmp_path):
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

    (folder / "data.txt").write_text("d\n")
    assert cli(project, "run", "exp", "-n", "1", "--no-report").exit_code == 0
    [(paths, _)] = project_runs(project)
    config = load_run_config(paths)
    assert "- experiment/: the experiment's folder" in build_report_prompt(config, project, project / "d.md", paths.run_id)
    shutil.rmtree(paths.experiment)
    assert "- experiment/:" not in build_report_prompt(config, project, project / "d.md", paths.run_id)


# --- a committed folder: frozen, not hidden; uncommitted files warned about ---


def mock_settings(folder, **settings):
    import yaml

    data = yaml.safe_load((folder / "config.yaml").read_text())
    data["agent"]["mock"].update(settings)
    (folder / "config.yaml").write_text(yaml.safe_dump(data))


def test_the_folder_is_frozen_but_not_hidden(project, folder):
    config = load_config(project, folder / "config.yaml")
    assert config.frozen_names() == ["autohelix/"]  # it stands for the config in it
    assert config.frozen_patterns() == ["autohelix/exp/config.yaml", "autohelix/"]
    assert config.hidden == ["autohelix/exp/config.yaml"]
    (project / "other.yaml").write_text((folder / "config.yaml").read_text())  # a library caller's file
    other = load_config(project, project / "other.yaml")  # autohelix/ is frozen for it too
    assert other.frozen_names() == ["other.yaml", "autohelix/"] and other.hidden == ["other.yaml"]
    shutil.rmtree(project / "autohelix")
    other = load_config(project, project / "other.yaml")  # shown and matched without one too
    assert other.frozen_names() == ["other.yaml", "autohelix/"]
    assert other.frozen_patterns() == ["other.yaml", "autohelix/"]


def test_an_agent_edit_to_a_committed_folder_is_reset_and_resume_continues(project, folder):
    (folder / "render.py").write_text("print('stored')\n")
    mock_settings(folder, change_file="autohelix/exp/render.py", change_content="print('edited')")
    git(project, "add", "-f", "autohelix")
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


def test_an_agent_edit_to_a_committed_lib_file_is_reset_and_resume_continues(project, folder):
    lib = project / "autohelix" / "lib"
    lib.mkdir()
    (lib / "metrics.py").write_text("print('stored')\n")
    (folder / "metrics.py").symlink_to("../lib/metrics.py")
    mock_settings(folder, change_file="autohelix/lib/metrics.py", change_content="print('edited')")
    git(project, "add", "-f", "autohelix")
    git(project, "commit", "-qm", "commit the experiment and its library")
    result = cli(project, "run", "exp", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    [(paths, _)] = project_runs(project)
    assert History(project, paths.run_id).load()[-1].discarded_frozen == ["autohelix/lib/metrics.py"]
    assert git(project, "show", f"autohelix/{paths.run_id}:autohelix/lib/metrics.py").stdout == "print('stored')\n"
    result = resumed(project, paths)  # the linked file still matches the snapshot
    assert result.exit_code == 0, result.output
    assert History(project, paths.run_id).load()[-1].discarded_frozen == ["autohelix/lib/metrics.py"]


def test_uncommitted_folder_files_warn_under_auto_landing(project, folder):
    from tests.test_run_notices import before_header

    (project / ".git" / "info" / "exclude").write_text("")  # autohelix/ not ignored: its files can move the branch
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
    assert "commit autohelix/exp/config.yaml" not in said
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
    git(project, "add", "-f", "autohelix/shared/kept.py")
    git(project, "commit", "-qm", "a committed helper")
    data = yaml.safe_load((folder / "config.yaml").read_text())
    data.update(gates=["python autohelix/exp/render.py", "python autohelix/shared/kept.py",
                       'python "$AUTOHELIX_EXPERIMENT/render.py"', "ls src/autohelix/x .autohelix/runs"],
                observe=[{"name": "m", "command": "python autohelix/lib/metrics.py"}],
                stop=["test -f 'autohelix/exp/render.py'"])
    (folder / "config.yaml").write_text(yaml.safe_dump(data))
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


def test_a_path_outside_the_folder_is_told_to_commit_it(project):
    from autohelix.checks import uncommitted_command_paths
    from autohelix.config import CommandSpec

    config = load_config(project)
    config.gates = [CommandSpec(command="sh autohelix/run.sh")]
    assert uncommitted_command_paths(config, project) == []  # not here: nothing to commit
    (project / "autohelix" / "run.sh").write_text("true\n")
    assert uncommitted_command_paths(config, project) == [
        "autohelix/run.sh (named by a command) isn't in the commit, so not in the copy: commit it, or link it "
        "into autohelix/demo/ and use `$AUTOHELIX_EXPERIMENT/...`."]
    git(project, "add", "-f", "autohelix/run.sh")
    git(project, "commit", "-qm", "commit it")
    assert uncommitted_command_paths(config, project) == []


def test_dot_slash_paths_warn_and_judge_entries_stay_quiet(project):
    """`./autohelix/...` is the same path; an `ask:` question or a `review:` prompt naming
    autohelix/ is read by a judge (which has `experiment/`), not run, so it isn't checked."""
    from autohelix.checks import uncommitted_command_paths
    from autohelix.config import CommandSpec, ObserveEntry

    (project / "autohelix" / "run.sh").write_text("true\n")
    (project / "autohelix" / "spec.md").write_text("spec\n")
    config = load_config(project)
    config.gates = [CommandSpec(command="sh ./autohelix/run.sh"),
                    CommandSpec(command="autohelix ask -- 'Does it follow autohelix/spec.md'",
                                label="ask: Does it follow autohelix/spec.md")]
    config.observe = [ObserveEntry(name="r", command="autohelix review -- 'Compare with autohelix/spec.md'",
                                   review="Compare with autohelix/spec.md")]
    assert uncommitted_command_paths(config, project) == [
        "autohelix/run.sh (named by a command) isn't in the commit, so not in the copy: commit it, or link it "
        "into autohelix/demo/ and use `$AUTOHELIX_EXPERIMENT/...`."]


def test_ref_names_stay_quiet_and_the_folder_itself_is_named_as_the_variable(project, folder):
    """`autohelix/<id>` is a run's ref, not a file; `cd autohelix/exp` is the folder."""
    from autohelix.checks import uncommitted_command_paths
    from autohelix.config import CommandSpec

    (folder / "render.sh").write_text("true\n")
    config = load_config(project, folder / "config.yaml")
    config.gates = [CommandSpec(command="git diff autohelix/20261119-1200-exp-ab12 -- code.py"),
                    CommandSpec(command="cd autohelix/exp && ./render.sh")]
    assert uncommitted_command_paths(config, project) == [
        "autohelix/exp (named by a command) isn't in the commit, so not in the copy: use `$AUTOHELIX_EXPERIMENT`."]


def test_names_the_snapshot_leaves_out_are_said_to_be_left_out(project, folder):
    from autohelix.checks import uncommitted_command_paths
    from autohelix.config import CommandSpec

    (folder / ".env").write_text("X=1\n")
    (folder / "prompt.md").write_text("{{ goal }}\n")
    (folder / "config.yaml").write_text((folder / "config.yaml").read_text() + "prompt_template: prompt.md\n")
    config = load_config(project, folder / "config.yaml")
    config.gates = [CommandSpec(command="cat autohelix/exp/config.yaml autohelix/exp/.env autohelix/exp/prompt.md"),
                    CommandSpec(command="cd autohelix/exp")]
    said = uncommitted_command_paths(config, project)
    assert said[0] == ("autohelix/exp/config.yaml (named by a command) isn't in the commit, so not in the copy: "
                       "the run doesn't store it in `$AUTOHELIX_EXPERIMENT`: the config is `$AUTOHELIX_CONFIG`.")
    for name in (".env", "prompt.md"):
        assert (f"autohelix/exp/{name} (named by a command) isn't in the commit, so not in the copy: the run "
                "doesn't store it in `$AUTOHELIX_EXPERIMENT` (it leaves out the config, its template, dotfiles and "
                "`__pycache__/`): commit it.") in said
    # The folder holds only its config (and what is left out): its runs get no variable.
    assert said[-1] == ("autohelix/exp (named by a command) isn't in the commit, so not in the copy: commit it (it "
                        "holds only its config, so commands get no `$AUTOHELIX_EXPERIMENT`).")


def test_the_advice_holds_with_a_linked_autohelix_folder(project, tmp_path):
    """`autohelix/` a link out of the project: the folder is named as the config names it."""
    from autohelix.checks import uncommitted_command_paths
    from autohelix.config import CommandSpec

    (tmp_path / "private" / "exp").mkdir(parents=True)
    (tmp_path / "private" / "exp" / "config.yaml").write_text(config_file(project).read_text())
    (tmp_path / "private" / "exp" / "render.py").write_text("print(1)\n")
    shutil.rmtree(project / "autohelix")
    (project / "autohelix").symlink_to(tmp_path / "private")
    config = load_config(project, project / "autohelix" / "exp" / "config.yaml")
    config.gates = [CommandSpec(command="python autohelix/exp/render.py")]
    assert uncommitted_command_paths(config, project) == [
        "autohelix/exp/render.py (named by a command) isn't in the commit, so not in the copy: use "
        "`$AUTOHELIX_EXPERIMENT/render.py`."]


def test_the_advice_follows_what_the_snapshot_stores(project, folder):
    """`cfg.yaml -> config.yaml` is left out like the config; a stored link's names
    are under it."""
    from autohelix.checks import uncommitted_command_paths
    from autohelix.config import CommandSpec

    (folder / "cfg.yaml").symlink_to("config.yaml")
    (project / "autohelix" / "lib").mkdir()
    (project / "autohelix" / "lib" / "m.py").write_text("m\n")
    (folder / "lib").symlink_to("../lib")
    config = load_config(project, folder / "config.yaml")
    config.gates = [CommandSpec(command="cat autohelix/exp/cfg.yaml && python autohelix/exp/lib/m.py")]
    assert uncommitted_command_paths(config, project) == [
        "autohelix/exp/cfg.yaml (named by a command) isn't in the commit, so not in the copy: the run doesn't store "
        "it in `$AUTOHELIX_EXPERIMENT` (it leaves out the config, its template, dotfiles and `__pycache__/`): "
        "commit it.",
        "autohelix/exp/lib/m.py (named by a command) isn't in the commit, so not in the copy: use "
        "`$AUTOHELIX_EXPERIMENT/lib/m.py`."]


def test_a_file_named_autohelix_is_frozen_as_the_folder_would_be(project):
    """Every config's autohelix/ is frozen and shown (a run's config is always in it)."""
    (project / "other.yaml").write_text(config_file(project).read_text())  # a library caller's file
    shutil.rmtree(project / "autohelix")
    (project / "autohelix").write_text("not a folder\n")
    assert load_config(project, project / "other.yaml").frozen_names() == ["other.yaml", "autohelix/"]
