"""Git-backed regressions for local runtime exclusions at run (`setup` touches no git state)."""

import subprocess

import pytest
from click.testing import CliRunner

from autohelix.cli import main
from autohelix.harness import AutoHelixRunError, Harness
from autohelix.history import History


def git(project, *args):
    return subprocess.run(
        [
            "git", "-c", "user.name=Test", "-c", "user.email=test@test.com",
            *args,
        ],
        cwd=project, capture_output=True, text=True, check=True,
    ).stdout


@pytest.fixture(params=["fresh-clone", "existing-ignore", "subdirectory", "worker"])
def project_layout(tmp_path, request):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@test.com")
    project = source
    if request.param == "subdirectory":
        project = source / "apps" / "demo"
        project.mkdir(parents=True)
    elif request.param == "existing-ignore":
        (source / ".gitignore").write_text(".autohelix/\n")

    (project / "main.py").write_text('print("hello")\n')
    (project / "autohelix.yaml").write_text(
        "goal: Test local exclusions\n"
        "agent:\n"
        "  backend: mock\n"
        "  settings:\n"
        "    change_file: main.py\n"
        "    change_content: '# Modified in iteration {iteration}'\n"
        "gates: ['true']\n"
        "report: false\n"
    )
    git(source, "add", "-A")
    git(source, "commit", "-m", "Initial commit")

    if request.param == "fresh-clone":
        project = tmp_path / "clone"
        git(tmp_path, "clone", str(source), str(project))
        git(project, "config", "user.name", "Test")
        git(project, "config", "user.email", "test@test.com")
        common_repo = project
    elif request.param == "worker":
        project = tmp_path / "worker"
        git(source, "worktree", "add", "-b", "worker-1", str(project))
        assert (project / ".git").is_file()
        common_repo = source
    else:
        common_repo = source
    return project, common_repo


def test_exclusions_keep_runtime_local_in_each_layout(project_layout):
    project, common_repo = project_layout
    head_before = git(project, "rev-parse", "HEAD").strip()
    ignore = project / ".gitignore"
    ignore_before = ignore.read_bytes() if ignore.exists() else None

    result = CliRunner().invoke(
        main, ["run", "-p", str(project), "-n", "1", "--no-report"],
    )
    assert History(project).load()[-1].accepted
    # Only the accepted iteration adds a commit, never ignore setup.
    assert git(project, "rev-list", "--count", f"{head_before}..HEAD").strip() == "1"
    repo_prefix = git(project, "rev-parse", "--show-prefix").strip()
    assert git(project, "show", "--format=", "--name-only", "HEAD").strip() == repo_prefix + "main.py"

    assert result.exit_code == 0, result.output
    assert git(project, "status", "--porcelain") == ""
    assert (ignore.read_bytes() if ignore.exists() else None) == ignore_before
    runtime_file = project / ".autohelix" / "ignored.txt"
    runtime_file.write_text("runtime state\n")
    assert git(project, "check-ignore", ".autohelix/ignored.txt").strip() == ".autohelix/ignored.txt"
    exclude = common_repo / ".git" / "info" / "exclude"
    assert ".autohelix/" in exclude.read_text().splitlines()
    assert "__pycache__/" in exclude.read_text().splitlines()

    # A worker's shared exclusion also applies to the original checkout.
    (common_repo / ".autohelix").mkdir(exist_ok=True)
    (common_repo / ".autohelix" / "ignored.txt").write_text("runtime state\n")
    assert git(common_repo, "check-ignore", ".autohelix/ignored.txt").strip() == ".autohelix/ignored.txt"


def test_staged_user_changes_are_preserved(project_layout):
    project, _ = project_layout
    ignore = project / ".gitignore"
    # Include a user-authored ignore edit: startup must neither rewrite it nor
    # commit it together with the already-staged code edit.
    ignore.write_text("user-output/\n")
    git(project, "add", ".gitignore")
    (project / "main.py").write_text('print("staged user work")\n')
    git(project, "add", "main.py")
    (project / "main.py").write_text('print("unstaged user work")\n')
    head_before = git(project, "rev-parse", "HEAD")
    index_before = git(project, "write-tree")
    staged_before = git(project, "diff", "--cached", "--binary")
    unstaged_before = git(project, "diff", "--binary")

    result = CliRunner().invoke(
        main, ["run", "-p", str(project), "-n", "1", "--no-report"],
    )
    assert "Uncommitted changes in tracked files" in result.output
    assert History(project).load() == []

    # Refusing a dirty first baseline is a command error.
    assert result.exit_code == 1, result.output
    assert git(project, "rev-parse", "HEAD") == head_before
    assert git(project, "write-tree") == index_before
    assert git(project, "diff", "--cached", "--binary") == staged_before
    assert git(project, "diff", "--binary") == unstaged_before
    assert ignore.read_text() == "user-output/\n"


