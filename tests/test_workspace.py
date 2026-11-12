"""Candidate copies, snapshots and fingerprints (`autohelix.workspace`)."""

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from autohelix import workspace
from autohelix.runs import git
from autohelix.workspace import CandidateRejected, Copy, Fingerprint, Repo, Scope


@pytest.fixture
def repo_dir(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "local@example.com")
    git(root, "config", "user.name", "Local")
    (root / "code.py").write_text("print('baseline')\n")
    (root / "test_code.py").write_text("assert True\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")
    git(root, "config", "--add", "--local", "core.excludesFile", "/dev/null")
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "exclude").write_text(".autohelix/\n")
    return root


@pytest.fixture
def copies(tmp_path):
    root = tmp_path / "copies" / "run"  # outside the repository, as in a run
    root.mkdir(parents=True)
    return root.resolve()


def commit_all(root, message="change"):
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD").stdout.strip()


def tree_of(root, rev):
    return git(root, "rev-parse", f"{rev}^{{tree}}").stdout.strip()


def blob(root, tree, name):
    return subprocess.run(["git", "cat-file", "blob", f"{tree}:{name}"], cwd=root,
                          capture_output=True, check=True).stdout


def names(root, tree):
    return set(git(root, "ls-tree", "-r", "--name-only", tree).stdout.splitlines())


def agent_copy(repo_dir, copies, name="iter-1-agent"):
    repo = Repo(repo_dir)
    head = git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    return repo, head, Copy.create(repo, copies / name, files=head, head=head)


def test_untouched_copy_snapshots_to_its_tree_and_leaves_user_state(repo_dir, copies):
    before = git(repo_dir, "status", "--porcelain").stdout
    repo, head, copy = agent_copy(repo_dir, copies)
    assert (copy.path / "code.py").read_text() == "print('baseline')\n"
    assert subprocess.run(["git", "status", "--porcelain"], cwd=copy.path, capture_output=True,
                          text=True, env={**os.environ, **copy.env}).stdout == ""
    tree, discarded = copy.snapshot(Scope())
    assert tree == tree_of(repo_dir, head) and discarded == []
    assert copy.changed() == []
    assert git(repo_dir, "status", "--porcelain").stdout == before
    copy.remove()
    assert not copy.path.exists() and not copy.index.exists()


def test_snapshot_keeps_in_scope_and_resets_out_of_scope_in_index_only(repo_dir, copies):
    (repo_dir / "tool.sh").write_text("echo\n")
    (repo_dir / "old.txt").write_text("old\n")
    head = commit_all(repo_dir)
    repo = Repo(repo_dir)
    copy = Copy.create(repo, copies / "c", files=head, head=head)
    (copy.path / "code.py").write_text("print('fast')\n")
    (copy.path / "new.py").write_text("new\n")
    (copy.path / "tool.sh").chmod(0o755)
    (copy.path / "old.txt").unlink()
    (copy.path / "test_code.py").write_text("assert False is False\n")
    (copy.path / "stray.txt").write_text("stray\n")
    scope = Scope(editable=("code.py", "new.py", "tool.sh", "old.txt"))
    tree, discarded = copy.snapshot(scope)
    assert discarded == ["stray.txt", "test_code.py"]
    assert blob(repo_dir, tree, "code.py") == b"print('fast')\n"
    assert blob(repo_dir, tree, "test_code.py") == b"assert True\n"
    assert "new.py" in names(repo_dir, tree) and "old.txt" not in names(repo_dir, tree)
    assert "stray.txt" not in names(repo_dir, tree)
    assert "100755 blob" in git(repo_dir, "ls-tree", tree, "tool.sh").stdout
    # Nothing is written into the copy.
    assert (copy.path / "test_code.py").read_text() == "assert False is False\n"
    assert (copy.path / "stray.txt").exists()


