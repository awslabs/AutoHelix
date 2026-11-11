"""Where candidate copies live: a private per-run folder outside the project."""

import os
import stat

import pytest

import autohelix.runs as runs
from autohelix.harness import Harness
from autohelix.runs import (COPIES_ENV, RunPaths, claim_copies, delete, copies_base, ensure_copies,
                            git, project_key, remove_copies)
from tests.test_runs import configure, project  # noqa: F401 - fixture


def mode(path):
    return stat.S_IMODE(path.lstat().st_mode)


def test_copies_live_outside_the_project_and_go_at_segment_end(project, copies_dir, tmp_path):  # noqa: F811
    seen = tmp_path / "seen"
    configure(project, constraints=[f'pwd -P >> {seen}; echo "$GIT_CEILING_DIRECTORIES" >> {seen}'])
    harness = Harness(project, land_mode="manual")
    root = harness.paths.copies
    assert root == copies_dir / project_key(project.resolve()) / harness.paths.run_id
    assert not root.exists()  # created on first use
    assert [p.name for p in root.parent.iterdir()] == ["project.json"]  # which checkout, for the sweep
    harness.run(max_iterations=1, report=False)
    lines = seen.read_text().splitlines()
    assert lines[0] == str(root / "iter-0-checks")  # baseline
    assert lines[1].split(os.pathsep)[0] == str(root)
    assert lines[2] == str(root / "iter-1-checks")
    assert [p.name for p in root.parent.iterdir()] == ["project.json"]  # the run's folder is gone


def test_a_copy_without_git_finds_no_enclosing_repository(project, monkeypatch, tmp_path):  # noqa: F811
    home = tmp_path / "home"  # a home directory can itself be a git repository
    git(home.parent, "init", "-q", str(home))
    monkeypatch.setenv(COPIES_ENV, str(home / ".cache" / "autohelix" / "copies"))
    configure(project, constraints=["rm -rf .git && ! git rev-parse --git-dir"])
    harness = Harness(project, land_mode="manual")
    harness.run(max_iterations=1, report=False)
    assert harness.history.load()[-1].accepted


def test_observers_use_the_recorded_base(project, monkeypatch, tmp_path):  # noqa: F811
    harness = Harness(project, land_mode="manual")
    root = ensure_copies(harness.run_state.copies_root)
    (root / "iter-1-agent").mkdir()
    harness.close()
    monkeypatch.setenv(COPIES_ENV, str(tmp_path / "elsewhere"))
    paths = RunPaths(project, harness.paths.run_id)
    assert paths.copies == root
    delete(paths)  # from a shell with another base
    assert not root.exists()


def test_a_changed_base_cleans_the_old_one(project, monkeypatch, tmp_path):  # noqa: F811
    first = Harness(project, land_mode="manual")
    old = ensure_copies(first.run_state.copies_root)
    (old / "iter-4-agent").mkdir()
    first.close()
    monkeypatch.setenv(COPIES_ENV, str(tmp_path / "new"))
    second = Harness(project, land_mode="manual")
    assert second.paths.run_id == first.paths.run_id
    assert not old.exists()
    assert second.run_state.copies_root == (tmp_path / "new").resolve() / old.parent.name / old.name
    assert second.paths.copies == second.run_state.copies_root
    assert os.environ["GIT_CEILING_DIRECTORIES"].split(os.pathsep)[0] == str(second.run_state.copies_root)
    second.close()


@pytest.mark.parametrize("record", ["relative/base", "/tmp/../tmp", "/a:b"])
def test_an_invalid_record_is_ignored(run_paths, copies_dir, record):
    run_paths.copies_record.write_text(record + "\n")
    assert run_paths.copies == copies_dir / project_key(run_paths.project) / "test"


def test_a_hostile_record_cannot_empty_another_folder(run_paths, tmp_path):
    victim = tmp_path / "victim"
    (victim / "keep").mkdir(parents=True)
    hostile = tmp_path / "hostile"
    (hostile / project_key(run_paths.project)).mkdir(parents=True)
    (hostile / project_key(run_paths.project) / "test").symlink_to(victim)
    run_paths.copies_record.write_text(f"{hostile.resolve()}\n")
    warnings = []
    claim_copies(run_paths, warnings.append)  # a changed base: cleans the recorded one
    assert (victim / "keep").is_dir() and warnings
    run_paths.copies_record.write_text(f"{hostile.resolve()}\n")
    delete(run_paths)
    assert (victim / "keep").is_dir()


