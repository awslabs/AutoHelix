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
    words = landing_words(landing(paths, data).payload(), paths.run_id, "main", land_mode="manual")
    assert words.cell == "1 to land" and words.sentence.endswith(f"main can fast-forward: `autohelix land {short}`.")
    assert (words.note, words.commands) == ("manual landing", (f"autohelix land {short}",))

    with RunLock(paths.lock):
        land(paths, data, harness.run_state.expected_tip)
    assert relation(paths, data) == ("same", 0, 0)
    assert landing_words(landing(paths, data).payload(), paths.run_id, "main").cell == "landed"

    commit_on_main(project, "user")
    assert relation(paths, data) == ("behind", 0, 1)
    words = landing_words(landing(paths, data).payload(), paths.run_id, "main")
    assert (words.cell, words.commands) == ("landed", ())  # nothing to land: no command

    resumed = Harness(project, land_mode="manual")  # continues its own tip, without the user's commit
    resumed.run(Budget(until=2), report=False)
    assert relation(paths, data) == ("diverged", 1, 1)
    words = landing_words(landing(paths, data).payload(), paths.run_id, "main", land_mode="manual")
    assert (words.cell, words.note) == ("1 diverged", "diverged from main")
    assert words.commands == (f"git merge autohelix/{paths.run_id}",)  # never `autohelix land`: it can't
    # `land` refuses in the same words, and `delete` counts the same unlanded commit.
    data = paths.read_identity()  # manual since the resume
    with RunLock(paths.lock), pytest.raises(RuntimeError) as refused:
        land(paths, data, resumed.run_state.expected_tip)
    assert str(refused.value) == f"Cannot land: {words.sentence}"
    assert "1 commit on autohelix/" in words.sentence and "1 commit on main that it lacks" in words.sentence
    with pytest.raises(RuntimeError, match=rf"has 1 unlanded commit on .*land them first \(`git merge autohelix/"):
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


def test_diverged_words_for_an_auto_landing_run_say_how_to_stop_landing():
    payload = {"relation": "diverged", "commits": 2, "behind": 1, "fast_forward": False}
    run_id = "20261117-1200-sorting-krp3"
    auto = landing_words(payload, run_id, "main", land_mode="auto").sentence
    assert auto == ("main diverged from run krp3: 2 commits on autohelix/20261117-1200-sorting-krp3 that main "
                    "lacks, 1 commit on main that it lacks, so `autohelix land` can't fast-forward main. Merge it "
                    "by hand: `git merge autohelix/20261117-1200-sorting-krp3`, or `autohelix resume krp3 --land "
                    "manual` to stop trying to land it.")
    assert "--land manual" not in landing_words(payload, run_id, "main", land_mode="manual").sentence


def test_a_tip_with_no_commits_of_its_own_is_never_diverged(project):
    """The target reset behind the run's base, then committed on: the run accepted
    nothing, so it has nothing to land (`behind`, not `0 diverged`)."""
    commit_on_main(project, "before")
    harness = Harness(project, land_mode="manual")
    harness.run(Budget(until=1), report=False)
    paths, data = harness.paths, harness.run_info
    git(project, "update-ref", paths.ref, data["base_commit"])  # as if it had accepted nothing
    git(project, "reset", "-q", "--hard", "HEAD~1")
    commit_on_main(project, "other")
    assert relation(paths, data) == ("behind", 0, 1)
    assert landing_words(landing(paths, data).payload(), paths.run_id, "main", accepted=False).cell == "—"


def test_resume_refusal_reads_the_relation(project):
    """An auto-landing resume refuses a run whose target is `behind`: the harness's
    check before writing and `reconcile` under the lock say the same."""
    harness = Harness(project)
    harness.run(Budget(until=1), report=False)  # lands
    commit_on_main(project, "user")
    assert relation(harness.paths, harness.run_info) == ("behind", 0, 1)
    with pytest.raises(RuntimeError, match=r"main has 1 commit past run \w+'s tip \(committed after its tip\)"):
        Harness(project)


def test_resume_of_a_diverged_auto_run_says_what_works(project, capsys):
    """`reconcile`'s warning is the relation's sentence: the merge that works, and, for a
    run that lands automatically, how to stop trying."""
    harness = Harness(project, land_mode="manual")
    harness.run(Budget(until=1), report=False)
    commit_on_main(project, "user")
    capsys.readouterr()
    resumed = Harness(project, land_mode="auto")
    try:
        out = " ".join(capsys.readouterr().out.split())
        run_id, short = harness.paths.run_id, short_id(harness.paths.run_id)
        assert f"main diverged from run {short}" in out
        assert (f"Merge it by hand: `git merge autohelix/{run_id}`, or `autohelix resume {short} --land manual` "
                f"to stop trying to land it. Run {short} continues from its accepted tip.") in out
    finally:
        resumed.close()
