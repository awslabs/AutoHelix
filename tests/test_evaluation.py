"""Evaluation within an iteration: gate commands, then observe entries with the metric
gates on each entry's numbers; observations stored by entry name; the variables gate,
observe and stop commands get ($AUTOHELIX_CANDIDATE, _BASE, _PROJECT, _SCRATCH,
_PREVIOUS, _OBSERVATIONS)."""

import json
import os
import subprocess

import pytest
import yaml

from autohelix.checks import run_observe_entry
from autohelix.config import Config, ObserveEntry, capture_problem
from autohelix.harness import Harness
from autohelix.history import History
from autohelix.iteration import IterationWorkspace


def _git(project, *args) -> str:
    return subprocess.run(["git", *args], cwd=project, capture_output=True, text=True, check=True).stdout.strip()


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


@pytest.fixture
def log(tmp_path):
    """A file outside the project the commands append to, to show what ran."""
    return tmp_path / "ran.log"


def _configure(project, **sections) -> None:
    config = {"goal": "Test evaluation",
              "agent": {"backend": "mock", "settings": {"change_file": "hello.py",
                                                        "change_content": "# iteration {iteration}"}},
              **sections}
    (project / "autohelix.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "config")


def _run(project, n: int) -> list:
    Harness(project, verbose=False).run(max_iterations=n, report=False)
    return History(project).load()


def _lines(path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def _observations(project):
    return next((project / ".autohelix" / "runs").iterdir()) / "observations"


# --- order and short-circuit ---------------------------------------------------


def test_failing_gate_skips_later_gates_and_observe(project, log):
    _configure(project, gates=[
        f'echo "$AUTOHELIX_ITERATION first" >> {log}',
        f'echo "$AUTOHELIX_ITERATION second" >> {log}; echo broken; [ "$AUTOHELIX_ITERATION" = 0 ] || exit 3',
        f'echo "$AUTOHELIX_ITERATION third" >> {log}',
        {"metric": "speed", "regress": "5%"},
    ], observe=[{"name": "bench", "command": f'echo "$AUTOHELIX_ITERATION bench" >> {log}; echo "##autohelix[speed=1]"',
                 "metrics": {"speed": "higher"}}])
    baseline, record = _run(project, 1)

    assert _lines(log) == ["0 first", "0 second", "0 third", "0 bench", "1 first", "1 second"]
    assert not record.accepted
    assert record.reason.startswith("gate failed: echo")
    assert record.failure_output == "broken"
    assert [(g["kind"], g["passed"]) for g in record.gates] == [
        ("command", True), ("command", False), ("command", None), ("metric", None)]
    assert record.gates[1]["return_code"] == 3 and record.gates[1]["output"] == "broken"
    assert "output" not in record.gates[0] and record.gates[0]["return_code"] == 0
    assert record.gates[3]["text"] == "speed regress 5%"
    assert record.observe == [] and record.metrics == {}
    assert not (_observations(project) / "iter-1").exists()
    # The old field stays populated for now.
    assert [c["passed"] for c in record.constraints] == [True, False]
    # At the baseline the gates ran but didn't reject; metric gates aren't checked there.
    assert baseline.accepted
    assert [g["passed"] for g in baseline.gates] == [True, True, True, None]


def test_metric_gate_failure_after_an_entry_skips_the_next(project, log):
    _configure(project, gates=[{"metric": "speed", "regress": "5%"}], observe=[
        {"name": "bench", "command": 'if [ "$AUTOHELIX_ITERATION" = 0 ]; then echo "##autohelix[speed=10]"; '
                                     'else echo "##autohelix[speed=5]"; fi',
         "metrics": {"speed": "higher"}},
        {"name": "later", "command": f'echo "$AUTOHELIX_ITERATION later" >> {log}'},
    ])
    _, record = _run(project, 1)

    assert _lines(log) == ["0 later"]
    assert not record.accepted
    assert record.reason == "metric gate: speed 5 is 50.0% worse than 10 (regress: 5% allowed)"
    assert record.metrics == {"speed": 5}
    assert [o["name"] for o in record.observe] == ["bench"]
    [gate] = record.gates
    assert gate["passed"] is False and gate["value"] == 5 and gate["reference"] == 10
    assert gate["reason"] == record.reason
    # A rejected iteration still stores the output of the entries that ran.
    assert (_observations(project) / "iter-1" / "bench" / "stdout.txt").read_text() == "##autohelix[speed=5]\n"
    assert not (_observations(project) / "iter-1" / "later").exists()


# --- failures continue ---------------------------------------------------------


FLAKY = ('echo "##autohelix[score=7]"; echo oops >&2; [ "$AUTOHELIX_ITERATION" = 0 ] || exit 2')


def test_failed_entry_contributes_no_metrics_and_the_run_continues(project, log):
    _configure(project, observe=[
        {"name": "flaky", "command": FLAKY, "metrics": {"score": "higher"}},
        {"name": "other", "command": f'echo "$AUTOHELIX_ITERATION other" >> {log}; echo "##autohelix[size=3]"',
         "metrics": {"size": "lower"}},
    ])
    baseline, record = _run(project, 1)

    assert baseline.metrics == {"score": 7, "size": 3}
    assert record.accepted
    assert record.metrics == {"size": 3}  # not score: its entry failed
    assert _lines(log) == ["0 other", "1 other"]
    flaky, other = record.observe
    assert flaky["name"] == "flaky" and flaky["ok"] is False
    assert flaky["return_code"] == 2 and flaky["error"] == "exited 2" and flaky["metrics"] == {}
    assert isinstance(flaky["duration_s"], float)
    assert other == {**other, "name": "other", "ok": True, "return_code": 0, "error": None, "metrics": {"size": 3}}
    # Its output is kept: stdout and stderr apart, stderr.txt only when there is any.
    flaky_dir = _observations(project) / "iter-1" / "flaky"
    assert (flaky_dir / "stdout.txt").read_text() == "##autohelix[score=7]\n"
    assert (flaky_dir / "stderr.txt").read_text() == "oops\n"
    assert not (_observations(project) / "iter-1" / "other" / "stderr.txt").exists()
    persisted = json.loads(History(project).history_path.read_text().splitlines()[-1])
    assert persisted["observe"][0]["error"] == "exited 2"


def test_gate_on_a_failed_entrys_metric_fails(project, log):
    _configure(project, gates=[{"metric": "score", "regress": "50%"}], observe=[
        {"name": "flaky", "command": FLAKY, "metrics": {"score": "higher"}},
        {"name": "other", "command": f'echo "$AUTOHELIX_ITERATION other" >> {log}'},
    ])
    _, record = _run(project, 1)
    assert not record.accepted
    assert record.reason == "metric gate: score was not measured"
    assert record.gates[0]["passed"] is False and record.gates[0]["value"] is None
    assert _lines(log) == ["0 other"]  # rejected right after the entry: nothing was verified


def test_observe_entry_timeout_keeps_partial_output(tmp_path):
    entry = ObserveEntry(name="slow", command="echo '##autohelix[a=1]'; echo partial; sleep 10",
                         metrics={"a": "higher"}, timeout=1)
    result = run_observe_entry(entry, tmp_path)
    assert not result.ok and result.error == "timed out after 1s" and result.return_code == -1
    assert result.values == {} and "partial" in result.output
    assert "slow" in result.errors["a"]


def test_observe_entry_that_cannot_start(tmp_path):
    entry = ObserveEntry(name="gone", command="true", metrics={"a": "higher"})
    result = run_observe_entry(entry, tmp_path / "missing")
    assert not result.ok and result.error.startswith("could not run: ") and result.values == {}


def test_review_entry_missing_a_declared_number_fails(tmp_path):
    entry = ObserveEntry(name="progress", command="echo '##autohelix[done=1]'; echo 'missing: 0'",
                         metrics={"done": "higher", "missing": "lower"}, review="Count.")
    result = run_observe_entry(entry, tmp_path)
    assert not result.ok and result.error == "did not report missing" and result.values == {}


# --- storage -------------------------------------------------------------------


def test_observations_by_name_with_captures_at_their_relative_paths(project, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("outside\n")
    _configure(project, observe=[
        {"name": "bench",
         "command": (f"mkdir -p a b; echo A > a/log.txt; echo B > b/log.txt; ln -sf {secret} link.txt; "
                     "mkdir -p out/sub; echo deep > out/sub/x.txt; ln -sf ../../hello.py out/sub/y.txt; echo bench"),
         "capture": ["a/log.txt", "b/log.txt", "link.txt", "missing.txt", "out"]},
        {"name": "lint", "command": "echo lint; mkdir -p a; echo L > a/log.txt", "capture": ["a/log.txt"]},
    ])
    _, record = _run(project, 1)
    assert record.accepted
    assert record.observe[0]["capture_errors"] == ["capture 'link.txt' skipped: link.txt is a symlink"]
    for iteration in (0, 1):
        folder = _observations(project) / f"iter-{iteration}"
        assert sorted(p.name for p in folder.iterdir()) == ["bench", "lint"]
        bench = folder / "bench"
        assert (bench / "stdout.txt").read_text() == "bench\n"
        assert (bench / "a" / "log.txt").read_text() == "A\n"
        assert (bench / "b" / "log.txt").read_text() == "B\n"
        assert (bench / "out" / "sub" / "x.txt").read_text() == "deep\n"
        assert not (bench / "out" / "sub" / "y.txt").exists()  # symlinks are not followed
        assert not (bench / "link.txt").exists() and not (bench / "secret.txt").exists()
        assert (folder / "lint" / "a" / "log.txt").read_text() == "L\n"
        assert (folder / "lint" / "stdout.txt").read_text() == "lint\n"
        assert not list(folder.glob("stdout-*"))


@pytest.mark.parametrize("capture", ["../secret.txt", "/etc/passwd", "stdout.txt", "STDERR.TXT", ".git/config",
                                     "sub/.autohelix/x", ".autohelix-iteration", "a/.GIT"])
def test_refused_captures_are_config_errors(capture):
    config = Config.from_dict({"goal": "g", "observe": [{"name": "bench", "command": "true", "capture": [capture]}]})
    errors = [i.message for i in config.validate() if i.level == "error"]
    assert any(f"capture {capture!r}" in message for message in errors), errors
    assert capture_problem("sub/stdout.txt") is None  # only the entry's own files, at the top


def test_refused_captures_are_skipped_at_capture_time(tmp_path):
    root, stage = tmp_path / "copy", tmp_path / "stage"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text("secret")
    (root / "STDOUT.TXT").write_text("planted")
    stage.mkdir()
    history = History(tmp_path)
    problems = history.stage_output(stage, "out\n", "", root, ["../x", ".git/config", "STDOUT.TXT", ".git"])
    assert len(problems) == 4 and all("skipped" in p for p in problems)
    assert sorted(p.name for p in stage.iterdir()) == ["stdout.txt"]
    assert (stage / "stdout.txt").read_text() == "out\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable files")
def test_special_and_unreadable_captures_are_recorded_not_fatal(project):
    _configure(project, observe=[
        {"name": "bench",
         "command": ("mkdir -p out; echo ok > out/x.txt; mkfifo out/pipe; echo s > out/locked; chmod 000 out/locked; "
                     "mkfifo top.fifo; echo t > top.txt; chmod 000 top.txt; echo bench"),
         "capture": ["out", "top.fifo", "top.txt"]},
        {"name": "after", "command": "echo after"},
    ])
    _, record = _run(project, 1)
    assert record.accepted
    bench, after = record.observe
    assert after["ok"] and "capture_errors" not in after
    errors = bench["capture_errors"]
    assert len(errors) == 3, errors
    assert errors[0].startswith("capture 'out' not saved completely: ") and "locked" in errors[0]
    assert errors[1] == "capture 'top.fifo' skipped: not a regular file or folder"
    assert errors[2].startswith("capture 'top.txt' not saved completely: ")
    stored = _observations(project) / "iter-1" / "bench"
    assert (stored / "out" / "x.txt").read_text() == "ok\n"
    assert not (stored / "out" / "pipe").exists() and not (stored / "top.fifo").exists()


def test_output_replaces_what_earlier_commands_wrote_there(project, log):
    # A gate and an earlier entry plant files in a later entry's folder by absolute path:
    # neither survives, the entry's folder holds only its own output.
    plant = ('d="$AUTOHELIX_RUN_DIR/observations/iter-$AUTOHELIX_ITERATION/later"; mkdir -p "$d"; '
             'echo planted > "$d/stdout.txt"; echo planted > "$d/{name}"')
    _configure(project, gates=[plant.format(name="from-gate")], observe=[
        {"name": "first", "command": plant.format(name="from-entry")},
        {"name": "later", "command": "echo mine"},
    ])
    records = _run(project, 1)
    assert [r.accepted for r in records] == [True, True]
    for iteration in (0, 1):
        later = _observations(project) / f"iter-{iteration}" / "later"
        assert sorted(p.name for p in later.iterdir()) == ["stdout.txt"]
        assert (later / "stdout.txt").read_text() == "mine\n"


def test_observations_are_cleared_when_an_iteration_starts(project, log):
    _configure(project, observe=[{"name": "bench", "command": f'ls "$AUTOHELIX_RUN_DIR/observations/" >> {log}'}])
    harness = Harness(project, verbose=False)
    try:
        harness._capture_baseline()
        stale = harness.paths.observations / "iter-1" / "bench"
        stale.mkdir(parents=True)
        (stale / "stdout.txt").write_text("from a killed attempt\n")
        seen = {}
        original = harness.build_prompt

        def build_prompt(iteration, worktree_dir, members=()):
            seen["shown"] = sorted(p.name for p in (worktree_dir / ".autohelix-iteration" / "observations").iterdir())
            return original(iteration, worktree_dir, members)

        harness.build_prompt = build_prompt
        record = harness.run_iteration(1)
        assert record.accepted
        assert seen["shown"] == ["iter-0"]  # the agent didn't see the stale iter-1
    finally:
        harness.close()


# --- variables -----------------------------------------------------------------


def test_previous_is_a_copy_from_the_last_accepted_iteration(project, log):
    # Iteration 2 is rejected (score drops) after its entry ran: it never counts as previous.
    command = ('echo "iter=$AUTOHELIX_ITERATION"; '
               'if [ -n "${AUTOHELIX_PREVIOUS+set}" ]; then echo "prev=$(head -1 "$AUTOHELIX_PREVIOUS/stdout.txt")"; '
               f'echo "$AUTOHELIX_ITERATION $AUTOHELIX_PREVIOUS" >> {log}; '
               'echo tampered > "$AUTOHELIX_PREVIOUS/stdout.txt"; else echo prev=none; fi; '
               'if [ "$AUTOHELIX_ITERATION" = 2 ]; then echo "##autohelix[score=1]"; else echo "##autohelix[score=9]"; fi')
    _configure(project, gates=[{"metric": "score", "regress": "0%"}],
               observe=[{"name": "tracker", "command": command, "metrics": {"score": "higher"}}])
    records = _run(project, 3)
    assert [(r.iteration, r.accepted) for r in records] == [(0, True), (1, True), (2, False), (3, True)]

    def seen(iteration):
        return (_observations(project) / f"iter-{iteration}" / "tracker" / "stdout.txt").read_text().splitlines()[:2]

    assert seen(0) == ["iter=0", "prev=none"]  # unset at the first evaluation
    assert seen(1) == ["iter=1", "prev=iter=0"]  # the baseline counts
    assert seen(2) == ["iter=2", "prev=iter=1"]
    assert seen(3) == ["iter=3", "prev=iter=1"]  # not the rejected iteration 2
    # Writing to the copy changed nothing in run state, and the copy is gone.
    previous = [line.split(" ", 1)[1] for line in _lines(log)]
    assert len(previous) == 3 and len(set(previous)) == 3
    assert not any(os.path.exists(p) for p in previous)


def test_scratch_is_fresh_private_and_removed_after_each_command(project, log):
    check = (f'test -d "$AUTOHELIX_SCRATCH" && [ -z "$(ls -A "$AUTOHELIX_SCRATCH")" ] && '
             f'echo "$AUTOHELIX_SCRATCH" >> {log} && touch "$AUTOHELIX_SCRATCH/x"')
    gone = f'! test -e "$(tail -1 {log})"'  # the previous command's scratch
    _configure(project, gates=[check, gone, check],
               observe=[{"name": "bench", "command": f"{gone} && {check}"}],
               stop=[f"{gone} && {check} && exit 1"])
    harness = Harness(project, verbose=False)
    copies = harness.run_state.copies_root
    harness.run(max_iterations=1, report=False)
    records = History(project).load()
    assert [r.accepted for r in records] == [True, True]
    assert all(g["passed"] for r in records for g in r.gates)
    assert [o["ok"] for r in records for o in r.observe] == [True, True]
    scratches = _lines(log)
    assert len(scratches) == 2 * 4 and len(set(scratches)) == 8  # 2 gates, 1 entry, 1 stop; twice
    assert all(path.startswith(str(copies) + "/") for path in scratches)


def test_call_folder_is_removed_on_interrupt(project):
    _configure(project)
    harness = Harness(project, verbose=False)
    try:
        workspace = IterationWorkspace(1, harness.run_state, harness.history, harness.repo)
        with pytest.raises(KeyboardInterrupt):
            with workspace.call_folder() as folders:
                (folders["scratch"] / "x").write_text("x")
                folder = folders["scratch"].parent
                raise KeyboardInterrupt
        assert not folder.exists()
        workspace.close()
    finally:
        harness.close()


def test_candidate_base_and_project(project, log):
    record = f'echo "$AUTOHELIX_ITERATION $AUTOHELIX_CANDIDATE $AUTOHELIX_BASE $AUTOHELIX_PROJECT" >> {log}'
    _configure(project, gates=[record], observe=[{"name": "bench", "command": record}])
    base = _git(project, "rev-parse", "HEAD")
    harness = Harness(project, verbose=False)
    harness.run(max_iterations=1, report=False)
    commit = History(project).load()[1].commit
    assert commit
    a_tree, t_tree = _git(project, "rev-parse", f"{base}^{{tree}}"), _git(project, "rev-parse", f"{commit}^{{tree}}")
    assert _git(project, "rev-parse", f"{commit}^") == base
    expected = [f"0 {a_tree} {base} {project.resolve()}", f"1 {t_tree} {base} {project.resolve()}"]
    assert _lines(log) == [expected[0]] * 2 + [expected[1]] * 2


def test_stop_gets_a_copy_of_the_accepted_observations(project, log):
    _configure(project,
               observe=[{"name": "bench", "command": 'echo "iter=$AUTOHELIX_ITERATION"'}],
               stop=[f'cat "$AUTOHELIX_OBSERVATIONS/bench/stdout.txt" >> {log}; '
                     f'[ -z "${{AUTOHELIX_PREVIOUS+set}}" ] || echo previous-set >> {log}; '
                     'echo tampered > "$AUTOHELIX_OBSERVATIONS/bench/stdout.txt"; exit 1'])
    records = _run(project, 1)
    assert [r.accepted for r in records] == [True, True]
    assert _lines(log) == ["iter=0", "iter=1"]
    for iteration in (0, 1):
        stored = _observations(project) / f"iter-{iteration}" / "bench" / "stdout.txt"
        assert stored.read_text() == f"iter={iteration}\n"


def test_previous_does_not_leak_from_the_harness_environment(project, log, monkeypatch):
    monkeypatch.setenv("AUTOHELIX_PREVIOUS", "/nowhere")
    monkeypatch.setenv("AUTOHELIX_OBSERVATIONS", "/nowhere")
    _configure(project, gates=[f'echo "gate ${{AUTOHELIX_PREVIOUS-unset}} ${{AUTOHELIX_OBSERVATIONS-unset}}" >> {log}'],
               observe=[{"name": "bench",
                         "command": f'echo "bench ${{AUTOHELIX_PREVIOUS-unset}} ${{AUTOHELIX_OBSERVATIONS-unset}}" >> {log}'}])
    _run(project, 1)
    lines = _lines(log)
    assert lines[:2] == ["gate unset unset", "bench unset unset"]


# --- baseline ------------------------------------------------------------------


def test_failed_command_entry_at_the_baseline_is_a_setup_error(project):
    _configure(project, observe=[{"name": "bench", "command": "echo '##autohelix[speed=3]'; exit 1",
                                  "metrics": {"speed": "higher"}}])
    with pytest.raises(RuntimeError, match=r"did not produce required metric\(s\): speed \(bench exited 1\)"):
        Harness(project, verbose=False).run(max_iterations=1, report=False)
    [record] = History(project).load()
    assert not record.accepted and record.observe[0]["error"] == "exited 1"


def test_failed_entry_without_metrics_at_the_baseline_continues(project):
    _configure(project, gates=["exit 1", {"metric": "speed", "regress": "5%"}], observe=[
        {"name": "notes", "command": "echo nothing; exit 4"},
        {"name": "bench", "command": "echo '##autohelix[speed=3]'", "metrics": {"speed": "higher"}},
    ])
    harness = Harness(project, verbose=False)
    harness._capture_baseline()
    [baseline] = History(project).load()
    harness.close()
    assert baseline.accepted and baseline.metrics == {"speed": 3}
    assert [(o["name"], o["ok"]) for o in baseline.observe] == [("notes", False), ("bench", True)]
    assert [g["passed"] for g in baseline.gates] == [False, None]


@pytest.mark.parametrize("gated", [False, True])
def test_failed_review_entry_at_the_baseline(project, gated):
    gates = [{"metric": "missing", "improve": "0%"}] if gated else []
    _configure(project, gates=gates, observe=[{"name": "progress", "review": "Count.",
                                               "metrics": {"missing": "lower"}}])
    harness = Harness(project, verbose=False)
    harness.config.observe[0].command = "echo 'missing: 0'; exit 3"  # without `autohelix review`
    try:
        if gated:
            with pytest.raises(RuntimeError, match=r"required metric\(s\): missing \(progress exited 3\)"):
                harness._capture_baseline()
        else:
            harness._capture_baseline()
            [baseline] = History(project).load()
            assert baseline.accepted and baseline.metrics == {}
    finally:
        harness.close()


# --- review fixes: previous, order, tampering, interrupts ---------------------


def _first_lines(project, iteration, name="tracker"):
    return (_observations(project) / f"iter-{iteration}" / name / "stdout.txt").read_text().splitlines()[:2]


TRACKER = ('echo "iter=$AUTOHELIX_ITERATION"; '
           'if [ -n "${AUTOHELIX_PREVIOUS+set}" ]; then echo "prev=$(head -1 "$AUTOHELIX_PREVIOUS/stdout.txt")"; '
           'else echo prev=none; fi')


def test_previous_skips_iterations_where_the_entry_failed(project):
    _configure(project, observe=[{"name": "tracker", "command": TRACKER + '; [ "$AUTOHELIX_ITERATION" != 2 ]'}])
    records = _run(project, 3)
    assert [r.accepted for r in records] == [True] * 4
    assert records[2].observe[0]["ok"] is False
    assert _first_lines(project, 3) == ["iter=3", "prev=iter=1"]  # not the failed run in iteration 2


def test_an_accepted_no_op_counts_as_previous(project):
    # The agent's change is out of scope, discarded: every iteration is an accepted no-op.
    _configure(project, scope={"editable": ["hello.py"]}, observe=[{"name": "tracker", "command": TRACKER}])
    config = yaml.safe_load((project / "autohelix.yaml").read_text())
    config["agent"]["settings"]["change_file"] = "elsewhere.txt"
    (project / "autohelix.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    _git(project, "commit", "-qam", "out-of-scope change")
    records = _run(project, 3)
    assert [(r.accepted, r.commit) for r in records[1:]] == [(True, None)] * 3
    assert _first_lines(project, 3) == ["iter=3", "prev=iter=2"]


def test_previous_after_resume_and_for_an_entry_added_on_resume(project):
    _configure(project, observe=[{"name": "tracker", "command": TRACKER}])
    Harness(project, verbose=False, land_mode="manual").run(max_iterations=1, report=False)
    config = yaml.safe_load((project / "autohelix.yaml").read_text())
    config["observe"].append({"name": "fresh", "command": TRACKER})
    (project / "autohelix.yaml").write_text(yaml.safe_dump(config, sort_keys=False))  # uncommitted: after the baseline
    Harness(project, verbose=False).run(max_iterations=2, report=False)  # resume: iteration 2
    records = History(project).load()
    assert [(r.iteration, r.accepted) for r in records] == [(0, True), (1, True), (2, True)]
    assert _first_lines(project, 2) == ["iter=2", "prev=iter=1"]
    assert _first_lines(project, 2, "fresh") == ["iter=2", "prev=none"]


def test_baseline_retry_clears_observations_and_has_no_previous(project, tmp_path, log):
    marker = tmp_path / "second-attempt"
    _configure(project, gates=['d="$AUTOHELIX_RUN_DIR/observations/iter-0/ghost"; mkdir -p "$d"; touch "$d/x"'],
               observe=[{"name": "bench",
                         "command": (f'echo "prev=${{AUTOHELIX_PREVIOUS-unset}}" >> {log}; '
                                     f'[ -e {marker} ] && echo "##autohelix[speed=1]"; true'),
                         "metrics": {"speed": "higher"}}])
    with pytest.raises(RuntimeError, match="did not produce required metric"):
        Harness(project, verbose=False).run(max_iterations=1, report=False)
    observations = _observations(project) / "iter-0"
    assert sorted(p.name for p in observations.iterdir()) == ["bench", "ghost"]
    marker.touch()
    harness = Harness(project, verbose=False)  # resumes the run: a baseline retry
    harness.config.gates = []  # this attempt plants nothing: what's left is the first attempt's
    harness.run(max_iterations=1, report=False)
    records = History(project).load()
    assert [(r.iteration, r.accepted) for r in records][:2] == [(0, True), (1, True)]
    assert sorted(p.name for p in observations.iterdir()) == ["bench"]
    first, retry, iteration_1 = _lines(log)
    assert first == retry == "prev=unset" and iteration_1.startswith("prev=/")


def test_metric_gates_are_checked_right_after_their_own_entry(project, log):
    # Gates list b before a; observe runs a's entry first: a is checked (and fails) right
    # after it, b's entry never runs and b stays pending.
    _configure(project, gates=[{"metric": "b", "regress": "0%"}, {"metric": "a", "regress": "0%"}], observe=[
        {"name": "ea", "command": 'if [ "$AUTOHELIX_ITERATION" = 0 ]; then echo "##autohelix[a=5]"; '
                                  'else echo "##autohelix[a=1]"; fi', "metrics": {"a": "higher"}},
        {"name": "eb", "command": f'echo "$AUTOHELIX_ITERATION eb" >> {log}; echo "##autohelix[b=5]"',
         "metrics": {"b": "higher"}},
    ])
    _, record = _run(project, 1)
    assert not record.accepted and record.reason.startswith("metric gate: a 1")
    assert [g["passed"] for g in record.gates] == [None, False]
    assert _lines(log) == ["0 eb"]


def test_metric_gate_on_a_later_entry_passes_the_earlier_one(project, log):
    _configure(project, gates=[{"metric": "b", "regress": "0%"}, {"metric": "a", "regress": "0%"}], observe=[
        {"name": "ea", "command": 'echo "##autohelix[a=5]"', "metrics": {"a": "higher"}},
        {"name": "eb", "command": 'if [ "$AUTOHELIX_ITERATION" = 0 ]; then echo "##autohelix[b=5]"; '
                                  'else echo "##autohelix[b=1]"; fi', "metrics": {"b": "higher"}},
    ])
    _, record = _run(project, 1)
    assert not record.accepted and record.reason.startswith("metric gate: b 1")
    assert [g["passed"] for g in record.gates] == [False, True]


def test_ask_integrity_violation_in_a_gate_stops_later_commands(project, log):
    _configure(project, gates=[f'[ "$AUTOHELIX_ITERATION" = 0 ] || autohelix ask "Done?" || true',
                               f'echo "$AUTOHELIX_ITERATION later" >> {log}'],
               observe=[{"name": "bench", "command": f'echo "$AUTOHELIX_ITERATION bench" >> {log}'}])
    config = yaml.safe_load((project / "autohelix.yaml").read_text())
    config["agent"]["settings"]["judge_change_file"] = "planted.md"
    (project / "autohelix.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    _git(project, "commit", "-qam", "judge edits")
    _, record = _run(project, 1)
    assert not record.accepted
    assert _lines(log) == ["0 later", "0 bench"]
    first, second = record.gates
    assert first["passed"] is False and first["reason"] == record.reason  # it exited 0, but changed files
    assert second["passed"] is None and record.observe == []


def test_gate_that_modifies_the_candidate_is_recorded_failed(project):
    _configure(project, gates=['[ "$AUTOHELIX_ITERATION" = 0 ] || echo x >> hello.py'])
    _, record = _run(project, 1)
    assert not record.accepted and record.reason.startswith("checks modified candidate files: ")
    assert record.gates[0]["passed"] is False and record.gates[0]["reason"] == record.reason


def test_background_child_of_a_gate_is_killed_before_the_next_command(project, tmp_path):
    late = tmp_path / "late"
    _configure(project, gates=[f"(sleep 1; touch {late}) >/dev/null 2>&1 &",
                               f"sleep 2; test ! -e {late}"])
    records = _run(project, 1)
    assert [g["passed"] for r in records for g in r.gates] == [True, True, True, True]
    assert not late.exists()


def test_run_command_kills_background_children_after_a_normal_exit(tmp_path):
    from autohelix.checks import run_command

    late = tmp_path / "late"
    result = run_command(f"(sleep 1; touch {late}) >/dev/null 2>&1 & echo started", tmp_path)
    assert result.passed and result.output == "started\n"
    subprocess.run(["sleep", "2"])
    assert not late.exists()


def test_metrics_file_is_not_passed_to_gates_and_observe(project, log, monkeypatch):
    monkeypatch.setenv("AUTOHELIX_METRICS", "/nowhere")
    _configure(project, gates=[f'echo "gate ${{AUTOHELIX_METRICS-unset}}" >> {log}'],
               observe=[{"name": "bench", "command": f'echo "bench ${{AUTOHELIX_METRICS-unset}}" >> {log}'}],
               stop=[f'[ -f "$AUTOHELIX_METRICS" ] && echo stop-has-metrics >> {log}; exit 1'])
    _run(project, 1)
    assert _lines(log)[:3] == ["gate unset", "bench unset", "stop-has-metrics"]


def test_interrupt_during_a_real_command_removes_its_call_folder(project, log):
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signals reach only the main thread")
    _configure(project, gates=[f'[ "$AUTOHELIX_ITERATION" = 0 ] || {{ echo "$AUTOHELIX_SCRATCH" >> {log}; '
                               'kill -INT $PPID; sleep 5; }'])
    harness = Harness(project, verbose=False, land_mode="manual")
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        harness._capture_baseline()
        with pytest.raises(KeyboardInterrupt):
            harness.run_iteration(1)
    finally:
        signal.signal(signal.SIGINT, previous)
        harness.close()
    [scratch] = _lines(log)
    assert not os.path.exists(scratch) and not os.path.exists(os.path.dirname(scratch))
    record = History(project).load()[-1]
    assert record.iteration == 1 and record.reason == "interrupted"
