"""One run/target relation (`runs.landing`) and its one wording (`runs.landing_words`):
every reader (resume's refusal, `land`, `delete`, the runs table, the web run list)
answers from the same computation."""

import pytest

from autohelix.harness import Budget, Harness
from autohelix.run_table import _landing
from autohelix.runs import RunLock, delete, git, land, landing, landing_words, short_id


def commit_on_main(project, name):
    (project / f"{name}.txt").write_text(name)
    git(project, "add", ".")
    git(project, "commit", "-qm", name)


def relation(paths, data):
    found = landing(paths, data)
    return found.relation, found.ahead, found.behind


def test_each_relation_with_its_counts_and_words(project):
    harness = Harness(project, land_mode="manual")
    harness.run(Budget(until=1), report=False)
    paths, data = harness.paths, harness.run_info
    short = short_id(paths.run_id)
    assert relation(paths, data) == ("ahead", 1, 0)
    cell, sentence = landing_words(landing(paths, data).payload(), paths.run_id, "main")
    assert cell == "1 to land" and sentence.endswith(f"main can fast-forward: `autohelix land {short}`.")

    with RunLock(paths.lock):
        land(paths, data, harness.run_state.expected_tip)
    assert relation(paths, data) == ("same", 0, 0)
    assert landing_words(landing(paths, data).payload(), paths.run_id, "main")[0] == "landed"

    commit_on_main(project, "user")
    assert relation(paths, data) == ("behind", 0, 1)
    assert landing_words(landing(paths, data).payload(), paths.run_id, "main")[0] == "landed"

    resumed = Harness(project, land_mode="manual")  # continues its own tip, without the user's commit
    resumed.run(Budget(until=2), report=False)
    assert relation(paths, data) == ("diverged", 1, 1)
    cell, sentence = landing_words(landing(paths, data).payload(), paths.run_id, "main")
    assert cell == "1 diverged"
    # `land` refuses in the same words, and `delete` counts the same unlanded commit.
    with RunLock(paths.lock), pytest.raises(RuntimeError) as refused:
        land(paths, data, resumed.run_state.expected_tip)
    assert str(refused.value) == f"Cannot land: {sentence}"
    assert "1 commit on autohelix/" in sentence and "1 commit on main that it lacks" in sentence
    with pytest.raises(RuntimeError, match="has 1 unlanded commit on"):
        delete(paths)


def test_ahead_counts_only_the_runs_own_commits(project):
    """A target reset behind the run's base: the target can fast-forward, and the base's
    commits it lacks aren't the run's (the web run list used to count them)."""
    commit_on_main(project, "before")
    harness = Harness(project, land_mode="manual")
    harness.run(Budget(until=1), report=False)
    git(project, "reset", "-q", "--hard", "HEAD~1")  # main before the run's base commit
    assert relation(harness.paths, harness.run_info) == ("ahead", 1, 0)


def test_no_target_branch_counts_every_accepted_commit(project):
    harness = Harness(project, land_mode="manual")
    harness.run(Budget(until=1), report=False)
    detached = {**harness.run_info, "target_branch": None}
    found = landing(harness.paths, detached)
    assert (found.relation, found.ahead, found.payload()) == (None, 1, None)
    run = {"id": harness.paths.run_id, "landing": None, "target_branch": None, "accepted": 1}
    assert _landing(run) == "—"


def test_resume_refusal_reads_the_relation(project):
    """An auto-landing resume refuses a run whose target is `behind`: the harness's
    check before writing and `reconcile` under the lock say the same."""
    harness = Harness(project)
    harness.run(Budget(until=1), report=False)  # lands
    commit_on_main(project, "user")
    assert relation(harness.paths, harness.run_info) == ("behind", 0, 1)
    with pytest.raises(RuntimeError, match=r"main has 1 commit past run \w+'s tip \(committed after its tip\)"):
        Harness(project)
