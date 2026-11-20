"""What `run` and `resume` say at their start about other runs, the run imported, a
failed first attempt, an uncommitted config and a group without manual landing."""

from autohelix.runs import git, short_id
from tests.helpers import CONFIG, config_file, cli, configure, latest_run
from tests.test_groups import identity, started


def before_header(result):
    """What it says before its header (`  AutoHelix  v…`), the start's notices."""
    assert "  AutoHelix  v" in result.output, result.output
    return " ".join(result.output.split("  AutoHelix  v")[0].split())


def test_import_of_an_unlanded_run_says_land_it_first_and_nothing_else(project):
    first, _ = started(project, "--land", "manual")  # its commit stays on autohelix/<id>
    short = short_id(first.run_id)
    _, result = started(project, "--import", short)
    said = before_header(result)
    assert (f"{short}'s 1 accepted commit isn't in main: land it first (`autohelix land {short}`, or "
            f"`git switch -c <branch> autohelix/{first.run_id}`) to start from it.") in said
    assert "Previous run" not in said and "This run starts from main without" not in said


def test_import_of_a_landed_run_says_nothing(project):
    first, _ = started(project)  # lands automatically
    _, result = started(project, "--import", short_id(first.run_id))
    assert "accepted commit" not in before_header(result) and "Previous run" not in before_header(result)


def test_import_of_a_diverged_run_offers_the_merge(project):
    first, _ = started(project, "--land", "manual")
    (project / "user.py").write_text("x = 1\n")
    git(project, "add", "user.py")
    git(project, "commit", "-qm", "user")
    short = short_id(first.run_id)
    _, result = started(project, "--import", short)
    assert (f"land it first (`git merge autohelix/{first.run_id}`, or `git switch -c <branch> "
            f"autohelix/{first.run_id}`)") in before_header(result)


def test_the_config_change_refusal_says_land_it_first(project):
    first, _ = started(project, "--land", "manual")
    short = short_id(first.run_id)
    configure(project, goal="Another goal")
    result = cli(project, "resume", short, "-n", "1", "--no-report")
    assert result.exit_code == 1
    said = " ".join(result.output.split())
    assert f"`autohelix run demo --import {short}`. {short}'s 1 accepted commit isn't in main: land it first" in said


def failing_baseline(project):
    configure(project, observe=[{"name": "bench", "command": "true", "metrics": {"score": "higher"}}])


def test_a_failed_baseline_is_a_disposable_attempt(project):
    failing_baseline(project)
    result = cli(project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 1
    paths = latest_run(project)
    short = short_id(paths.run_id)
    said = " ".join(result.output.split())
    assert (f"every declared metric needs a value at the baseline. Fix it and run `autohelix run demo` again "
            f"(`autohelix delete {short}` removes this attempt).") in said
    # The history keeps the error itself, without the advice.
    assert "Fix it" not in (paths.iteration_dir(0) / "record.json").read_text()

    again = cli(project, "run", "-n", "1", "--no-report")
    said = before_header(again)
    assert f"Previous run for this config: {short}, without an accepted baseline (`autohelix delete {short}`" in said
    assert "resume" not in said and "--import" not in said

    configure(project, goal="Fixed")
    refused = cli(project, "resume", short, "-n", "1", "--no-report")
    said = " ".join(refused.output.split())
    assert refused.exit_code == 1 and f"run {short} has no accepted baseline: start a new run, `autohelix run demo`" in said
    assert "--import" not in said and "restore the file" not in said


def test_an_untracked_prompt_template_warns_under_auto_landing(project):
    configure(project, prompt_template="../../prompt.md")  # private config, untracked template outside autohelix/
    (project / "prompt.md").write_text("Goal: {{ goal }}\n")
    _, result = started(project)
    warning = "commit prompt.md first: a commit on main after this run lands makes `resume` refuse."
    assert warning in before_header(result) and "commit autohelix/demo/config.yaml" not in result.output
    _, result = started(project, "--land", "manual")
    assert "commit prompt.md first" not in result.output


def test_an_untracked_config_warns_and_an_ignored_one_doesnt(project):
    exclude = project / ".git" / "info" / "exclude"  # autohelix/ shared: not private as setup makes it
    exclude.write_text("".join(line for line in exclude.read_text().splitlines(keepends=True)
                               if line.strip() != "/autohelix/"))
    config_file(project).parent.rename(project / "autohelix" / "perf")  # untracked, not ignored
    _, result = started(project, "perf")
    assert "commit autohelix/perf/config.yaml first" in before_header(result)
    (project / ".gitignore").write_text("autohelix/\n")
    git(project, "add", ".gitignore")
    git(project, "commit", "-qm", "ignore")
    _, result = started(project, "perf")
    assert "commit autohelix/perf/config.yaml first" not in result.output


def test_a_committed_config_doesnt_warn(project):
    git(project, "add", "-f", CONFIG)  # an experiment shared with the project
    git(project, "commit", "-qm", "config")
    _, result = started(project)
    assert "first: a commit on" not in result.output


def test_resume_group_without_land_says_it_still_lands_automatically(project):
    paths, _ = started(project)  # auto landing
    short = short_id(paths.run_id)
    result = cli(project, "resume", short, "--group", "g", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert f"{short} still lands automatically; pass `--land manual` for a group." in " ".join(result.output.split())
    assert identity(paths).get("land") == "auto"
    result = cli(project, "resume", short, "--group", "g", "--until", "1")  # nothing to run: stored at once
    assert f"{short} still lands automatically" in " ".join(result.output.split())
    result = cli(project, "resume", short, "--group", "g", "--land", "manual", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert "still lands automatically" not in result.output


def test_import_still_warns_about_a_live_run_of_the_config(project):
    from autohelix.runs import RunLock

    first, _ = started(project)  # lands automatically: nothing to land first
    short = short_id(first.run_id)
    with RunLock(first.lock):  # another terminal is running it
        _, result = started(project, "--import", short)
    said = before_header(result)
    assert f"{short} is also running for this config. Both land on main" in said
    assert "accepted commit" not in said and "Previous run" not in said


def test_import_on_another_branch_lands_into_it(project):
    first, _ = started(project, "--land", "manual")
    short = short_id(first.run_id)
    git(project, "switch", "-qc", "perf")  # at main, without the run's commit
    _, result = started(project, "--import", short)
    assert (f"{short}'s 1 accepted commit isn't in perf: land it first (`autohelix land {short} --into perf`, or "
            f"`git switch -c <branch> autohelix/{first.run_id}`) to start from it.") in before_header(result)


def test_import_on_a_detached_head_offers_the_branch(project):
    first, _ = started(project, "--land", "manual")
    short = short_id(first.run_id)
    git(project, "switch", "-q", "--detach", "main")
    _, result = started(project, "--import", short, "--land", "manual")  # detached: nothing to land on
    assert (f"{short}'s 1 accepted commit isn't in HEAD: land it first (`git switch -c <branch> "
            f"autohelix/{first.run_id}`) to start from it.") in before_header(result)