@pytest.mark.parametrize("negation", ["!.*", "!*"])
@pytest.mark.parametrize("existing_runtime", [False, True])
def test_conflicting_ignore_refuses_and_preserves_user_work(
    project_layout, existing_runtime, negation, monkeypatch,
):
    project, common_repo = project_layout
    ignore = project / ".gitignore"
    ignore.write_text(negation + "\n")
    git(project, "add", ".gitignore")
    git(project, "commit", "-m", "Ignore rules")
    # Tracked changes are refused first (before any write); untracked work must survive.
    (project / "scratch.py").write_text('print("untracked user work")\n')
    ignore_before = ignore.read_bytes()
    head_before = git(project, "rev-parse", "HEAD")
    index_before = git(project, "write-tree")
    staged_before = git(project, "diff", "--cached", "--binary")
    unstaged_before = git(project, "diff", "--binary")
    worktrees_before = git(project, "worktree", "list", "--porcelain")
    config_before = (project / "autohelix.yaml").read_bytes()
    runtime = project / ".autohelix"
    if existing_runtime:
        runtime.mkdir()

    def unexpected_start(*args, **kwargs):
        pytest.fail("Ignore failure must refuse before creating an agent or worktree")

    monkeypatch.setattr("autohelix.harness.create_agent", unexpected_start)
    monkeypatch.setattr("autohelix.workspace.Copy.create", unexpected_start)

    for _ in range(2):
        result = CliRunner().invoke(main, ["run", "-p", str(project), "-n", "1", "--no-report"])
        assert result.exit_code == 1, result.output
        assert "not effectively ignored" in result.output
        assert f".gitignore:1:{negation}" in result.output
        assert "Add .autohelix/ after" in result.output
        assert "Traceback" not in result.output

    assert not (runtime / "history.jsonl").exists()
    assert not (runtime / "worktrees").exists()
    assert not (runtime / "prompt.md").exists()
    if existing_runtime:
        assert list(runtime.iterdir()) == []
    else:
        assert not runtime.exists()
    assert git(project, "worktree", "list", "--porcelain") == worktrees_before
    assert git(project, "rev-parse", "HEAD") == head_before
    assert git(project, "write-tree") == index_before
    assert git(project, "diff", "--cached", "--binary") == staged_before
    assert git(project, "diff", "--binary") == unstaged_before
    assert ignore.read_bytes() == ignore_before
    assert (project / "scratch.py").read_text() == 'print("untracked user work")\n'
    assert (project / "autohelix.yaml").read_bytes() == config_before
    exclude = common_repo / ".git" / "info" / "exclude"
    assert exclude.read_text().splitlines().count(".autohelix/") == 1


@pytest.mark.parametrize("project_layout", ["fresh-clone"], indirect=True)
@pytest.mark.parametrize("negation", ["!.*", "!*"])
def test_later_runtime_ignore_overrides_negation(project_layout, negation):
    project, _ = project_layout
    ignore = project / ".gitignore"
    rules = negation + "\n.autohelix/\n"
    ignore.write_text(rules)
    git(project, "add", ".gitignore")
    git(project, "commit", "-m", "Ignore rules")

    result = CliRunner().invoke(
        main, ["run", "-p", str(project), "-n", "1", "--no-report"],
    )
    assert History(project).load()[-1].accepted

    assert result.exit_code == 0, result.output
    assert ignore.read_text() == rules
    assert git(project, "status", "--porcelain") == ""


@pytest.mark.parametrize("project_layout", ["fresh-clone"], indirect=True)
@pytest.mark.parametrize("negation", ["!.*", "!*"])
def test_programmatic_run_rechecks_changed_ignore_rules(
    project_layout, negation, monkeypatch,
):
    project, _ = project_layout
    harness = Harness(project)  # creates .autohelix/ and the local excludes
    (project / ".gitignore").write_text(negation + "\n")

    def unexpected_iteration(*args, **kwargs):
        pytest.fail("Ignore failure must refuse before any iteration starts")

    monkeypatch.setattr(harness, "run_iteration", unexpected_iteration)
    with pytest.raises(AutoHelixRunError, match="not effectively ignored"):
        harness.run(max_iterations=1, report=False)

    assert not (project / ".autohelix" / "history.jsonl").exists()
    assert not (project / ".autohelix" / "worktrees").exists()
