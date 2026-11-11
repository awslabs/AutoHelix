"""Content policy regressions across Git plumbing, raw files and submodules."""

import os
import shutil

import pytest
import yaml

from autohelix.checkpoint import CandidateChanged, ContentCheckpoint, ContentGuard, PinnedGit, Scope
from autohelix.harness import AutoHelixRunError, Harness
from autohelix.agents import AgentRunResult
from autohelix.runs import git
from tests.test_runs import project


def test_restore_refuses_parent_symlink_without_writing_outside(project, tmp_path):
    (project / ".gitignore").write_text("protected\n")
    protected = project / "protected"
    protected.mkdir()
    (protected / "file").write_text("baseline")
    git(project, "add", "-f", "protected/file", ".gitignore")
    git(project, "commit", "-qm", "protected")
    checkpoint = ContentCheckpoint(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_text("user uncommitted work")
    shutil.rmtree(protected)
    protected.symlink_to(outside)
    with pytest.raises(RuntimeError, match="parent symlink"):
        checkpoint.restore(checkpoint.changed())
    assert (outside / "file").read_text() == "user uncommitted work"


def test_replaced_worktree_root_cannot_redirect_checkpoint(project, tmp_path):
    checkpoint = ContentCheckpoint(project)
    original = tmp_path / "saved"
    project.rename(original)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "code.py").write_text("user work")
    project.symlink_to(outside)
    with pytest.raises(RuntimeError, match="removed or replaced"):
        checkpoint.restore(["code.py"])
    assert (outside / "code.py").read_text() == "user work"


def test_restore_preserves_dirty_raw_bytes_and_mode_under_filter(project):
    (project / ".gitattributes").write_text("code.py filter=trim\n")
    git(project, "config", "filter.trim.clean", "sed s/DIRTY/CLEAN/g")
    git(project, "config", "filter.trim.smudge", "cat")
    (project / "code.py").write_text("DIRTY candidate bytes")
    git(project, "add", ".gitattributes", "code.py")
    git(project, "commit", "-qm", "filter")
    (project / "code.py").chmod(0o664)
    checkpoint = ContentCheckpoint(project)
    (project / "code.py").write_text("other bytes")
    (project / "code.py").chmod(0o600)
    checkpoint.restore(checkpoint.changed())
    assert (project / "code.py").read_text() == "DIRTY candidate bytes"
    assert (project / "code.py").stat().st_mode & 0o777 == 0o664


@pytest.mark.parametrize("child", [False, True])
def test_filter_hidden_raw_dirt_cannot_be_accepted_as_noop(project, tmp_path, child):
    if child:
        add_submodule(project, tmp_path)
    target = project / "sub" if child else project
    git(target, "config", "user.email", "test@example.com")
    git(target, "config", "user.name", "Test")
    git(target, "config", "filter.trim.clean", "sed s/DIRTY/CLEAN/g")
    git(target, "config", "filter.trim.smudge", "cat")
    (target / ".gitattributes").write_text("code.py filter=trim\n")
    (target / "code.py").write_text("CLEAN baseline")
    git(target, "add", ".gitattributes", "code.py")
    git(target, "commit", "-qm", "filter")
    if child:
        git(project, "commit", "-qam", "child filter")
    checkpoint = ContentCheckpoint(project)
    (target / "code.py").write_text("DIRTY baseline")
    assert not git(target, "status", "--porcelain", "--", "code.py").stdout.strip()
    with pytest.raises(RuntimeError, match="cannot recreate|dirty submodule"):
        checkpoint.commit(Scope(project.resolve(), project.resolve()), "accept", validated=checkpoint.capture())


def test_filter_roundtrip_publishes_recreated_validated_bytes(project):
    git(project, "config", "filter.trim.clean", "sed s/DIRTY/CLEAN/g")
    git(project, "config", "filter.trim.smudge", "sed s/CLEAN/DIRTY/g")
    (project / ".gitattributes").write_text("code.py filter=trim\n")
    (project / "code.py").write_text("DIRTY baseline")
    git(project, "add", ".gitattributes", "code.py")
    git(project, "commit", "-qm", "filter")
    checkpoint = ContentCheckpoint(project)
    (project / "code.py").write_text("DIRTY improvement")
    commit = checkpoint.commit(Scope(project.resolve(), project.resolve()), "accept",
                               validated=checkpoint.capture())
    assert commit
    assert git(project, "show", f"{commit}:code.py").stdout == "CLEAN improvement"
    assert git(project, "cat-file", "--filters", f"{commit}:code.py").stdout == "DIRTY improvement"


def test_standalone_unborn_git_retains_cached_inventory(project, tmp_path):
    empty = tmp_path / "unborn"
    empty.mkdir()
    git(empty, "init", "-q")
    (empty / ".gitignore").write_text("ignored.txt\n")
    (empty / "ignored.txt").write_text("staged ignored content")
    git(empty, "add", "-f", "ignored.txt")
    with pytest.raises(CandidateChanged):
        with ContentGuard(empty):
            git(empty, "rm", "--cached", "ignored.txt")
            (empty / "ignored.txt").write_text("changed behind removed index")


def test_nongit_and_dangling_symlink_mutations_are_detected(tmp_path):
    (tmp_path / "draft").write_text("candidate")
    (tmp_path / "link").symlink_to("missing")
    with pytest.raises(CandidateChanged):
        with ContentGuard(tmp_path):
            (tmp_path / "draft").write_text("judge edit")
            (tmp_path / "link").unlink()
            (tmp_path / "link").symlink_to("another-missing")


