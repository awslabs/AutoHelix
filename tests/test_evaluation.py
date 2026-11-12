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
from autohelix.config import ObserveEntry
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
    # Its output is kept (stdout and stderr).
    stdout = (_observations(project) / "iter-1" / "flaky" / "stdout.txt").read_text()
    assert "##autohelix[score=7]" in stdout and "oops" in stdout
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
         "capture": ["a/log.txt", "b/log.txt", "link.txt", "../secret.txt", "missing.txt", "out", "stdout.txt"]},
        {"name": "lint", "command": "echo lint; mkdir -p a; echo L > a/log.txt", "capture": ["a/log.txt"]},
    ])
    _, record = _run(project, 1)
    assert record.accepted
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
        assert not (folder.parent / "secret.txt").exists()
        assert (folder / "lint" / "a" / "log.txt").read_text() == "L\n"
        assert (folder / "lint" / "stdout.txt").read_text() == "lint\n"
        assert not list(folder.glob("stdout-*"))


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
