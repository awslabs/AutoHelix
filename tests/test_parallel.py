"""Tests for the parallel-exploration wrapper (autohelix.parallel)."""

import json
import subprocess
from pathlib import Path

import pytest

# Tests poke module internals (P._run_segment, P.subprocess), so import the
# implementation module directly rather than the package's public __init__.
from autohelix.parallel import core as P
from autohelix.runs import RunPaths


def test_mock_parallel_real_subprocess(tmp_path):
    """Workers keep stable run ids across segments and land validated commits."""
    repo = _init_repo(tmp_path)
    config = repo / "autohelix.yaml"
    config.write_text(
        "goal: append a note\n"
        "agent:\n"
        "  type: mock\n"
        "  settings:\n"
        "    change_file: sort.py\n"
        "    notes_content: 'worker iteration {iteration}'\n"
        "    cost_usd: 0.1\n"
        "scope:\n"
        "  editable: [sort.py]\n"
        "budget:\n"
        "  iterations: 2\n"
        "report: false\n"
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "mock config"], cwd=repo, check=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    result = subprocess.run(
        ["autohelix", "parallel", "--worker", str(config), "--worker", str(config),
         "--sync-notes-every", "1"],
        cwd=repo, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    ids = []
    for index in (1, 2):
        worker = repo.parent / "proj-parallel" / f"worker-{index}"
        run_id = (worker / ".autohelix" / "current").read_text().strip()
        ids.append(run_id)
        state = worker / ".autohelix" / "runs" / run_id
        records = [json.loads(line) for line in (state / "history.jsonl").read_text().splitlines()]
        assert [record["iteration"] for record in records] == [0, 1, 2]
        assert all(record["accepted"] for record in records)
        tip = subprocess.check_output(
            ["git", "rev-parse", f"refs/autohelix/{run_id}"], cwd=repo, text=True,
        ).strip()
        branch_tip = subprocess.check_output(
            ["git", "rev-parse", f"parallel/worker-{index}"], cwd=repo, text=True,
        ).strip()
        assert tip == branch_tip != base
        assert (state / "notes" / "notes.md").exists()
    assert len(set(ids)) == 2
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == base


def _init_repo(tmp_path: Path) -> Path:
    """A minimal git repo with a sorting-style autohelix.yaml."""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "sort.py").write_text("def sort_list(x):\n    return sorted(x)\n")
    (repo / "autohelix.yaml").write_text(
        "goal: make it fast\n"
        "observables:\n"
        "  - command: python benchmark.py\n"
        "    values:\n"
        "      speed: higher\n"
        "scope:\n"
        "  editable: [sort.py]\n"
        "budget:\n"
        "  iterations: 6\n"
    )
    for c in (["init", "-q"], ["config", "user.email", "a@b.c"],
              ["config", "user.name", "t"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", *c], cwd=repo, capture_output=True)
    return repo


def _seed_history(worktree: Path, scores: list[float], baseline: float = 1.0) -> None:
    """Write a history.jsonl with one accepted iteration per score."""
    ah = RunPaths(worktree).run_dir
    ah.mkdir(parents=True, exist_ok=True)
    rows = [{"iteration": 0, "accepted": True, "metrics": {"speed": baseline}, "usage": {}}]
    for i, s in enumerate(scores, start=1):
        rows.append({"iteration": i, "accepted": True, "metrics": {"speed": s}, "usage": {}})
    (ah / "history.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_setup_link_sync_teardown(tmp_path):
    repo = _init_repo(tmp_path)
    cfg = repo / "autohelix.yaml"
    workers = P.setup_workers(repo, [cfg, cfg, cfg], tmp_path / "shared")

    assert [w.name for w in workers] == ["worker-1", "worker-2", "worker-3"]
    assert len({w.run_id for w in workers}) == 3
    assert all(w.worktree.exists() for w in workers)
    assert all(w.budget == 6 for w in workers)

    P.link_peer_notes(workers)
    ext = workers[0].worktree / ".autohelix" / "peer_notes"
    assert sorted(p.name for p in ext.iterdir()) == ["worker-2", "worker-3"]
    assert all((ext / n).is_symlink() for n in ["worker-2", "worker-3"])

    # worker-2 writes a note -> sync -> worker-1 reads it through the symlink
    note = RunPaths(workers[1].worktree).notes / "iter-1.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("tried quicksort")
    P.sync_own_notes(workers[1])
    seen = workers[0].worktree / ".autohelix" / "peer_notes" / "worker-2" / "iter-1.md"
    assert seen.exists() and "quicksort" in seen.read_text()

    P.teardown_workers(repo, workers, keep_branches=True)
    assert all(not w.worktree.exists() for w in workers)
    branches = subprocess.run(["git", "branch", "--list", "parallel/*"],
                              cwd=repo, capture_output=True, text=True).stdout
    assert "parallel/worker-1" in branches


def test_setup_workers_reuses_existing_worktree(tmp_path):
    """Re-running setup reuses the existing worktree/branch instead of clobbering."""
    repo = _init_repo(tmp_path)
    cfg = repo / "autohelix.yaml"
    workers = P.setup_workers(repo, [cfg], tmp_path / "shared")
    wt = workers[0].worktree

    # A file written into the worktree survives a second setup (nothing deleted).
    marker = RunPaths(wt).history
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("prior-run-state")

    workers2 = P.setup_workers(repo, [cfg], tmp_path / "shared")
    assert workers2[0].worktree == wt
    assert marker.read_text() == "prior-run-state"


def test_setup_workers_reattaches_branch_after_manual_worktree_removal(tmp_path):
    """If the worktree dir is gone but the branch remains, setup re-attaches it."""
    repo = _init_repo(tmp_path)
    cfg = repo / "autohelix.yaml"
    workers = P.setup_workers(repo, [cfg], tmp_path / "shared")
    P.teardown_workers(repo, workers, keep_branches=True)  # dir gone, branch kept
    assert not workers[0].worktree.exists()

    workers2 = P.setup_workers(repo, [cfg], tmp_path / "shared")
    assert workers2[0].worktree.exists()
    branches = subprocess.run(["git", "branch", "--list", "parallel/*"],
                              cwd=repo, capture_output=True, text=True).stdout
    assert "parallel/worker-1" in branches


def test_run_worker_resumes_from_history(tmp_path, monkeypatch):
    """run_worker skips iterations already recorded in history."""
    repo = _init_repo(tmp_path)
    cfg = repo / "autohelix.yaml"
    workers = P.setup_workers(repo, [cfg], tmp_path / "shared")
    w = workers[0]
    _seed_history(w.worktree, [5.0, 6.0])  # iterations 1 and 2 already done

    segments = []
    monkeypatch.setattr(P, "_run_segment", lambda w, upto, v, r=None: segments.append(upto))

    P.run_worker(w, sync_every=2, verbose=False)
    # budget is 6; already done through 2, so resume runs 4 then 6.
    assert segments == [4, 6]


def test_run_worker_syncs_every_n(tmp_path, monkeypatch):
    """run_worker runs in chunks of sync_every, syncing notes between."""
    repo = _init_repo(tmp_path)
    cfg = repo / "autohelix.yaml"
    workers = P.setup_workers(repo, [cfg], tmp_path / "shared")
    w = workers[0]

    segments = []

    def fake_segment(worker, upto, verbose, run_args=None):
        segments.append(upto)

    monkeypatch.setattr(P, "_run_segment", fake_segment)

    P.run_worker(w, sync_every=2, verbose=False)
    assert segments == [2, 4, 6]
    assert w.error is None


def test_only_last_segment_writes_report(tmp_path, monkeypatch):
    """Segment iteration limits keep the worker going; only the last writes a summary."""
    repo = _init_repo(tmp_path)
    workers = P.setup_workers(repo, [repo / "autohelix.yaml"], tmp_path / "shared")

    calls = []
    separate_reports = []
    monkeypatch.setattr(P, "_run_segment", lambda w, upto, v, r=None: calls.append((upto, r)))
    monkeypatch.setattr(P, "read_run_status",
                        lambda *a: {"stop_reason": {"kind": "budget", "budget": "iterations"}})
    monkeypatch.setattr(P, "_write_worker_report", lambda w: separate_reports.append(w.name))

    P.run_worker(workers[0], sync_every=2, verbose=False, run_args=["--verbose"])
    assert calls == [(2, ["--verbose", "--no-report"]), (4, ["--verbose", "--no-report"]), (6, ["--verbose"])]
    assert separate_reports == []


def test_run_worker_sync_every_1(tmp_path, monkeypatch):
    """sync_every=1 syncs after every iteration."""
    repo = _init_repo(tmp_path)
    cfg = repo / "autohelix.yaml"
    workers = P.setup_workers(repo, [cfg], tmp_path / "shared")

    segments = []
    monkeypatch.setattr(P, "_run_segment", lambda w, upto, v, r=None: segments.append(upto))

    P.run_worker(workers[0], sync_every=1, verbose=False)
    assert segments == [1, 2, 3, 4, 5, 6]


def test_run_args_forwarded_to_subprocess(tmp_path, monkeypatch):
    """Extra run_args appear in the subprocess command."""
    repo = _init_repo(tmp_path)
    cfg = repo / "autohelix.yaml"
    workers = P.setup_workers(repo, [cfg], tmp_path / "shared")

    captured = {}

    class FakePopen:
        def __init__(self, cmd, cwd, env, stdout, stderr, start_new_session, **kwargs):
            captured["cmd"] = cmd
            captured["peers"] = env.get("AUTOHELIX_PEER_RUNS")
            self.pid = 99999

        returncode = 0
        def communicate(self):
            return None, ""
        def wait(self):
            pass

    monkeypatch.setattr(P.subprocess, "Popen", FakePopen)
    P.run_worker(workers[0], sync_every=6, verbose=False, run_args=["--timeout", "600"])

    assert captured["cmd"][:5] == ["autohelix", "resume", workers[0].run_id, "--until", "6"]
    assert "--timeout" in captured["cmd"] and "600" in captured["cmd"]
    assert json.loads(captured["peers"]) == {}


def test_cli_rejects_wrapper_owned_passthrough(tmp_path):
    from click.testing import CliRunner
    from autohelix.cli import main

    repo = _init_repo(tmp_path)
    cfg = str(repo / "autohelix.yaml")
    runner = CliRunner()
    for bad in (["-n", "3"], ["--iterations", "5"]):
        result = runner.invoke(main, ["parallel", "--worker", cfg, "-p", str(repo), *bad])
        assert result.exit_code != 0
        assert "managed by" in result.output or "cannot be forwarded" in result.output


def test_clear_shared(tmp_path):
    shared = tmp_path / "shared"
    (shared / "worker-1").mkdir(parents=True)
    (shared / "worker-1" / "iter-1.md").write_text("stale")
    assert P.clear_shared(shared) is True
    assert not shared.exists()
    assert P.clear_shared(shared) is False


def test_parallel_display_update(tmp_path):
    """ParallelDisplay reads history and updates worker snapshots."""
    from autohelix.parallel.display import ParallelDisplay

    display = ParallelDisplay(
        worker_names=["worker-1", "worker-2"],
        budgets=[4, 4],
        directions=["higher", "higher"],
    )

    # Simulate a worktree with history
    wt = tmp_path / "wt1"
    wt.mkdir()
    _seed_history(wt, [5.0, 10.0], baseline=1.0)

    display.update_worker(0, wt)
    w = display.workers[0]
    assert w.iteration == 2
    assert w.best_score == 10.0
    assert w.baseline == 1.0


def test_leaderboard_sorts_by_score(tmp_path):
    repo = _init_repo(tmp_path)
    cfg = repo / "autohelix.yaml"
    workers = P.setup_workers(repo, [cfg, cfg, cfg], tmp_path / "shared")

    workers[0].best_score, workers[0].best_metric = 5.0, "speed"
    workers[1].best_score, workers[1].best_metric = 9.0, "speed"
    workers[2].best_score, workers[2].best_metric = 3.0, "speed"

    lb = P.leaderboard(workers)
    assert [w.best_score for w in lb] == [9.0, 5.0, 3.0]

    P.teardown_workers(repo, workers)