@pytest.mark.parametrize("scope", [{}, {"frozen": ["frozen.txt"]}])
def test_project_subdirectory_never_accepts_staged_sibling(project, monkeypatch, scope):
    sub = project / "sub"
    sub.mkdir()
    config = yaml.safe_load((project / "autohelix.yaml").read_text())
    config["scope"] = scope
    (sub / "autohelix.yaml").write_text(yaml.safe_dump(config))
    (sub / "code.py").write_text("baseline sub")
    (project / "sibling.py").write_text("baseline sibling")
    git(project, "add", ".")
    git(project, "commit", "-qm", "subdirectory")
    harness = Harness(sub, land_mode="manual")

    def agent(cwd, *_):
        (cwd / "code.py").write_text("candidate sub")
        (cwd.parent / "sibling.py").write_text("protected sibling")
        git(cwd, "add", "../sibling.py")
        return AgentRunResult(success=True, usage={"cost_usd": 0.2})

    monkeypatch.setattr(harness, "run_agent", agent)
    try:
        result = harness.run_iteration(1)
        assert result.accepted
        assert git(project, "show", f"{harness.paths.ref}:sibling.py").stdout == "baseline sibling"
        assert git(project, "show", f"{harness.paths.ref}:sub/code.py").stdout == "candidate sub"
    finally:
        harness.close()


def add_submodule(project, tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    git(child, "init", "-q")
    git(child, "config", "user.email", "test@example.com")
    git(child, "config", "user.name", "Test")
    (child / "code.py").write_text("child baseline")
    git(child, "add", ".")
    git(child, "commit", "-qm", "child")
    git(project, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(child), "sub")
    git(project, "commit", "-qam", "submodule")


def test_submodule_guard_uses_child_pin_under_parent_environment(project, tmp_path, monkeypatch):
    add_submodule(project, tmp_path)
    for key, value in PinnedGit.discover(project).env().items():
        monkeypatch.setenv(key, value)
    with pytest.raises(CandidateChanged):
        with ContentGuard(project):
            (project / "sub" / "code.py").write_text("dirty child")
            (project / "sub" / ".git").unlink()


def test_initializing_empty_submodule_invalidates_guard(project, tmp_path):
    add_submodule(project, tmp_path)
    git(project, "submodule", "deinit", "-f", "sub")
    with pytest.raises(CandidateChanged):
        with ContentGuard(project):
            git(project, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "sub")
            (project / "sub" / "code.py").write_text("judge changed initialized child")


def test_initialized_submodule_late_clean_commit_cannot_publish_unvalidated_code(project, tmp_path):
    add_submodule(project, tmp_path)
    git(project, "submodule", "deinit", "-f", "sub")
    checkpoint = ContentCheckpoint(project)
    git(project, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "sub")
    validated = checkpoint.capture()
    child = project / "sub"
    git(child, "config", "user.email", "test@example.com")
    git(child, "config", "user.name", "Test")
    (child / "code.py").write_text("late unvalidated child commit")
    git(child, "commit", "-qam", "late")
    with pytest.raises(RuntimeError, match="changed after validation"):
        checkpoint.commit(Scope(project.resolve(), project.resolve()), "parent", validated=validated)


@pytest.mark.parametrize("state", ["dirty", "advanced-dirty", "clean-advance", "staged", "new"])
def test_submodule_publication_requires_committed_candidate(project, tmp_path, state):
    add_submodule(project, tmp_path)
    child = project / "sub"
    git(child, "config", "user.email", "test@example.com")
    git(child, "config", "user.name", "Test")
    git(project, "config", "diff.ignoreSubmodules", "all")
    checkpoint = ContentCheckpoint(project)
    (child / "code.py").write_text("child accepted change")
    if state in ("advanced-dirty", "clean-advance"):
        git(child, "add", ".")
        git(child, "commit", "-qm", "child accepted")
        if state == "advanced-dirty":
            (child / "code.py").write_text("uncommitted extra")
    elif state == "staged":
        git(child, "add", ".")
    elif state == "new":
        git(child, "checkout", "--", "code.py")
        (child / "new.py").write_text("new candidate")
    scope = Scope(project.resolve(), project.resolve())
    if state == "clean-advance":
        commit = checkpoint.commit(scope, "parent", validated=checkpoint.capture())
        assert commit
        assert git(project, "ls-tree", commit, "sub").stdout.split()[2] == git(child, "rev-parse", "HEAD").stdout.strip()
    else:
        with pytest.raises(RuntimeError, match="dirty submodule"):
            checkpoint.commit(scope, "parent", validated=checkpoint.capture())


def test_publication_rejects_editable_drift_after_validation(project, monkeypatch):
    harness = Harness(project, land_mode="manual")
    build = harness._build_commit_message

    def message(*args, **kwargs):
        (kwargs["worktree_path"] / "code.py").write_text("unvalidated drift")
        return build(*args, **kwargs)

    monkeypatch.setattr(harness, "_build_commit_message", message)
    try:
        with pytest.raises(AutoHelixRunError, match="changed after validation"):
            harness.run_iteration(1)
        assert not harness.history.load()[-1].accepted
        assert git(project, "rev-parse", harness.paths.ref).stdout.strip() == harness.run_info["base_commit"]
    finally:
        harness.close()
