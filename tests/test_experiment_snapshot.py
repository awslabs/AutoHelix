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