def test_frozen_directory_replaced_by_symlink_is_restored(repo_dir, copies, tmp_path):
    (repo_dir / "tests").mkdir()
    (repo_dir / "tests" / "test_a.py").write_text("frozen\n")
    head = commit_all(repo_dir)
    repo = Repo(repo_dir)
    copy = Copy.create(repo, copies / "c", files=head, head=head)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "test_a.py").write_text("weakened\n")
    shutil.rmtree(copy.path / "tests")
    (copy.path / "tests").symlink_to(elsewhere)
    (copy.path / "code.py").write_text("changed\n")
    # The symlink is in scope, but can't coexist with the restored frozen file.
    tree, discarded = copy.snapshot(Scope(frozen=("tests/test_a.py",)))
    assert "tests" in discarded and "tests/test_a.py" in discarded
    assert blob(repo_dir, tree, "tests/test_a.py") == b"frozen\n"
    assert blob(repo_dir, tree, "code.py") == b"changed\n"
    assert (elsewhere / "test_a.py").read_text() == "weakened\n"


def test_crlf_autocrlf_and_working_tree_encoding_round_trip(repo_dir, copies):
    (repo_dir / ".gitattributes").write_text(
        "*.txt eol=crlf\nutf16.dat working-tree-encoding=UTF-16LE-BOM text\n")
    (repo_dir / "a.txt").write_text("one\ntwo\n")
    (repo_dir / "auto.md").write_bytes(b"x\r\ny\r\n")
    (repo_dir / "utf16.dat").write_bytes("héllo\n".encode("utf-16"))
    git(repo_dir, "config", "core.autocrlf", "true")
    head = commit_all(repo_dir)
    repo = Repo(repo_dir)
    copy = Copy.create(repo, copies / "agent", files=head, head=head)
    assert (copy.path / "a.txt").read_bytes() == b"one\r\ntwo\r\n"
    assert (copy.path / "auto.md").read_bytes() == b"x\r\ny\r\n"
    assert b"\xff\xfe" == (copy.path / "utf16.dat").read_bytes()[:2]
    tree, discarded = copy.snapshot(Scope())
    assert tree == tree_of(repo_dir, head) and discarded == []
    (copy.path / "a.txt").write_bytes(b"one\r\nthree\r\n")
    tree, _ = copy.snapshot(Scope())
    assert blob(repo_dir, tree, "a.txt") == b"one\nthree\n"
    checks = Copy.create(repo, copies / "checks", files=tree, head=head)
    assert (checks.path / "a.txt").read_bytes() == b"one\r\nthree\r\n"
    assert checks.changed() == []
    # The agent's own git sees CRLF files as clean too.
    status = subprocess.run(["git", "status", "--porcelain"], cwd=checks.path, text=True,
                            capture_output=True, env={**os.environ, **checks.env}).stdout
    assert status.splitlines() == [" M a.txt"]


def test_clean_filter_cannot_hide_a_write(repo_dir, copies):
    (repo_dir / ".gitattributes").write_text("value.txt filter=launder\n")
    git(repo_dir, "config", "filter.launder.clean", "sed s/EVIL/GOOD/g")
    git(repo_dir, "config", "filter.launder.smudge", "cat")
    (repo_dir / "value.txt").write_text("GOOD\n")
    head = commit_all(repo_dir)
    repo = Repo(repo_dir)
    copy = Copy.create(repo, copies / "review", files=head, head=head)
    (copy.path / "value.txt").write_text("EVIL\n")
    assert copy.changed() == ["value.txt"]
    # The filter is copied, so git inside the copy sees no change at all.
    status = subprocess.run(["git", "status", "--porcelain"], cwd=copy.path, text=True,
                            capture_output=True, env={**os.environ, **copy.env}).stdout
    assert status == ""


def test_touch_identical_rewrite_and_new_files_are_not_changes(repo_dir, copies):
    _, _, copy = agent_copy(repo_dir, copies)
    os.utime(copy.path / "code.py", (time.time() + 5, time.time() + 5))
    (copy.path / "test_code.py").write_text("assert True\n")
    (copy.path / ".coverage").write_text("scratch")
    (copy.path / "build").mkdir()
    assert copy.changed() == []
    (copy.path / "code.py").chmod(0o755)
    (copy.path / "test_code.py").unlink()
    assert copy.changed() == ["code.py", "test_code.py"]