def test_a_symlinked_base_is_resolved(run_paths, monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    monkeypatch.setenv(COPIES_ENV, str(tmp_path / "link"))
    root = claim_copies(run_paths)
    assert root == real.resolve() / project_key(run_paths.project) / "test"
    assert run_paths.copies_record.read_text().strip() == str(real.resolve())


def test_default_base_follows_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv(COPIES_ENV)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert copies_base() == tmp_path.resolve() / "autohelix" / "copies"
    monkeypatch.setenv("XDG_CACHE_HOME", "relative")  # ignored, as the XDG spec says
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert copies_base() == (tmp_path / "home").resolve() / ".cache" / "autohelix" / "copies"
    monkeypatch.setenv(COPIES_ENV, "relative")
    with pytest.raises(RuntimeError, match="absolute"):
        copies_base()


def test_project_key_is_safe_for_the_ceiling(tmp_path):
    key = project_key(tmp_path / "my proj:v2")
    assert key.startswith("my_proj_v2-") and os.pathsep not in key
    assert project_key(tmp_path / "a") != project_key(tmp_path / "b" / "a")


def test_own_folders_are_made_private_and_others_refused(run_paths, copies_dir):
    root = run_paths.copies
    root.parent.mkdir(mode=0o755)
    root.parent.chmod(0o755)
    assert ensure_copies(root) == root
    assert mode(root.parent) == 0o700 and mode(root) == 0o700
    root.rmdir()
    root.symlink_to(copies_dir)
    with pytest.raises(RuntimeError, match="not a directory owned by you"):
        ensure_copies(root)


def test_a_base_others_can_write_must_be_sticky(run_paths, tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    monkeypatch.setenv(COPIES_ENV, str(shared))
    with pytest.raises(RuntimeError, match="not writable by others"):
        claim_copies(run_paths)
    shared.chmod(0o1777)  # like /tmp: others can't rename our entries
    assert claim_copies(run_paths) == run_paths.copies_under(shared.resolve())


def test_a_concurrently_removed_project_folder_is_recreated(run_paths, monkeypatch):
    root = run_paths.copies
    real, calls = runs._own_dir, []

    def racing(path, *, create):
        calls.append(path)
        if len(calls) == 2:  # another run's segment end removes the empty project folder
            path.parent.rmdir()
        real(path, create=create)

    monkeypatch.setattr(runs, "_own_dir", racing)
    assert ensure_copies(root).is_dir() and len(calls) == 4


def test_remove_copies_keeps_the_project_folder_of_other_runs(run_paths):
    root = ensure_copies(run_paths.copies)
    other = ensure_copies(root.parent / "other")
    (root / "iter-1-agent" / "x").mkdir(parents=True)
    remove_copies(root)
    assert not root.exists() and other.is_dir()


def test_delete_finishes_when_copies_cannot_be_removed(project, monkeypatch):  # noqa: F811
    harness = Harness(project, land_mode="manual")
    harness.run(max_iterations=1, report=False)
    root = ensure_copies(harness.paths.copies)
    (root / "iter-9-agent").mkdir()

    def fail(path):
        raise OSError("busy")

    monkeypatch.setattr("autohelix.workspace.remove_copy", fail)
    warnings = []
    delete(harness.paths, force=True, warn=warnings.append)
    assert "busy" in warnings[0]
    assert not harness.paths.run_dir.exists()
    assert git(project, "rev-parse", "--verify", harness.paths.ref, check=False).returncode != 0


def test_delete_removes_copies(project):  # noqa: F811
    harness = Harness(project, land_mode="manual")
    harness.run(max_iterations=1, report=False)
    root = ensure_copies(harness.paths.copies)
    (root / "iter-9-agent" / "x").mkdir(parents=True)
    delete(harness.paths, force=True)
    assert not root.exists() and not harness.paths.run_dir.exists()
