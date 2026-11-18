"""One run/target relation (`runs.landing`) and its one wording (`runs.landing_words`):
every reader (resume's refusal, `land`, `delete`, the runs table, the web run list)
answers from the same computation."""

import pytest

from autohelix.harness import Budget, Harness
from autohelix.run_table import _landing
from autohelix.runs import Landing, RunLock, delete, git, land, landing, landing_words, short_id


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
    words = landing_words(landing(paths, data), paths.run_id, "manual")
    assert words.cell == "1 to land" and words.sentence.endswith(f"main can fast-forward: `autohelix land {short}`.")
    assert (words.note, words.commands) == ("manual landing", (f"autohelix land {short}",))

    with RunLock(paths.lock):
        land(paths, data, harness.run_state.expected_tip)
    assert relation(paths, data) == ("same", 0, 0)
    assert landing_words(landing(paths, data), paths.run_id).cell == "landed"

    commit_on_main(project, "user")
    assert relation(paths, data) == ("behind", 0, 1)
    words = landing_words(landing(paths, data), paths.run_id)
    assert (words.cell, words.commands) == ("landed", ())  # nothing to land: no command

    resumed = Harness(project, land_mode="manual")  # continues its own tip, without the user's commit
    resumed.run(Budget(until=2), report=False)
    assert relation(paths, data) == ("diverged", 1, 1)
    words = landing_words(landing(paths, data), paths.run_id, "manual")
    assert (words.cell, words.note) == ("1 diverged", "diverged from main")
    assert words.commands == (f"git merge autohelix/{paths.run_id}",)  # never `autohelix land`: it can't
    # `land` refuses in the same words, and `delete` counts the same unlanded commit.
    data = paths.read_identity()  # manual since the resume
    with RunLock(paths.lock), pytest.raises(RuntimeError) as refused:
        land(paths, data, resumed.run_state.expected_tip)
    assert str(refused.value) == f"Cannot land: {words.sentence}"
    assert "1 commit on autohelix/" in words.sentence and "1 commit on main that it lacks" in words.sentence
    with pytest.raises(RuntimeError, match=r"^Not deleting run \w+, which has 1 unlanded commit: main diverged from run .*`git merge autohelix/.*`autohelix delete --force \w+` discards it\.$"):
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
    words = landing_words(found, harness.paths.run_id)
    run = {"id": harness.paths.run_id, "landing": None, "target_branch": None, "accepted": 1,
           "landing_words": {"cell": words.cell, "text": words.sentence, "note": words.note, "commands": []}}
    assert _landing(run) == "—" and words.sentence.startswith("Started on a detached HEAD")


def test_diverged_words_for_an_auto_landing_run_say_how_to_stop_landing():
    found = Landing("t", "main", "m", "diverged", 2, 1, True)
    run_id = "20261117-1200-sorting-krp3"
    auto = landing_words(found, run_id, "auto").sentence
    assert auto == ("main diverged from run krp3: 2 commits on autohelix/20261117-1200-sorting-krp3 that main "
                    "lacks, 1 commit on main that it lacks, so `autohelix land` can't fast-forward main. Merge it "
                    "by hand: `git merge autohelix/20261117-1200-sorting-krp3`, or `autohelix resume krp3 --land "
                    "manual` to stop trying to land it.")
    assert "--land manual" not in landing_words(found, run_id, "manual").sentence


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
    assert landing_words(landing(paths, data), paths.run_id).cell == "—"  # tip is base: nothing accepted


def test_resume_refusal_reads_the_relation(project):
    """An auto-landing resume refuses a run whose target is `behind`: the harness's
    check before writing and `reconcile` under the lock say the same."""
    harness = Harness(project)
    harness.run(Budget(until=1), report=False)  # lands
    commit_on_main(project, "user")
    assert relation(harness.paths, harness.run_info) == ("behind", 0, 1)
    with pytest.raises(RuntimeError, match=r"^Everything run \w+ accepted is in main, which has 1 commit past its tip \(committed after it\)\. "):
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