def test_racily_clean_same_size_rewrite_is_detected(tmp_path):
    (tmp_path / "f").write_text("aaaa")
    fingerprint = Fingerprint.take(tmp_path, ["f"])
    stamp = (tmp_path / "f").stat()
    (tmp_path / "f").write_text("bbbb")
    os.utime(tmp_path / "f", ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert fingerprint.changed(tmp_path) == ["f"]


def test_symlinked_parent_directory_is_a_change(repo_dir, copies, tmp_path):
    (repo_dir / "pkg").mkdir()
    (repo_dir / "pkg" / "mod.py").write_text("x = 1\n")
    head = commit_all(repo_dir)
    copy = Copy.create(Repo(repo_dir), copies / "c", files=head, head=head)
    clone = tmp_path / "clone"
    shutil.copytree(copy.path / "pkg", clone)
    shutil.rmtree(copy.path / "pkg")
    (copy.path / "pkg").symlink_to(clone)
    assert copy.changed() == ["pkg"]


def test_three_thousand_files_are_fast(repo_dir, copies):
    for directory in range(30):
        folder = repo_dir / f"d{directory}"
        folder.mkdir()
        for index in range(100):
            (folder / f"f{index}.py").write_text(f"value = {directory * 100 + index}\n")
    head = commit_all(repo_dir)
    started = time.monotonic()
    repo = Repo(repo_dir)
    copy = Copy.create(repo, copies / "agent", files=head, head=head)
    (copy.path / "d3" / "f7.py").write_text("value = -1\n")
    tree, _ = copy.snapshot(Scope(editable=("d3",)))
    checks = Copy.create(repo, copies / "checks", files=tree, head=head)
    for _ in range(3):
        assert checks.changed() == []
    copy.remove()
    checks.remove()
    assert time.monotonic() - started < 30


def test_filenames_with_newlines(repo_dir, copies):
    (repo_dir / "odd\nname.py").write_text("x\n")
    (repo_dir / "frozen\nname.py").write_text("y\n")
    head = commit_all(repo_dir)
    copy = Copy.create(Repo(repo_dir), copies / "c", files=head, head=head)
    (copy.path / "odd\nname.py").write_text("changed\n")
    (copy.path / "frozen\nname.py").write_text("changed\n")
    tree, discarded = copy.snapshot(Scope(frozen=("frozen*",)))
    assert discarded == ["frozen\nname.py"]
    assert blob(repo_dir, tree, "odd\nname.py") == b"changed\n"
    assert blob(repo_dir, tree, "frozen\nname.py") == b"y\n"
    assert copy.changed() == ["frozen\nname.py", "odd\nname.py"]


def test_agent_git_activity_stays_in_the_copy(repo_dir, copies):
    refs = git(repo_dir, "for-each-ref").stdout
    config = (repo_dir / ".git" / "config").read_text()
    _, head, copy = agent_copy(repo_dir, copies)
    env = {**os.environ, **copy.env}

    def run(*args):
        return subprocess.run(["git", *args], cwd=copy.path, env=env, capture_output=True, text=True)

    (copy.path / "code.py").write_text("print('committed')\n")
    assert run("commit", "-qam", "agent commit").returncode == 0
    assert run("branch", "-D", "main").returncode == 0
    run("branch", "evil")
    run("config", "core.evil", "true")
    (copy.path / ".git" / "hooks").mkdir(exist_ok=True)
    (copy.path / ".git" / "hooks" / "post-commit").write_text("#!/bin/sh\ntouch /tmp/x\n")
    assert git(repo_dir, "for-each-ref").stdout == refs
    assert (repo_dir / ".git" / "config").read_text() == config
    assert not (repo_dir / ".git" / "hooks" / "post-commit").exists()
    tree, _ = copy.snapshot(Scope())
    assert blob(repo_dir, tree, "code.py") == b"print('committed')\n"


def test_copies_list_only_the_refs_they_are_given(repo_dir, copies):
    head = git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    for run in ("own", "member", "stranger"):
        git(repo_dir, "update-ref", f"refs/autohelix/{run}", head)
    git(repo_dir, "stash", "list")
    repo = Repo(repo_dir)
    copy = Copy.create(repo, copies / "iter-1-agent", files=head, head=head,
                       refs=("refs/autohelix/own", "refs/autohelix/member"))

    def run(*args):
        return subprocess.run(["git", *args], cwd=copy.path, capture_output=True, text=True,
                              env={**os.environ, **copy.env})

    assert run("show", "autohelix/member:code.py").stdout == "print('baseline')\n"
    listed = run("for-each-ref", "--format=%(refname)").stdout.split()
    assert "refs/autohelix/own" in listed and "refs/autohelix/member" in listed and "refs/heads/main" in listed
    assert "refs/autohelix/stranger" not in listed
    assert run("rev-parse", "--verify", "autohelix/stranger").returncode != 0
    assert not (copy.path / ".git" / "refs" / "stash").exists()


def test_deleted_or_replaced_copy_git_cannot_reach_user_repo(repo_dir, copies):
    head = git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    _, _, copy = agent_copy(repo_dir, copies)
    shutil.rmtree(copy.path / ".git")
    env = {**os.environ, **copy.env}
    status = subprocess.run(["git", "status"], cwd=copy.path, env=env, capture_output=True, text=True)
    assert status.returncode != 0 and "not a git repository" in status.stderr.lower()
    reset = subprocess.run(["git", "reset", "--hard", "HEAD~0"], cwd=copy.path / ".", env=env,
                           capture_output=True)
    assert reset.returncode != 0
    (copy.path / ".git").write_text(f"gitdir: {repo_dir / '.git'}\n")
    (copy.path / "code.py").write_text("print('edited')\n")
    tree, _ = copy.snapshot(Scope())
    assert blob(repo_dir, tree, "code.py") == b"print('edited')\n"
    assert ".git" not in names(repo_dir, tree)
    assert git(repo_dir, "rev-parse", "HEAD").stdout.strip() == head
    assert git(repo_dir, "status", "--porcelain").stdout == ""


def test_repo_local_and_include_if_identity_reach_agent_commits(repo_dir, copies, tmp_path, monkeypatch):
    git(repo_dir, "config", "--unset", "user.email")
    git(repo_dir, "config", "--unset", "user.name")
    identity = tmp_path / "identity.cfg"
    identity.write_text("[user]\n\tname = Included\n\temail = included@example.com\n")
    global_config = tmp_path / "global.cfg"
    global_config.write_text(f'[includeIf "gitdir:{repo_dir.resolve()}/.git"]\n\tpath = {identity}\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    _, _, copy = agent_copy(repo_dir, copies)
    env = {**os.environ, **copy.env}
    (copy.path / "code.py").write_text("x\n")
    subprocess.run(["git", "commit", "-qam", "agent"], cwd=copy.path, env=env, check=True)
    author = subprocess.run(["git", "log", "-1", "--format=%ae"], cwd=copy.path, env=env,
                            capture_output=True, text=True).stdout.strip()
    assert author == "included@example.com"
    monkeypatch.delenv("GIT_CONFIG_GLOBAL")
    git(repo_dir, "config", "user.email", "repo-local@example.com")
    git(repo_dir, "config", "user.name", "Local")
    _, _, second = agent_copy(repo_dir, copies, "second")
    (second.path / "code.py").write_text("y\n")
    subprocess.run(["git", "commit", "-qam", "agent"], cwd=second.path, check=True,
                   env={**os.environ, **second.env})
    assert subprocess.run(["git", "log", "-1", "--format=%ae"], cwd=second.path, capture_output=True,
                          text=True, env={**os.environ, **second.env}).stdout.strip() == "repo-local@example.com"


@pytest.mark.parametrize("ignored", [True, False])
def test_subdirectory_project(repo_dir, copies, ignored):
    if not ignored:
        (repo_dir / ".git" / "info" / "exclude").write_text("")
    (repo_dir / "proj").mkdir()
    (repo_dir / "proj" / "code.py").write_text("sub\n")
    head = commit_all(repo_dir)
    project = repo_dir / "proj"
    (project / ".autohelix").mkdir()
    repo = Repo(project)
    assert repo.prefix == "proj/" and repo.root == repo_dir.resolve()
    copy = Copy.create(repo, copies / "c", files=head, head=head)
    assert copy.working_dir == copy.path / "proj"
    (copy.working_dir / "code.py").write_text("edited\n")
    (copy.path / "code.py").write_text("outside the project\n")
    for folder in (".autohelix", ".autohelix-iteration"):
        (copy.working_dir / folder / "notes").mkdir(parents=True)
        (copy.working_dir / folder / "notes" / "n.md").write_text(f"notes in {folder}")
    tree, discarded = copy.snapshot(Scope(prefix=repo.prefix, editable=("code.py",)))
    assert discarded == ["code.py"]  # the state folders never reach the index at all
    assert blob(repo_dir, tree, "proj/code.py") == b"edited\n"
    assert not any(".autohelix" in name for name in names(repo_dir, tree))
    assert (copy.path / ".git" / "info" / "exclude").read_text().endswith(
        "\n/proj/.autohelix/\n/proj/.autohelix-iteration/\n")


def test_the_freeze_never_hashes_harness_material(repo_dir, copies):
    """Seeded material and an inner run's state are excluded by pathspec, not reset
    afterwards: their contents never enter the user's object store."""
    repo, head, copy = agent_copy(repo_dir, copies)
    secret = {folder: f"unique {folder} {os.urandom(8).hex()}\n" for folder in (".autohelix", ".autohelix-iteration")}
    for folder, text in secret.items():
        (copy.path / folder / "logs").mkdir(parents=True)
        (copy.path / folder / "logs" / "seeded.txt").write_text(text)
    (copy.path / "code.py").write_text("print('edited')\n")
    tree, discarded = copy.snapshot(Scope())
    assert discarded == [] and names(repo_dir, tree) == {"code.py", "test_code.py"}
    for text in secret.values():
        oid = subprocess.run(["git", "hash-object", "--stdin"], cwd=repo_dir, input=text.encode(),
                             capture_output=True, check=True).stdout.decode().strip()
        assert subprocess.run(["git", "cat-file", "-e", oid], cwd=repo_dir).returncode != 0
    assert subprocess.run(["git", "cat-file", "-e", f"{tree}:code.py"], cwd=repo_dir).returncode == 0


def test_linked_worktree_project(repo_dir, tmp_path):
    linked = tmp_path / "linked"
    git(repo_dir, "worktree", "add", "-q", "-b", "side", str(linked))
    repo = Repo(linked)
    assert repo.objects == (repo_dir / ".git" / "objects").resolve()
    copies = tmp_path.resolve() / "copies" / "run"
    copies.mkdir(parents=True)
    head = git(linked, "rev-parse", "HEAD").stdout.strip()
    copy = Copy.create(repo, copies / "c", files=head, head=head)
    (copy.path / "code.py").write_text("linked\n")
    tree, _ = copy.snapshot(Scope())
    assert blob(repo_dir, tree, "code.py") == b"linked\n"
    log = subprocess.run(["git", "log", "--format=%s"], cwd=copy.path, capture_output=True, text=True,
                         env={**os.environ, **copy.env})
    assert log.stdout.strip() == "baseline"


@pytest.fixture
def with_submodule(repo_dir, tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    git(library, "init", "-q")
    (library / "lib.py").write_text("lib\n")
    git(library, "add", ".")
    git(library, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "lib")
    git(repo_dir, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(library), "lib")
    return commit_all(repo_dir, "submodule")


def test_writes_inside_a_submodule_are_rejected(repo_dir, copies, with_submodule):
    copy = Copy.create(Repo(repo_dir), copies / "c", files=with_submodule, head=with_submodule)
    assert (copy.path / "lib").is_dir() and not any((copy.path / "lib").iterdir())
    tree, _ = copy.snapshot(Scope())
    assert tree == tree_of(repo_dir, with_submodule)
    (copy.path / "lib" / "lib.py").write_text("vendored edit\n")
    with pytest.raises(CandidateRejected, match="submodule changes are not supported"):
        copy.snapshot(Scope())


def test_changed_or_new_gitlink_is_rejected(repo_dir, copies, with_submodule):
    copy = Copy.create(Repo(repo_dir), copies / "c", files=with_submodule, head=with_submodule)
    nested = copy.path / "vendor"
    nested.mkdir()
    git(nested, "init", "-q")
    (nested / "x").write_text("x")
    git(nested, "add", ".")
    git(nested, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "x")
    with pytest.raises(CandidateRejected, match="vendor"):
        copy.snapshot(Scope())


def test_swapped_copies_root_is_refused_without_touching_outside(repo_dir, copies, tmp_path):
    _, _, copy = agent_copy(repo_dir, copies)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / copy.path.name).mkdir()
    (outside / copy.path.name / "user.txt").write_text("user work")
    moved = tmp_path / "moved"
    copies.rename(moved)
    copies.symlink_to(outside)
    for operation in (lambda: copy.snapshot(Scope()), copy.changed, copy.remove):
        with pytest.raises(RuntimeError, match="moved or replaced"):
            operation()
    assert (outside / copy.path.name / "user.txt").read_text() == "user work"


def test_swapped_copy_is_refused_and_removal_only_unlinks(repo_dir, copies, tmp_path):
    _, _, copy = agent_copy(repo_dir, copies)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "user.txt").write_text("user work")
    shutil.rmtree(copy.path)
    copy.path.symlink_to(outside)
    with pytest.raises(RuntimeError, match="removed or replaced"):
        copy.snapshot(Scope())
    copy.remove()
    assert not copy.path.is_symlink() and (outside / "user.txt").read_text() == "user work"


def test_removal_handles_read_only_folders(repo_dir, copies):
    _, _, copy = agent_copy(repo_dir, copies)
    cache = copy.path / "cache" / "pkg"
    cache.mkdir(parents=True)
    (cache / "mod.go").write_text("x")
    (cache / "mod.go").chmod(0o444)
    cache.chmod(0o555)
    copy.remove()
    assert not copy.path.exists()


def test_attr_source_keeps_original_attributes(repo_dir, copies):
    (repo_dir / ".gitattributes").write_text("*.txt text eol=lf\n")
    (repo_dir / "data.txt").write_text("a\nb\n")
    head = commit_all(repo_dir)
    repo = Repo(repo_dir)
    if not repo.attr_source:
        pytest.skip("git lacks --attr-source")
    copy = Copy.create(repo, copies / "c", files=head, head=head)
    (copy.path / ".gitattributes").write_text("*.txt -text\n")
    (copy.path / "data.txt").write_bytes(b"a\r\nc\r\n")
    tree, discarded = copy.snapshot(Scope(editable=("data.txt",)))
    assert discarded == [".gitattributes"]
    assert blob(repo_dir, tree, "data.txt") == b"a\nc\n"


def test_missing_attr_source_warns_and_checks_see_published_tree(repo_dir, copies, caplog, monkeypatch):
    (repo_dir / ".gitattributes").write_text("*.txt text eol=lf\n")
    (repo_dir / "data.txt").write_text("a\nb\n")
    head = commit_all(repo_dir)
    repo = Repo(repo_dir)
    repo.attr_source = False
    copy = Copy.create(repo, copies / "c", files=head, head=head)
    (copy.path / ".gitattributes").write_text("*.txt -text\n")
    (copy.path / "data.txt").write_bytes(b"a\r\nc\r\n")
    tree, _ = copy.snapshot(Scope(editable=("data.txt",)))
    checks = Copy.create(repo, copies / "checks", files=tree, head=head)
    published = git(repo_dir, "commit-tree", tree, "-p", head, "-m", "p").stdout.strip()
    fresh = Copy.create(repo, copies / "fresh", files=published, head=published)
    assert (checks.path / "data.txt").read_bytes() == (fresh.path / "data.txt").read_bytes()


def test_clean_smudge_round_trip_stores_clean_and_checks_see_smudged(repo_dir, copies):
    git(repo_dir, "config", "filter.trim.clean", "sed s/DIRTY/CLEAN/g")
    git(repo_dir, "config", "filter.trim.smudge", "sed s/CLEAN/DIRTY/g")
    (repo_dir / ".gitattributes").write_text("code.py filter=trim\n")
    (repo_dir / "code.py").write_text("DIRTY baseline")
    head = commit_all(repo_dir)
    repo = Repo(repo_dir)
    copy = Copy.create(repo, copies / "agent", files=head, head=head)
    assert (copy.path / "code.py").read_text() == "DIRTY baseline"
    (copy.path / "code.py").write_text("DIRTY improvement")
    tree, _ = copy.snapshot(Scope())
    assert blob(repo_dir, tree, "code.py") == b"CLEAN improvement"
    checks = Copy.create(repo, copies / "checks", files=tree, head=head)
    assert (checks.path / "code.py").read_text() == "DIRTY improvement"
    assert checks.changed() == []


def test_scope_patterns_are_relative_to_the_project():
    scope = Scope(prefix="proj/", editable=("src/", "*.md"), frozen=("src/vendor", "src/*_test.py"))
    assert scope.allows("proj/src/a.py") and scope.allows("proj/README.md")
    assert not scope.allows("proj/src/vendor/lib.py") and not scope.allows("proj/src/a_test.py")
    assert not scope.allows("proj/srcx/a.py") and not scope.allows("src/a.py")
    assert Scope().allows("anything") and not Scope(frozen=(".",)).allows("x")



@pytest.mark.parametrize("mode", [0o000, 0o300])
def test_removal_handles_unreadable_folders(repo_dir, copies, mode):
    _, _, copy = agent_copy(repo_dir, copies)
    locked = copy.path / "locked" / "inner"
    locked.mkdir(parents=True)
    (locked / "f").write_text("x")
    locked.chmod(mode)
    (copy.path / "locked").chmod(mode)
    copy.remove()
    assert not copy.path.exists()


def test_planted_index_symlink_cannot_reach_user_index(repo_dir, copies):
    user_index = (repo_dir / ".git" / "index").read_bytes()
    _, _, copy = agent_copy(repo_dir, copies)
    assert not copy.index.is_relative_to(copies)
    (copies / f"{copy.path.name}.index").symlink_to(repo_dir / ".git" / "index")
    (copy.path / "test_code.py").unlink()
    copy.snapshot(Scope())
    assert (repo_dir / ".git" / "index").read_bytes() == user_index
    copy.index.unlink()
    copy.index.symlink_to(repo_dir / ".git" / "index")
    with pytest.raises(RuntimeError, match="Harness index was replaced"):
        copy.snapshot(Scope())
    assert (repo_dir / ".git" / "index").read_bytes() == user_index


def test_agent_conditions_reject_instead_of_crashing(repo_dir, copies):
    _, _, copy = agent_copy(repo_dir, copies)
    git(copy.path / "scratch" if (copy.path / "scratch").mkdir() is None else copy.path, "init", "-q")
    with pytest.raises(CandidateRejected, match="could not snapshot"):
        copy.snapshot(Scope())
    shutil.rmtree(copy.path / "scratch")
    (copy.path / ".autohelix").write_text("a file, not the state folder")
    tree, discarded = copy.snapshot(Scope())
    assert ".autohelix" not in names(repo_dir, tree) and ".autohelix" not in discarded  # never staged
    (copy.path / "code.py").chmod(0)
    assert copy.changed() == ["code.py"]
    (copy.path / "code.py").chmod(0o644)
