# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Parallel exploration: run N AutoHelix workers concurrently on the same goal.

Thin orchestration layer around `autohelix run`. Each worker is a normal
AutoHelix run in its own git worktree (branch `parallel/worker-{i}`), with its
own config, history, and `.autohelix/` state. Coordination is note sharing only:
each worker's notes are mirrored to a flat shared dir, and every worker symlinks
peers' shared dirs into `.autohelix/peer_notes/`.

Workers run fully async: a worker only ever writes its OWN branch and its OWN
shared-notes dir, and only ever reads peers' dirs. No locks needed.

Worktrees and branches persist across runs: like a plain `autohelix run`, a
re-run resumes each worker from its recorded history rather than starting over.
Nothing is deleted automatically — the branches are the deliverable, left for
the user to merge or discard.
"""

from __future__ import annotations

import os
import json
import signal
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from autohelix.config import load_config
from autohelix.costs import summarize_costs
from autohelix.history import History
from autohelix.run_status import read_run_status
from autohelix.runs import RunPaths, select_run
from autohelix.state import ensure_runtime_ignored


@dataclass
class Worker:
    """One parallel worker."""

    index: int
    name: str
    config_file: Path
    worktree: Path
    branch: str
    shared_dir: Path
    budget: int
    error: str | None = field(default=None)
    best_score: float | None = field(default=None)
    best_metric: str | None = field(default=None)
    direction: str = field(default="higher")
    total_cost: float = field(default=0.0)
    run_id: str | None = None
    peers: dict[str, str] = field(default_factory=dict)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _git_checked(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    r = _git(args, cwd)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r


def _resolve_budget(config_file: Path, project_path: Path) -> int:
    config, _ = load_config(project_path, config_file=config_file)
    return config.max_iterations or 100


def _branch_exists(branch: str, repo: Path) -> bool:
    return _git(["rev-parse", "--verify", "--quiet", branch], cwd=repo).returncode == 0


def setup_workers(
    repo: Path,
    config_files: list[Path],
    shared_root: Path,
) -> list[Worker]:
    """Ensure a worktree + branch + shared-notes dir exists for each worker config.

    Worktrees and branches PERSIST across runs. If a worker's worktree already
    exists it is reused as-is, so a re-run resumes from its recorded history
    (exactly like re-running a plain `autohelix run`). Nothing is force-deleted.
    """
    repo = repo.resolve()
    root = repo.parent / f"{repo.name}-parallel"
    root.mkdir(parents=True, exist_ok=True)
    shared_root.mkdir(parents=True, exist_ok=True)

    # Drop registrations for worktrees whose directories were removed by hand,
    # so `worktree add` below won't refuse a path git still thinks is checked out.
    _git(["worktree", "prune"], cwd=repo)

    workers: list[Worker] = []
    for i, cfg in enumerate(config_files, start=1):
        name = f"worker-{i}"
        worktree = root / name
        branch = f"parallel/{name}"
        shared_dir = shared_root / name / "notes"
        shared_dir.mkdir(parents=True, exist_ok=True)

        if not worktree.exists():
            # Re-attach the existing branch if we have one; otherwise create it.
            if _branch_exists(branch, repo):
                _git_checked(["worktree", "add", str(worktree), branch], cwd=repo)
            else:
                _git_checked(["worktree", "add", "-b", branch, str(worktree)], cwd=repo)

        budget = _resolve_budget(cfg, worktree)
        ensure_runtime_ignored(worktree)
        config, _ = load_config(worktree, config_file=cfg)
        paths, _, lock = select_run(worktree, cfg, name=config.name)
        try:
            current = paths.root / "current.tmp"
            current.write_text(paths.run_id + "\n")
            current.replace(paths.root / "current")
        finally:
            lock.close()
        workers.append(Worker(
            index=i, name=name, config_file=cfg, worktree=worktree, branch=branch,
            shared_dir=shared_dir, budget=budget, run_id=paths.run_id,
        ))
    for worker in workers:
        worker.peers = {peer.name: peer.run_id for peer in workers if peer is not worker}
    return workers


def link_peer_notes(workers: list[Worker]) -> None:
    """Symlink each peer's flat shared dir into a worker's peer_notes/."""
    for w in workers:
        ext = w.worktree / ".autohelix" / "peer_notes"
        ext.mkdir(parents=True, exist_ok=True)
        for peer in workers:
            if peer.index == w.index:
                continue
            link = ext / peer.name
            if link.is_symlink() or link.exists():
                if link.is_symlink() or link.is_file():
                    link.unlink()
                else:
                    shutil.rmtree(link)
            link.symlink_to(peer.shared_dir, target_is_directory=True)


def sync_own_notes(worker: Worker) -> None:
    """Mirror a worker's own notes into its flat shared dir."""
    src = RunPaths(worker.worktree).notes
    if not src.exists():
        return
    for item in src.iterdir():
        dst = worker.shared_dir / item.name
        if item.is_file():
            shutil.copy2(item, dst)
        elif item.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(item, dst)