def squash_main_onto_base(project, data):
    """Replace main's commits since the run's base with one commit of the same tree, as a
    squash-merged PR leaves them."""
    tree = git(project, "rev-parse", "main^{tree}").stdout.strip()
    squashed = git(project, "commit-tree", tree, "-p", data["base_commit"], "-m", "Squashed PR").stdout.strip()
    git(project, "reset", "-q", "--hard", squashed)


def test_a_landed_run_stays_landed_after_a_squash_merged_pr(project):
    """`land` records `landed` in run.json; a target that has the work as one squashed
    commit reads landed, and only commits accepted after it count."""
    from autohelix.runs import RunPaths

    harness = Harness(project, land_mode="manual")
    harness.run(Budget(until=1), report=False)
    paths = harness.paths
    tip = harness.run_state.expected_tip
    with RunLock(paths.lock):
        land(paths, harness.run_info, tip)
    data = RunPaths(project, paths.run_id).read_identity()
    assert (data["landed"]["tip"], data["landed"]["target"]) == (tip, "main") and data["landed"]["at"]
    squash_main_onto_base(project, data)
    assert relation(paths, data) == ("landed", 0, 1)
    words = landing_words(landing(paths, data), paths.run_id, "manual")
    assert (words.cell, words.commands) == ("landed", ())
    assert "squash-merged" in words.sentence
    with RunLock(paths.lock):
        land(paths, data, tip)  # already there: nothing to do

    resumed = Harness(project, land_mode="manual")
    resumed.run(Budget(until=2), report=False)
    data = RunPaths(project, paths.run_id).read_identity()
    assert relation(paths, data) == ("diverged", 1, 1)  # the resume's commit, not the landed ones
    words = landing_words(landing(paths, data), paths.run_id, "manual")
    assert (words.cell, words.commands) == ("1 diverged", (f"git merge autohelix/{paths.run_id}",))


def test_a_squash_merge_without_land_stays_diverged(project):
    harness = Harness(project, land_mode="manual")
    harness.run(Budget(until=1), report=False)
    data = harness.paths.read_identity()
    assert "landed" not in data
    squash_main_onto_base(project, data)
    assert relation(harness.paths, data) == ("diverged", 1, 1)


def test_an_auto_resume_after_a_squash_merged_landing_refuses(project):
    harness = Harness(project)
    harness.run(Budget(until=1), report=False)  # auto landing records `landed`
    data = harness.paths.read_identity()
    assert data["landed"]["tip"] == harness.run_state.expected_tip
    squash_main_onto_base(project, data)
    with pytest.raises(RuntimeError, match=r"landed on main \(`autohelix land`\), which has other commits in their place since .* --import"):
        Harness(project)
    delete(harness.paths)  # nothing unlanded: no --force needed
    assert not harness.paths.run_dir.exists()


def squash_onto(project, branch, work):
    """Squash-merge `work` into `branch` (checked out) as one commit of its changes."""
    git(project, "switch", "-q", branch)
    git(project, "merge", "-q", "--squash", work)
    git(project, "commit", "-qm", "Squashed PR")


def test_the_pr_flow_reads_landed_once_squash_merged(project):
    """`autohelix land`, push, a squash-merged PR (main moved meanwhile), the local main
    reset to the remote's: the run reads landed, also after main changes the same lines
    again (no window: the recorded landing on main is the answer)."""
    harness = Harness(project, land_mode="manual")
    harness.run(Budget(until=1), report=False)
    paths, tip, base = harness.paths, harness.run_state.expected_tip, harness.run_info["base_commit"]
    with RunLock(paths.lock):
        land(paths, harness.run_info, tip)
    data = paths.read_identity()
    git(project, "branch", "remote-main", base)  # the remote's main, moving while the PR is open
    git(project, "switch", "-q", "remote-main")
    commit_on_main(project, "unrelated")
    squash_onto(project, "remote-main", tip)
    git(project, "switch", "-q", "main")
    git(project, "reset", "-q", "--hard", "remote-main")  # the local main reset to the remote's
    assert relation(paths, data) == ("landed", 0, 2)
    (project / "code.py").write_text("print('changed on main since')\n")  # the same line, after the PR
    git(project, "commit", "-qam", "later")
    assert relation(paths, data) == ("landed", 0, 3)


