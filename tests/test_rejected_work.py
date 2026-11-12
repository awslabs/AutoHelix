"""Rejected work: kept trees (refs/autohelix-rejected/<id>/<n>), rejected.patch for the
next agent, and the attempt's own record fields (summary, base, tree, discarded)."""

import subprocess

import pytest
import yaml

from autohelix import runs
from autohelix.harness import AutoHelixRunError, Harness
from autohelix.history import History
from autohelix.workspace import ITERATION_DIR


def _git(cwd, *args, check=True) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check).stdout.strip()


@pytest.fixture
def project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init")
    _git(project, "config", "user.email", "test@test.com")
    _git(project, "config", "user.name", "Test")
    (project / "hello.py").write_text('print("hello")\n')
    (project / ".gitignore").write_text(".autohelix/\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "init")
    return project


def _configure(project, settings=None, **sections) -> None:
    config = {"goal": "Test rejected work",
              "agent": {"backend": "mock", "settings": {"change_file": "hello.py",
                                                        "change_content": "# iteration {iteration}",
                                                        **(settings or {})}},
              **sections}
    (project / "autohelix.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "config")


class Spy:
    """Wraps Harness.run_agent: writes a summary, and records what each agent copy holds."""

    def __init__(self, harness, edit=None):
        self.harness, self.original, self.seen, self.edit = harness, harness.run_agent, {}, edit
        harness.run_agent = self

    def __call__(self, worktree, prompt, iteration):
        state = worktree / ITERATION_DIR
        patch = state / "rejected.patch"
        self.seen[iteration] = {
            "refs": _git(worktree, "for-each-ref", "--format=%(refname)"),
            "patch": patch.read_bytes() if patch.exists() else None,
            "prompt": prompt,
        }
        if patch.exists():  # the patch applies to the copy (the tip) and reproduces the rejected files
            probe = worktree.parent / f"probe-{iteration}"
            subprocess.run(["cp", "-R", str(worktree), str(probe)], check=True)
            _git(probe, "apply", str(probe / ITERATION_DIR / "rejected.patch"))
            self.seen[iteration]["applied"] = (probe / "hello.py").read_text()
            subprocess.run(["rm", "-rf", str(probe)], check=True)
        (state / "commit_summary.txt").write_text(f"attempt {iteration}\nmore detail\n")
        if self.edit:  # a change of the agent's own, before the mock runs (or fails)
            self.edit(worktree)
        return self.original(worktree, prompt, iteration)


def _refs(project, harness) -> list[str]:
    return _git(project, "for-each-ref", "--format=%(refname)", harness.paths.rejected_refs).split()


def test_rejected_trees_are_kept_and_shown_to_the_next_agent(project):
    _configure(project, gates=['[ "$AUTOHELIX_ITERATION" != 1 ]'])
    base = _git(project, "rev-parse", "HEAD")
    harness = Harness(project, verbose=False, land_mode="manual")
    spy = Spy(harness)
    harness.run(max_iterations=3, report=False)
    records = History(project).load()
    assert [(r.iteration, r.accepted) for r in records] == [(0, True), (1, False), (2, True), (3, True)]
    rejected, accepted = records[1], records[2]

    # Record fields on every attempt.
    assert rejected.summary == "attempt 1" and accepted.summary == "attempt 2"
    assert rejected.base == base == accepted.base == records[0].base
    assert rejected.tree and accepted.tree == _git(project, "rev-parse", f"{accepted.commit}^{{tree}}")
    assert rejected.discarded == [] and records[0].tree is None
    assert rejected.rejected_ref == harness.paths.rejected_ref(1)
    assert accepted.rejected_ref is None
    # One kept tree: the rejected attempt's, never an accepted one's.
    assert _refs(project, harness) == [harness.paths.rejected_ref(1)]
    assert _git(project, "rev-parse", harness.paths.rejected_ref(1)) == rejected.tree

    # The next agent gets rejected.patch against its HEAD; it reproduces the rejected files.
    assert spy.seen[1]["patch"] is None
    patch = spy.seen[2]["patch"]
    assert patch and b"+# iteration 1" in patch
    assert spy.seen[2]["applied"].strip() == _git(project, "show", f"{rejected.tree}:hello.py")
    assert spy.seen[3]["patch"] is None  # absent after an accept
    # Copies never list kept trees (only branches, tags and this run's ref).
    assert all("autohelix-rejected" not in seen["refs"] for seen in spy.seen.values())
    assert "refs/autohelix/" in spy.seen[3]["refs"]

    runs.archive(harness.paths)  # archive keeps them ...
    assert _refs(project, harness) == [harness.paths.rejected_ref(1)]
    runs.delete(harness.paths, force=True)  # ... delete removes them
    assert _refs(project, harness) == []


def test_failed_agent_attempt_keeps_its_tree_and_stays_rejected(project):
    _configure(project, settings={"should_fail": True})
    harness = Harness(project, verbose=False, land_mode="manual")
    Spy(harness, edit=lambda worktree: (worktree / "hello.py").write_text("half done\n"))
    harness.run(max_iterations=1, report=False)  # the loop goes on; the budget ends it here
    record = History(project).load()[-1]
    assert record.iteration == 1 and not record.accepted
    assert record.reason == "Iteration 1 agent failed: Mock failure"
    assert record.gates == [] and record.observe == []  # never checked
    assert record.summary == "attempt 1" and record.tree
    assert _refs(project, harness) == [harness.paths.rejected_ref(1)] == [record.rejected_ref]


def test_discarded_paths_are_recorded(project):
    (project / "tests").mkdir()
    (project / "tests" / "test_x.py").write_text("assert True\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "tests")
    _configure(project, settings={"change_file": "tests/test_x.py"}, scope={"editable": ["hello.py"]})
    harness = Harness(project, verbose=False, land_mode="manual")
    harness.run(max_iterations=1, report=False)
    record = History(project).load()[-1]
    assert record.accepted and record.commit is None  # its only change was out of scope
    assert record.discarded == ["tests/test_x.py"]
    assert _refs(project, harness) == []


def test_no_scope_warns_at_run_start(project, capsys):
    _configure(project, gates=["true"])
    Harness(project, verbose=False, land_mode="manual").run(max_iterations=1, report=False)
    assert "the agent may edit the tests and benchmarks that gate it" in capsys.readouterr().out