_stop_event = threading.Event()
_procs_lock = threading.Lock()
_procs: list[subprocess.Popen] = []


def _kill_tree(pid: int) -> None:
    """Kill a process and all its descendants."""
    try:
        children = subprocess.run(
            ["pgrep", "-P", str(pid)], capture_output=True, text=True
        ).stdout.split()
        for child_pid in children:
            _kill_tree(int(child_pid))
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError, ValueError):
        pass


def _run_segment(worker: Worker, upto: int, verbose: bool, run_args: list[str] | None = None) -> None:
    """Run `autohelix run -n <upto>` in the worker's worktree."""
    if _stop_event.is_set():
        return
    env = os.environ.copy()
    env["AUTOHELIX_PEER_RUNS"] = json.dumps(worker.peers)
    cmd = ["autohelix", "run", "-n", str(upto), "--config", str(worker.config_file)]
    if verbose:
        cmd.append("--verbose")
    if run_args:
        cmd.extend(run_args)
    if worker.run_id:
        cmd.extend(["--run", worker.run_id])
    cmd.extend(["--land", "auto"])
    proc = subprocess.Popen(cmd, cwd=worker.worktree, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    with _procs_lock:
        _procs.append(proc)
    _, stderr = proc.communicate()
    with _procs_lock:
        _procs.remove(proc)
    if proc.returncode:
        raise RuntimeError(f"Worker exited {proc.returncode}: {(stderr or '').strip()[-4000:]}")


def _write_worker_report(worker: Worker) -> None:
    """Write a worker's run summary (`autohelix report`) in its worktree."""
    config, _ = load_config(worker.worktree, config_file=worker.config_file)
    if _stop_event.is_set() or not config.report:
        return
    proc = subprocess.Popen(["autohelix", "report", "--config", str(worker.config_file)],
                            cwd=worker.worktree, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    with _procs_lock:
        _procs.append(proc)
    _, stderr = proc.communicate()
    with _procs_lock:
        _procs.remove(proc)
    if proc.returncode:
        raise RuntimeError(f"Worker report exited {proc.returncode}: {(stderr or '').strip()[-4000:]}")


def run_worker(worker: Worker, sync_every: int, verbose: bool,
               run_args: list[str] | None = None) -> None:
    """Drive a worker to its budget in chunks of sync_every, syncing notes between.

    Resumes from the worker's recorded history: iterations already completed in a
    prior run are not re-run.
    """
    try:
        done = History(worker.worktree).get_last_iteration()
        while done < worker.budget and not _stop_event.is_set():
            upto = min(done + sync_every, worker.budget)
            # Only a worker's last segment writes the run summary.
            extra = [] if upto == worker.budget else ["--no-report"]
            _run_segment(worker, upto, verbose, [*(run_args or []), *extra])
            if _stop_event.is_set():
                break
            sync_own_notes(worker)
            done = upto
            reason = (read_run_status(worker.worktree) or {}).get("stop_reason")
            if isinstance(reason, dict) and (
                reason.get("kind") == "condition"
                or (reason.get("kind") == "budget" and reason.get("budget") in {"cost", "time"})
            ):
                # The worker is done: don't start another segment. That segment ran
                # with --no-report unless it was the last, so write the summary now.
                if "--no-report" in extra and "--no-report" not in (run_args or []):
                    _write_worker_report(worker)
                break
    except Exception as e:  # noqa: BLE001
        worker.error = str(e)


def _worker_best(worker: Worker) -> tuple[float, str] | None:
    config, _ = load_config(worker.worktree, config_file=worker.config_file)
    directions = config.metric_directions()
    if not directions:
        return None
    # Rank by the first-declared metric when a config has several. metric_directions()
    # preserves YAML declaration order, so this is "first declared wins" by design —
    # to rank on a different metric, declare it first in the config.
    metric = next(iter(directions))
    best = History(worker.worktree).get_best_metrics(directions)
    entry = best.get(metric)
    if entry is None:
        return None
    return entry[0], metric


def run_parallel(
    repo: Path,
    config_files: list[Path],
    sync_every: int = 1,
    verbose: bool = False,
    run_args: list[str] | None = None,
) -> list[Worker]:
    """Set up workers, run them concurrently to budget, return results."""
    from rich.console import Console
    from rich.live import Live

    from autohelix.parallel.display import ParallelDisplay

    shared_root = repo / ".autohelix" / "parallel"
    clear_shared(shared_root)
    workers = setup_workers(repo, config_files, shared_root)
    link_peer_notes(workers)

    # Resolve directions for the display, using the first-declared metric per
    # worker (see _worker_best: declaration order is the ranking order).
    directions = []
    for w in workers:
        config, _ = load_config(w.worktree, config_file=w.config_file)
        d = config.metric_directions()
        directions.append(next(iter(d.values())) if d else "higher")

    display = ParallelDisplay(
        worker_names=[w.name for w in workers],
        budgets=[w.budget for w in workers],
        directions=directions,
    )

    _stop_event.clear()
    _procs.clear()

    threads = [
        threading.Thread(
            target=run_worker,
            args=(w, sync_every, verbose, run_args),
        )
        for w in workers
    ]
    for t in threads:
        t.start()

    console = Console()
    interrupted = False
    try:
        with Live(display, console=console, refresh_per_second=10, transient=False) as live:
            while any(t.is_alive() for t in threads):
                for i, w in enumerate(workers):
                    display.update_worker(i, w.worktree)
                    if w.error:
                        display.workers[i].error = w.error
                time.sleep(0.25)
            # Final update
            for i, w in enumerate(workers):
                display.update_worker(i, w.worktree)
                if w.error:
                    display.workers[i].error = w.error
    except KeyboardInterrupt:
        interrupted = True
        _stop_event.set()
        with _procs_lock:
            for proc in _procs:
                _kill_tree(proc.pid)
        for t in threads:
            t.join(timeout=5)
        console.print("\n  [yellow]Interrupted — workers stopped.[/yellow]")

    if not interrupted:
        for t in threads:
            t.join()

    for w in workers:
        config, _ = load_config(w.worktree, config_file=w.config_file)
        d = config.metric_directions()
        b = _worker_best(w)
        if b is not None:
            w.best_score, w.best_metric = b
        if d:
            # Direction of the first-declared metric (the ranking metric).
            w.direction = next(iter(d.values()))
        hist = History(w.worktree)
        w.total_cost = summarize_costs(hist.cost_usages).cost_usd or 0

    return workers


def leaderboard(workers: list[Worker]) -> list[Worker]:
    """Return workers sorted best-first. Only ranks workers that share the same metric."""
    scored = [w for w in workers if w.best_score is not None]
    unscored = [w for w in workers if w.best_score is None]
    metrics = {w.best_metric for w in scored}
    if len(metrics) == 1:
        direction = next((w.direction for w in scored), "higher")
        scored.sort(key=lambda w: w.best_score, reverse=(direction != "lower"))
    return scored + unscored


def clear_shared(shared_root: Path) -> bool:
    if shared_root.exists():
        shutil.rmtree(shared_root)
        return True
    return False



def teardown_workers(repo: Path, workers: list[Worker], keep_branches: bool = True) -> None:
    """Remove worktrees. Branches kept by default for diffing."""
    repo = repo.resolve()
    for w in workers:
        if w.worktree.exists():
            _git(["worktree", "remove", str(w.worktree), "--force"], cwd=repo)
        if not keep_branches:
            _git(["branch", "-D", w.branch], cwd=repo)