def test_a_landing_on_another_branch_is_not_a_landing_on_the_target(project):
    """`land --into other`, then that work squash-merged into the run's own target: the
    recorded landing names another branch, so main reads diverged."""
    harness = Harness(project, land_mode="manual")
    harness.run(Budget(until=1), report=False)
    paths, tip = harness.paths, harness.run_state.expected_tip
    git(project, "branch", "other", harness.run_info["base_commit"])
    with RunLock(paths.lock):
        land(paths, harness.run_info, tip, into="other")
    data = paths.read_identity()
    assert data["landed"]["target"] == "other"
    squash_onto(project, "main", "other")
    assert relation(paths, data) == ("diverged", 1, 1)
    with RunLock(paths.lock), pytest.raises(RuntimeError, match="^Cannot land: main diverged from run"):
        land(paths, data, tip)


def test_a_landing_on_one_branch_is_not_a_landing_on_another(project):
    harness = Harness(project, land_mode="manual")
    harness.run(Budget(until=1), report=False)
    paths, tip = harness.paths, harness.run_state.expected_tip
    git(project, "branch", "release", harness.run_info["base_commit"])
    with RunLock(paths.lock):
        land(paths, harness.run_info, tip)
    data = paths.read_identity()
    git(project, "switch", "-q", "release")
    commit_on_main(project, "hotfix")  # on release
    git(project, "switch", "-q", "main")
    assert landing(paths, data, into="release").relation == "diverged"
    with RunLock(paths.lock), pytest.raises(RuntimeError, match="^Cannot land: release diverged from run"):
        land(paths, data, tip, into="release")


def test_the_not_available_words_offer_into_only_when_the_target_is_missing(project):
    harness = Harness(project, land_mode="manual")
    harness.run(Budget(until=1), report=False)
    paths, data = harness.paths, harness.run_info
    short = short_id(paths.run_id)
    gone = landing(paths, {**data, "target_branch": "gone"})
    assert (gone.relation, gone.tip is not None, gone.target_tip) == (None, True, None)
    assert landing_words(gone, paths.run_id).sentence == (
        f"Branch gone is not available (`autohelix land {short} --into <branch>` lands it on another branch).")
    git(project, "update-ref", "-d", paths.ref)  # the run's own commits gone
    missing = landing_words(landing(paths, data), paths.run_id).sentence
    assert missing == f"Run {short}'s commits are not available (no autohelix/{paths.run_id})."
    assert "--into" not in missing


def test_landing_words_state_and_missing_are_the_sentence_s_own_clauses():
    """`state` (the sentence without its advice: an auto-landing resume's warning) and
    `missing` (`run --import`'s land-first notice) come from the same wording."""
    from autohelix.runs import landing_words

    run_id = "20261117-1200-perf-k3f9"
    ahead = landing_words(Landing("t", "main", "m", "ahead", 2, 0, True), run_id, "auto")
    assert ahead.state == f"2 commits on autohelix/{run_id} not in main yet."
    assert ahead.sentence == f"2 commits on autohelix/{run_id} not in main yet; main can fast-forward: `autohelix land k3f9`."
    assert ahead.missing == "k3f9's 2 accepted commits aren't in main"
    diverged = landing_words(Landing("t", "main", "m", "diverged", 1, 3, True), run_id)
    assert diverged.sentence.startswith(diverged.state[:-1] + ", so `autohelix land` can't fast-forward main.")
    assert diverged.missing == "k3f9's 1 accepted commit isn't in main"
    behind = landing_words(Landing("t", "main", "m", "behind", 0, 1, True), run_id)
    assert behind.sentence == "Everything run k3f9 accepted is in main, which has 1 commit past its tip (committed after it)."
    assert behind.missing == "" and behind.state == behind.sentence
    unchanged = landing_words(Landing("t", "main", "m", "behind", 0, 1, False), run_id)
    assert unchanged.sentence == ("main has 1 commit past run k3f9's base (committed since it started; it hasn't "
                                  "accepted any changes).")
    assert landing_words(Landing("t", "main", None, None, 2, 0, True), run_id).sentence == (
        "Branch main is not available (`autohelix land k3f9 --into <branch>` lands it on another branch).")
