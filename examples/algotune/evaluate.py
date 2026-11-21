#!/usr/bin/env python3
"""Score an AlgoTune solver with AlgoTune's protocol, measured by a parent that never runs solver code.

    python -I evaluate.py --task svm --solver DIR --data DIR [--split dev|test] [--json OUT]

AlgoTune's protocol (scripts/evaluate_results.py and AlgoTuner/utils at the pinned commit):
- per instance, N runs (dev 2, test 10), each a fresh process that imports the solver (not
  timed), warms up on the previous instance, then solves this one. Each call makes a new
  Solver() (get_fresh_solve_callable, solver_loader.py:540); the timed call is that, the
  solve, and materializing the output (deep_materialize_fast);
- the instance's time is its fastest run, for the reference and the solver alike; its
  speedup is reference / solver; the task's is the mean over instances, only if every
  instance is valid;
- thread variables are 1 for both sides (the solver may raise them at run time), 14 GB of
  address space per process; a run times out after 2 x 10x the reference for the warm-up
  and the timed call together (5 s to 300 s, main.py:181).

What this evaluator does differently, so the score can't be forged from solver.py:
- the parent never imports the solver, and decodes its outputs with an allowlist unpickler;
- the parent times each call on its own clock. It delivers the problem first, waits (up to
  2 s) for the run's threads to go idle, and checks from /proc that they used no more CPU
  receiving the problem than a trusted echo process does (so nothing was solved early);
  then it starts the clock with a one-byte signal and stops it when the output is back;
- what AlgoTune doesn't time (the start signal, pickling and sending the output) is
  subtracted: the round trip of the echo, a trusted child that goes through the same steps
  and sends a copy of the same output. Outputs are pickled straight into the
  socket and received into memory faulted in before the clock, the echo's copy is written
  just before its clock starts, and for the solver's output the echo first waits as long as
  the solve took, so it pays what the solver pays. A net time within the echo's jitter gets
  more echo runs; if it stays below what they resolve, its speedup is capped at that
  resolution and flagged (`speedup_capped`);
- every timed output is validated with the task's is_solution, in the parent, on its own
  copy of the problem;
- with --run-as (the evaluator as root, in its own container), solver code runs as an
  unprivileged user that can't read the instances or write outside its own empty working
  folder, and after each run none of that user's processes, IPC objects or files remain.
  The evaluator's own work folder is under WORK_BASE (root's, 0711), outside the shared
  folders swept for that user's files.

The reference's side of an instance (its fastest round trip, the echo runs on its output, the
CPU limit for receiving the problem derived from them) depends only on the reference, the
instance and the machine, so with --run-as it is kept in CACHE_DIR (root's, 0700; files 0600)
and later evaluations in the same container reuse it, as AlgoTune's agent loop keeps its
baseline times for a run. An entry is per instance and per warm-up instance, under a key of the
task, split, runs, data file, this file, AlgoTune's commit and task code, the Python and numpy
versions and the CPU affinity set; any change to one is a new key. The solver's side (its runs,
validation and the echo runs on its output) is measured every time. --no-reference-cache
bypasses the cache (neither read nor written); the result records, per instance, whether the
reference came from it and when it was measured.

Prints `##autohelix[speedup=...]` when every instance is valid; exits 1 otherwise.
The data folder holds `<task>/<split>.jsonl` (written by make_data.py, from AlgoTune's own
generator). AlgoTune itself comes from --algotune or $ALGOTUNE_ROOT. Linux only (/proc).
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import gc
import hashlib
import io
import json
import mmap
import multiprocessing
import os
import pickle
import resource
import select
import shutil
import signal
import socket
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEV_RUNS = 2  # AlgoTune's dev_runs (timing_config.py)
TEST_RUNS = 10  # AlgoTune's eval_runs
TIMEOUT_FACTOR = 10.0  # a run may take 10x the reference (solver_executor.py)
TIMEOUT_MIN_S, TIMEOUT_MAX_S = 5.0, 300.0
INIT_TIMEOUT_S = 120.0  # importing the solver: untimed, but bounded
REFERENCE_TIMEOUT_S = 600.0
BUILD_TIMEOUT_S = 1800.0
MEMORY_BYTES = 14 * 1024**3  # AlgoTune's memory_limit_gb_per_worker
THREAD_VARIABLES = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS")
MAX_MESSAGE = 1 << 31
ECHO_RUNS = 3  # echo runs per measured output (fastest kept)
ECHO_RUNS_MAX = 10  # in all, when the net time is within the echo's jitter or below its overhead
RESOLUTION_NS = 1_000  # no net time below this is resolved, whatever the echo's jitter
IDLE_WAIT_S = 2.0  # before delivering the problem, wait this long at most for idle threads
PREPARE_FACTOR, PREPARE_SLACK_NS = 2.0, 10_000_000  # CPU allowed while receiving the problem:
# twice the trusted echo's highest reading plus 10 ms. It catches only gross early work (the
# echo's own readings vary from run to run); the audit covers the rest.
STREAM_BATCH = 1 << 16  # the child sends the pickle in writes of this size
HEADROOM = 1 << 20  # the receive buffer is sized to the largest output seen + this
PAGE = 4096
SHARED_DIRS = ("/tmp", "/var/tmp", "/dev/shm", "/run/lock", "/dev/mqueue")
WORK_BASE = Path("/var/lib/algotune-eval")  # with --run-as: the work folders, not in SHARED_DIRS
# The reference cache: under WORK_BASE, not named like the work folders (algotune-gate removes
# WORK_BASE/algotune-eval-* after every gate), one folder per key.
CACHE_DIR = WORK_BASE / "reference-cache"
CACHE_VERSION = 1
CACHE_ENTRY_MAX = 1 << 20  # bytes; a larger entry file is ignored
IGNORED = shutil.ignore_patterns(".git", ".autohelix-iteration", "__pycache__", "*.pyc")


# ---------------------------------------------------------------------------- evaluation

def evaluate(task: str, solver: Path, data: Path, split: str, runs: int,
             limit: int | None, progress, run_as: tuple[int, int] | None = None,
             reference_cache: bool = True) -> dict:
    for name in THREAD_VARIABLES:
        os.environ[name] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER: orphans come to us
    if run_as is not None:
        _require_private(data / task)
    records = _instances(data, task, split, limit)
    reference_task = _task(task)
    work = Path(tempfile.mkdtemp(prefix="algotune-eval-", dir=_work_base(run_as)))
    work.chmod(0o711)  # the solver user may enter (its scratch is inside) but not list
    try:
        cache = None
        if run_as is None:
            cache_record = {"used": False, "why": "no --run-as"}
        elif not reference_cache:
            cache_record = {"used": False, "why": "--no-reference-cache"}
        else:
            cache = ReferenceCache(CACHE_DIR, _cache_key(task, data, split, runs))
            cache_record = {"used": True, "key": cache.digest, "folder": str(cache.folder)}
        build = work / "solver"
        if (error := _build(solver, build, run_as)) is not None:
            return {"task": task, "split": split, "valid": False, "error": error}
        if not (build / "solver.py").is_file():
            return {"task": task, "split": split, "valid": False, "error": "no solver.py"}
        _read_only(build)
        runner = Runner(task, str(build), work, run_as)
        # Each instance warms up on the previous one (the first on the last). Only those two
        # problems are pickled at a time: all at once would hold every problem in memory
        # (aes_gcm_encryption's 100 come to ~29 GB).
        def problem(i: int) -> bytes:
            return pickle.dumps(_plain(records[i]["problem"]), protocol=5)

        results = []
        warm = problem(len(records) - 1)
        for i, record in enumerate(records):
            timed = problem(i)
            entry = _score(runner, reference_task, record["seed"], warm, timed, runs, cache)
            warm = timed
            results.append(entry)
            progress(i + 1, len(records), entry)
            if not entry["valid"] and split == "dev":
                break  # one invalid dev instance already fails the gate
        valid = len(results) == len(records) and all(r["valid"] for r in results)
        speedups = [r["speedup"] for r in results if r["valid"]]
        if cache is not None:
            hits = sum(r["reference_cached"] for r in results)
            cache_record.update(hits=hits, misses=len(results) - hits)
        return {
            "task": task, "split": split, "runs": runs, "instances": len(records),
            "seeds": [r["seed"] for r in records], "valid": valid,
            "speedup": statistics.mean(speedups) if valid else None,
            "median_instance_speedup": statistics.median(speedups) if speedups else None,
            "reference_cache": cache_record,
            "results": results,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _score(runner: Runner, task, seed: int, warm: bytes, timed: bytes, runs: int,
           cache: ReferenceCache | None = None) -> dict:
    """One instance: `runs` runs of the reference, then of the solver, warming up on `warm`.

    Net times subtract the echo's overhead, its fastest run carrying the same output. For
    the solver's output the echo first spins for the solver's net time (estimated with
    plain echo runs), so the parent waits, and wakes, as it did for the solver. The echo's
    jitter (median - fastest run) is what that subtraction resolves: a net within it, or
    below the overhead, gets ECHO_RUNS_MAX echo runs in all; a net still below it (or at
    most 0) counts as that resolution, so the speedup is capped at what can be measured.

    The reference's side (_measure_reference) comes from `cache` when it has this instance
    with this warm-up instance, and goes into it when measured."""
    echo_log = []

    def echo(output: bytes, count: int, spin_ns: int = 0) -> list[dict]:
        found = [runner.run("echo", warm, timed, REFERENCE_TIMEOUT_S, output, spin_ns)
                 for _ in range(count)]
        if errors := [e["error"] for e in found if "error" in e]:
            raise SystemExit(f"echo run failed on seed {seed}: {errors[0]}")
        echo_log.append({"spin_ms": spin_ns / 1e6,
                         "overhead_ms": [(e["ns"] - spin_ns) / 1e6 for e in found]})
        return found

    measured = cache.get(seed, warm, timed) if cache is not None else None
    cached = measured is not None
    if cached:
        echo_log.append({**measured["echo"], "cached": True})
        # The receive buffer is sized as if the reference's output had come through it, so
        # the solver's first run doesn't pay for growing it.
        runner.largest = max(runner.largest, measured["output_size"])
    else:
        measured = _measure_reference(runner, seed, warm, timed, runs, echo)
        if cache is not None:
            cache.put(seed, warm, timed, measured)
    reference_ns = measured["reference_ns"]
    reference_overhead = measured["reference_overhead_ns"]
    reference_net = reference_ns - reference_overhead
    prepare_limit = measured["prepare_limit_ns"]

    timeout = min(max(2 * TIMEOUT_FACTOR * reference_ns / 1e9, TIMEOUT_MIN_S), TIMEOUT_MAX_S)
    attempts = [runner.run("solver", warm, timed, timeout) for _ in range(runs)]
    for attempt in attempts:
        if attempt.get("prepare_ns", 0) > prepare_limit:
            attempt["error"] = (
                f"used {attempt['prepare_ns'] / 1e6:.1f} ms of CPU while receiving the "
                f"problem, before the clock started (limit {prepare_limit / 1e6:.1f} ms)")
    entry = {"seed": seed, "reference_ms": reference_net / 1e6,
             "reference_raw_ms": reference_ns / 1e6,
             "reference_overhead_ms": reference_overhead / 1e6,
             "reference_cached": cached, "reference_measured_at": measured["measured_at"],
             "valid": False,
             "echo_runs_ms": echo_log,  # the reference's output, then the solver's
             "prepare_limit_ms": prepare_limit / 1e6,
             "solver_prepare_ms": max((a["prepare_ns"] / 1e6 for a in attempts
                                       if "prepare_ns" in a), default=None)}
    if failed := [a for a in attempts if "error" in a]:
        entry.update(error=failed[0]["error"],
                     timeout=any(a["error"].startswith("timeout") for a in failed))
        return entry

    verdicts = {}  # each distinct output validated once
    for attempt in attempts:
        if attempt["output"] not in verdicts:
            verdicts[attempt["output"]] = _check(task, runner.task, timed, attempt["output"])
    fastest = min(attempts, key=lambda a: a["ns"])
    entry.update(solver_raw_ms=fastest["ns"] / 1e6, raw_speedup=reference_ns / fastest["ns"],
                 distinct_outputs=len(verdicts))
    if problems := [v for v in verdicts.values() if v is not None]:
        entry.update(error=problems[0], solver_ms=fastest["ns"] / 1e6, solver_overhead_ms=0.0,
                     speedup=reference_net / fastest["ns"])
        return entry

    output = fastest["output"]
    spin = max(fastest["ns"] - min(e["ns"] for e in echo(output, min(runs, ECHO_RUNS))), 0)
    paired = [e["ns"] - spin for e in echo(output, min(runs, ECHO_RUNS), spin)]
    overhead, jitter = _resolution(paired)
    if fastest["ns"] - overhead <= max(jitter, overhead):
        paired += [e["ns"] - spin for e in echo(output, ECHO_RUNS_MAX - len(paired), spin)]
        overhead, jitter = _resolution(paired)
    net = fastest["ns"] - overhead
    floor = max(jitter, RESOLUTION_NS)
    entry.update(solver_ms=net / 1e6, solver_overhead_ms=overhead / 1e6,
                 solver_overhead_jitter_ms=jitter / 1e6, echo_runs=len(paired),
                 echo_spin_ms=spin / 1e6, speedup=reference_net / max(net, floor), valid=True)
    if net < floor:
        entry.update(speedup_capped=True, capped_at_ms=floor / 1e6, note=(
            f"net time {net / 1e6:.4f} ms (round trip {fastest['ns'] / 1e6:.4f} ms - "
            f"overhead {overhead / 1e6:.4f} ms) is below what the echo resolves "
            f"({floor / 1e6:.4f} ms): speedup capped at reference / {floor / 1e6:.4f} ms"))
    return entry


def _measure_reference(runner: Runner, seed: int, warm: bytes, timed: bytes, runs: int,
                       echo) -> dict:
    """The reference's side of an instance: `runs` reference runs (the fastest counts), then
    echo runs on its output (the fastest is its overhead; their receiving CPU sets the limit
    for the solver's). Plain JSON data, as kept in the cache: the output's size and digest,
    not the output itself (only the echo runs use it, and they are part of the entry)."""
    reference = [runner.run("reference", warm, timed, REFERENCE_TIMEOUT_S) for _ in range(runs)]
    if errors := [r["error"] for r in reference if "error" in r]:
        raise SystemExit(f"reference failed on seed {seed}: {errors[0]}")
    fastest = min(reference, key=lambda r: r["ns"])
    reference_ns = fastest["ns"]
    plain = echo(fastest["output"], min(runs, ECHO_RUNS))
    reference_overhead = min(e["ns"] for e in plain)
    if reference_ns - reference_overhead <= 0:
        raise SystemExit(
            f"reference on seed {seed}: round trip {reference_ns / 1e6:.3f} ms "
            f"is no longer than the echo's {reference_overhead / 1e6:.3f} ms")
    return {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "reference_ns": reference_ns, "reference_runs_ns": [r["ns"] for r in reference],
        "reference_overhead_ns": reference_overhead,
        "echo_prepare_ns": [e["prepare_ns"] for e in plain],
        "prepare_limit_ns": PREPARE_FACTOR * max(e["prepare_ns"] for e in plain)
        + PREPARE_SLACK_NS,
        "echo": {"spin_ms": 0.0, "overhead_ms": [e["ns"] / 1e6 for e in plain]},
        "output_size": len(fastest["output"]),
        "output_sha256": hashlib.sha256(fastest["output"]).hexdigest(),
    }


def _resolution(overheads: list[int]) -> tuple[int, int]:
    """The overhead (fastest echo run) and its jitter (median - fastest)."""
    fastest = min(overheads)
    return fastest, int(statistics.median(overheads)) - fastest


def _check(task, name: str, timed: bytes, output: bytes) -> str | None:
    """None if the output decodes and passes is_solution on a fresh copy of the problem."""
    try:
        solution = _decode(output, name)
    except Exception as error:  # noqa: BLE001
        return f"output rejected: {error}"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            ok = task.is_solution(pickle.loads(timed), solution)
    except Exception as error:  # noqa: BLE001
        return f"is_solution raised {type(error).__name__}: {error}"
    return None if bool(ok) else "is_solution returned False"


# ---------------------------------------------------------------------------- one run

class Runner:
    """Starts one child per run from a forkserver that preloaded only trusted modules."""

    def __init__(self, task: str, solver_dir: str, work: Path, run_as: tuple[int, int] | None):
        self.task, self.solver_dir, self.work, self.run_as = task, solver_dir, work, run_as
        self.context = multiprocessing.get_context("forkserver")
        self.context.set_forkserver_preload(
            ["numpy", "AlgoTuneTasks.factory", f"AlgoTuneTasks.{task}.{task}"])
        self.ipc_before = _sysv_ipc()
        self.buffer = _faulted(1 << 20)  # receives outputs (see _receive)
        self.largest = 0

    def run(self, kind: str, warm: bytes, timed: bytes, call_timeout: float,
            payload: bytes | None = None, spin_ns: int = 0) -> dict:
        """One run of the solver, the reference or the echo (which sends `payload`): {"ns":
        the timed call's round trip, "output": bytes, "prepare_ns": CPU used receiving the
        problem} or {"error": text}. The parent's side of _child's messages."""
        scratch = tempfile.mkdtemp(prefix="run-", dir=self.work)
        if self.run_as is not None:
            os.chown(scratch, *self.run_as)
        parent, child = self.context.Pipe()
        process = self.context.Process(target=_child, args=(
            child, kind, self.task, self.solver_dir, scratch, self.run_as, payload, spin_ns))
        process.start()
        child.close()

        def threads() -> dict[tuple[int, int], int]:
            if self.run_as is not None:
                return _thread_cpu(p for p in _pids() if _uid_of(p) == self.run_as[0])
            return _thread_cpu(_tree(process.pid))

        try:
            if error := _expect(parent, b"L", INIT_TIMEOUT_S, "loading"):
                return {"error": error}
            started = time.monotonic()
            parent.send_bytes(warm)
            if error := _expect(parent, b"R", call_timeout, "warm-up"):
                return {"error": error}
            _wait_idle(threads)  # e.g. BLAS threads still spinning after the warm-up
            before = threads()
            if parent.poll(0):
                return {"error": "the solver process sent a message before the timed problem"}
            parent.send_bytes(timed)
            left = max(call_timeout - (time.monotonic() - started), 0.0)
            if error := _expect(parent, b"P", left, "receiving the problem"):
                return {"error": error}
            after = threads()
            if set(after) != set(before):
                return {"error": "threads or processes started or ended while the problem "
                                 "was being received"}
            prepare_ns = sum(after.values()) - sum(before.values())
            if kind == "echo":
                parent.send_bytes(b"W")
                if error := _expect(parent, b"Q", REFERENCE_TIMEOUT_S, "copying"):
                    return {"error": error}
            if parent.poll(0):
                return {"error": "the solver process sent a message before the start signal"}
            if len(self.buffer) < self.largest + HEADROOM:  # before the clock
                self.buffer = _faulted(2 * self.largest + HEADROOM)
            begin = time.perf_counter_ns()
            parent.send_bytes(b"G")
            size = self._receive(parent.fileno(), started + call_timeout)
            end = time.perf_counter_ns()
            if isinstance(size, str):
                return {"error": size}
            output = bytes(memoryview(self.buffer)[:size])
            self.largest = max(self.largest, size)
            if output[:1] == b"E":
                return {"error": f"timed call: {output[1:2001].decode(errors='replace')}"}
            if not output:
                return {"error": "timed call: the solver process sent no output"}
            return {"ns": end - begin, "output": output, "prepare_ns": prepare_ns}
        except (EOFError, OSError) as error:
            return {"error": f"child exited ({error.__class__.__name__})"}
        finally:
            parent.close()
            process.join(2)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            if process.is_alive():
                process.kill()
            process.join()
            _reap_strays()
            if self.run_as is not None:
                _end_user_session(self.run_as[0])
            _remove_new_ipc(self.ipc_before)
            shutil.rmtree(scratch, ignore_errors=True)

    def _receive(self, fd: int, deadline: float) -> int | str:
        """Read the output stream, up to the child's shutdown, into self.buffer (faulted in
        before the clock, so receiving costs the same whichever process sent it); its length,
        or an error text. A larger stream grows the buffer here: that run pays for it, later
        runs get a buffer that fits."""
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        got, view = 0, memoryview(self.buffer)
        try:
            while True:
                if got == len(view):
                    if got > MAX_MESSAGE:
                        return "timed call: the output is larger than 2 GiB"
                    view.release()
                    grown = _faulted(2 * len(self.buffer))
                    grown[:got] = self.buffer[:got]
                    self.buffer, view = grown, memoryview(grown)
                left = deadline - time.monotonic()
                if left <= 0 or not poller.poll(left * 1000):
                    return "timeout"
                count = os.readv(fd, [view[got:]])
                if count == 0:
                    return got
                got += count
        finally:
            view.release()


def _faulted(size: int) -> bytearray:
    """A buffer whose pages are all mapped in (written once), so filling it doesn't fault."""
    buffer = bytearray(size)
    buffer[::PAGE] = bytes(len(range(0, size, PAGE)))
    return buffer


def _expect(connection, tag: bytes, timeout: float, phase: str) -> str | None:
    """None when the next message is `tag`, else an error text."""
    if not connection.poll(timeout):
        return f"timeout during {phase}"
    message = connection.recv_bytes(MAX_MESSAGE)
    if message == tag:
        return None
    if message[:1] == b"E":
        return f"{phase}: {message[1:2001].decode(errors='replace')}"
    return f"{phase}: unexpected message from the solver process"


# ---------------------------------------------------------------------------- child

def _child(conn, kind: str, task: str, solver_dir: str, scratch: str,
           run_as: tuple[int, int] | None, payload: bytes | None, spin_ns: int) -> None:
    """One run, in a fresh process forked from the forkserver; the only place solver code runs.

    Messages (parent -> child | child -> parent): `L` loaded | warm-up problem | `R` ready |
    timed problem | `P` prepared | (echo only: `W` | `Q`) | `G` go (the parent's clock
    starts) | the output, pickled straight into the socket and ended by shutting down its
    sending side. A failure: `E` + text, as a message before `G`, after it in the stream.

    The pickler and its writer are made before `P`, so after `G` there are only Solver()
    (a new one per call, as in AlgoTune), the solve, _materialize, the pickling, the writes
    (no buffer of the output's size: arrays are written from their own memory) and the
    shutdown. The echo decodes the output at start-up (its memory while receiving the
    problem, which sets the CPU limit for that, stays as it was measured) and again on `W`:
    the copy it sends, freshly written just before the clock, as a solver's output is. On
    `G` it first busy-waits spin_ns, standing in for the solve."""
    started = False
    try:
        os.setsid()
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        os.chdir(scratch)
        os.environ.update(HOME=scratch, TMPDIR=scratch)
        if run_as is not None:  # drop to the unprivileged solver user before any solver code
            os.setgroups([])
            os.setgid(run_as[1])
            os.setuid(run_as[0])
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_MSGQUEUE, (0, 0))
        if kind == "solver":
            sys.path.insert(0, solver_dir)
            import solver as module  # noqa: PLC0415 - the candidate, imported only here

            if hasattr(module, "Solver"):
                def solve(problem):
                    return module.Solver().solve(problem)
            else:
                solve = module.solve
        elif kind == "echo":
            output = _decode(payload, task)

            def solve(problem):
                return None
        else:
            solve = _task(task).solve
        conn.send_bytes(b"L")
        # Both problems' pickles stay referenced, as in AlgoTune's worker: freed, they would
        # hand the solve memory that is already faulted in.
        warm = conn.recv_bytes()
        solve(pickle.loads(warm))
        # AlgoTune collects right before its timed call (clear_solver_caches); without it, a
        # full collection (and its copy-on-write faults on the forkserver's pages) lands in
        # the timed call or not depending on how many objects this module happens to define.
        gc.collect()
        conn.send_bytes(b"R")
        timed = conn.recv_bytes()
        problem = pickle.loads(timed)
        # Return the heap's free pages, so the solve faults in fresh ones, as in AlgoTune's
        # worker (which received both problems before the warm-up).
        ctypes.CDLL(None).malloc_trim(0)
        channel = socket.socket(fileno=conn.fileno())  # the same socket, to shut down
        writer = open(conn.fileno(), "wb", buffering=STREAM_BATCH, closefd=False)
        pickler = pickle.Pickler(writer, protocol=5)
        conn.send_bytes(b"P")
        if kind == "echo":
            if conn.recv_bytes() != b"W":
                raise RuntimeError("expected the write signal")
            output = None  # the start-up copy is freed first
            output = _decode(payload, task)
            conn.send_bytes(b"Q")
        if conn.recv_bytes() != b"G":
            raise RuntimeError("expected the start signal")
        started = True
        if kind == "echo":
            until = time.perf_counter_ns() + spin_ns
            while time.perf_counter_ns() < until:
                pass
        pickler.dump(output if kind == "echo" else _materialize(solve(problem)))
        writer.flush()
        channel.shutdown(socket.SHUT_WR)
    except BaseException as error:  # noqa: BLE001 - every failure is reported, then exit
        text = f"{type(error).__name__}: {error}"[:2000].encode()
        with contextlib.suppress(BaseException):
            if not started:
                conn.send_bytes(b"E" + text)
            else:  # appended to a partial output, it is rejected all the same
                os.write(conn.fileno(), b"E" + text)
                channel.shutdown(socket.SHUT_WR)
    finally:
        os._exit(0)


def _materialize(value):
    """AlgoTune's deep_materialize_fast (isolated_benchmark.py:246), timed with the solve:
    array-likes become arrays, containers are walked. Like it, imports numpy on every call."""
    import numpy  # noqa: PLC0415

    if hasattr(value, "__array__"):
        return numpy.asarray(value)
    if isinstance(value, dict):
        return {k: _materialize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_materialize(v) for v in value]
    if isinstance(value, tuple):
        items = tuple(_materialize(v) for v in value)
        if hasattr(value, "_fields"):
            try:
                return value.__class__(*items)
            except TypeError:
                return items
        return items
    return value


# ---------------------------------------------------------------------------- outputs

_SAFE_GLOBALS = {
    ("builtins", name) for name in (
        "list", "dict", "tuple", "set", "frozenset", "int", "float", "complex", "bool",
        "str", "bytes", "bytearray", "slice", "range", "object")
} | {
    ("collections", "OrderedDict"), ("collections", "defaultdict"), ("collections", "deque"),
    ("copyreg", "_reconstructor"), ("copyreg", "__newobj__"), ("copyreg", "__newobj_ex__"),
    ("fractions", "Fraction"), ("decimal", "Decimal"),
    ("numpy", "ndarray"), ("numpy", "dtype"),
} | {
    (module, name)
    for module in ("numpy.core.multiarray", "numpy._core.multiarray", "numpy.core.numeric",
                   "numpy._core.numeric")
    for name in ("_reconstruct", "scalar", "_frombuffer")
}


class _OutputUnpickler(pickle.Unpickler):
    """Builds only plain data: containers, numbers, strings, numpy arrays, scipy sparse, and
    the task's own namedtuples (e.g. rectanglepacking's RectanglePlacement)."""

    def __init__(self, data: bytes, task: str):
        super().__init__(io.BytesIO(data))
        self.task_module = f"AlgoTuneTasks.{task}.{task}"

    def find_class(self, module: str, name: str):
        if (module, name) in _SAFE_GLOBALS:
            return super().find_class(module, name)
        if module == self.task_module:
            found = super().find_class(module, name)
            if isinstance(found, type) and issubclass(found, tuple) and hasattr(found, "_fields"):
                return found
        if module.startswith("scipy.sparse.") and name.endswith(("_matrix", "_array")):
            import scipy.sparse  # noqa: PLC0415

            found = super().find_class(module, name)
            if isinstance(found, type) and issubclass(
                    found, (scipy.sparse.spmatrix, scipy.sparse.sparray)):
                return found
        raise pickle.UnpicklingError(f"output contains a disallowed type: {module}.{name}")


def _decode(data: bytes, task: str):
    return _rebuild(_OutputUnpickler(data, task).load())


def _rebuild(value):
    """Sparse matrices rebuilt from their arrays alone (no attributes or methods kept)."""
    if isinstance(value, dict):
        return {k: _rebuild(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_rebuild(v) for v in value]
    if isinstance(value, tuple):
        items = [_rebuild(v) for v in value]
        return type(value)(*items) if hasattr(value, "_fields") else tuple(items)
    if "scipy.sparse" in sys.modules:
        import numpy  # noqa: PLC0415
        import scipy.sparse  # noqa: PLC0415

        if isinstance(value, (scipy.sparse.spmatrix, scipy.sparse.sparray)):
            cls = type(value)
            state = object.__getattribute__(value, "__dict__")
            shape = tuple(int(n) for n in state["_shape"])
            arrays = {k: v for k, v in state.items() if isinstance(v, numpy.ndarray)}
            form = cls.__name__.split("_")[0]
            if form in ("csr", "csc"):
                return cls((arrays["data"], arrays["indices"], arrays["indptr"]), shape=shape)
            if form == "coo":
                coords = tuple(state["coords"])
                if not all(isinstance(c, numpy.ndarray) for c in coords):
                    raise pickle.UnpicklingError("sparse coordinates aren't arrays")
                return cls((arrays["data"], coords), shape=shape)
            raise pickle.UnpicklingError(f"sparse format {cls.__name__} isn't accepted")
    return value


# ---------------------------------------------------------------------------- processes

def _pids() -> list[int]:
    return [int(e.name) for e in Path("/proc").iterdir() if e.name.isdigit()]


def _ppid(pid: int) -> int | None:
    with contextlib.suppress(OSError, IndexError, ValueError):
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    return None


def _uid_of(pid: int) -> int | None:
    with contextlib.suppress(OSError, ValueError):
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[1])
    return None


def _tree(root: int) -> set[int]:
    """The process and its descendants."""
    parents = {pid: _ppid(pid) for pid in _pids()}
    tree = {root}
    while more := {pid for pid, ppid in parents.items() if ppid in tree} - tree:
        tree |= more
    return tree


def _thread_cpu(pids) -> dict[tuple[int, int], int]:
    """CPU time (ns) of each thread of these processes, by (pid, tid), from schedstat."""
    found = {}
    for pid in pids:
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            continue
        for tid in tids:
            with contextlib.suppress(OSError, ValueError, IndexError):
                found[(pid, int(tid))] = int(
                    Path(f"/proc/{pid}/task/{tid}/schedstat").read_text().split()[0])
    return found


def _wait_idle(threads) -> None:
    """Wait until the run's threads stop using CPU (two quiet 5 ms intervals), or give up."""
    deadline, quiet, last = time.monotonic() + IDLE_WAIT_S, 0, sum(threads().values())
    while quiet < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
        now = sum(threads().values())
        quiet = quiet + 1 if now - last < 200_000 else 0
        last = now


def _reap_strays() -> None:
    """Kill processes a run left behind; as subreaper, orphans become our children."""
    from multiprocessing import forkserver, resource_tracker  # noqa: PLC0415

    keep = {forkserver._forkserver._forkserver_pid, resource_tracker._resource_tracker._pid}
    me = os.getpid()
    for pid in _pids():
        if pid not in keep and _ppid(pid) == me:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError, OSError):
                os.waitpid(pid, 0)


def _end_user_session(uid: int) -> None:
    """Nothing of the solver user's survives a run: processes, then files in shared folders."""
    for _ in range(100):
        pids = [pid for pid in _pids() if _uid_of(pid) == uid]
        if not pids:
            break
        for pid in pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        time.sleep(0.01)
    else:
        raise SystemExit(f"processes of uid {uid} survive being killed")
    for top in SHARED_DIRS:
        for root, dirs, files in os.walk(top, topdown=False):
            for name in files + dirs:
                path = os.path.join(root, name)
                with contextlib.suppress(OSError):
                    info = os.lstat(path)
                    if info.st_uid == uid:
                        if stat.S_ISDIR(info.st_mode):
                            shutil.rmtree(path, ignore_errors=True)
                        else:
                            os.unlink(path)


def _sysv_ipc() -> dict[str, set[int]]:
    found = {}
    for kind in ("shm", "msg", "sem"):
        try:
            lines = Path(f"/proc/sysvipc/{kind}").read_text().splitlines()[1:]
        except OSError:
            lines = []
        found[kind] = {int(line.split()[1]) for line in lines if line.strip()}
    return found


def _remove_new_ipc(before: dict[str, set[int]]) -> None:
    """Remove System V IPC objects created since the start: they would outlive a run."""
    new = {kind: ids - before[kind] for kind, ids in _sysv_ipc().items()}
    if not any(new.values()):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    remove = 0  # IPC_RMID
    for ipc_id in new["shm"]:
        libc.shmctl(ipc_id, remove, None)
    for ipc_id in new["msg"]:
        libc.msgctl(ipc_id, remove, None)
    for ipc_id in new["sem"]:
        libc.semctl(ipc_id, 0, remove)


# ---------------------------------------------------------------------------- reference cache

class ReferenceCache:
    """The reference's measurements, one file per instance, in `folder`/<key digest>/.

    Only root reads or writes it: every folder on the way must be root's and not writable by
    others, the cache folders 0700, the files 0600, opened without following symlinks. An
    entry is written to a temporary file and renamed into place, so concurrent evaluators
    see a whole entry or none (two that both miss measure it twice; the last rename wins).
    An entry that can't be read, or doesn't match its key and instance, counts as missing
    and is measured again."""

    def __init__(self, folder: Path, key: dict):
        self.key = key
        self.digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
        self.folder = folder / self.digest[:32]
        _private_folder(folder)
        _private_folder(self.folder)
        with contextlib.suppress(OSError):  # temporary files a killed evaluator left
            for path in self.folder.glob(".tmp-*"):
                if time.time() - path.lstat().st_mtime > 3600:
                    path.unlink()

    def _name(self, seed: int, warm: bytes, timed: bytes) -> tuple[Path, dict]:
        instance = {"seed": seed, "warm_sha256": hashlib.sha256(warm).hexdigest(),
                    "timed_sha256": hashlib.sha256(timed).hexdigest()}
        name = hashlib.sha256(json.dumps(instance, sort_keys=True).encode()).hexdigest()
        return self.folder / f"{name[:32]}.json", instance

    def get(self, seed: int, warm: bytes, timed: bytes) -> dict | None:
        path, instance = self._name(seed, warm, timed)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_size > CACHE_ENTRY_MAX):
                return None
            with os.fdopen(fd, "rb", closefd=False) as file:
                stored = json.loads(file.read(CACHE_ENTRY_MAX + 1))
        except (OSError, ValueError):
            return None
        finally:
            os.close(fd)
        if not (isinstance(stored, dict) and stored.get("version") == CACHE_VERSION
                and stored.get("key") == self.key and stored.get("instance") == instance):
            return None
        measured = stored.get("measured")
        try:
            ok = (isinstance(measured["measured_at"], str)
                  and all(isinstance(measured[k], int) and measured[k] > 0
                          for k in ("reference_ns", "reference_overhead_ns", "output_size"))
                  and measured["reference_ns"] > measured["reference_overhead_ns"]
                  and isinstance(measured["prepare_limit_ns"], (int, float))
                  and measured["prepare_limit_ns"] > 0
                  and isinstance(measured["echo"]["overhead_ms"], list))
        except (KeyError, TypeError):
            ok = False
        return measured if ok else None

    def put(self, seed: int, warm: bytes, timed: bytes, measured: dict) -> None:
        path, instance = self._name(seed, warm, timed)
        text = json.dumps({"version": CACHE_VERSION, "key": self.key, "instance": instance,
                           "measured": measured}, indent=1).encode()
        try:
            fd, staged = tempfile.mkstemp(prefix=".tmp-", dir=self.folder)  # 0600
        except OSError as error:
            print(f"note: reference cache entry not written: {error}", flush=True)
            return
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            os.replace(staged, path)
        except BaseException as error:
            with contextlib.suppress(OSError):
                os.unlink(staged)
            if not isinstance(error, OSError):
                raise
            print(f"note: reference cache entry not written: {error}", flush=True)


def _private_folder(folder: Path) -> None:
    """Make `folder` (mode 0700) if missing; refuse one that isn't a real folder of this user's
    with that mode, or whose parent anyone else can write."""
    parent = folder.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or \
            parent.st_mode & 0o022:
        raise SystemExit(f"{folder.parent} must be a folder of root's that only root can write")
    with contextlib.suppress(FileExistsError):
        folder.mkdir(mode=0o700)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or \
            stat.S_IMODE(info.st_mode) != 0o700:
        raise SystemExit(f"{folder} must be a folder of root's with mode 0700 (the reference "
                         "cache); remove it or pass --no-reference-cache")


def _cache_key(task: str, data: Path, split: str, runs: int) -> dict:
    """What the reference's measurements depend on, besides the instance itself (its
    problem and warm-up problem are part of each entry's name)."""
    import numpy  # noqa: PLC0415
    import AlgoTuneTasks  # noqa: PLC0415

    root = Path(AlgoTuneTasks.__file__).resolve().parent
    code = hashlib.sha256()
    for path in sorted([*(root / task).rglob("*.py"), *root.glob("*.py")]):
        code.update(f"{path.relative_to(root)}\0".encode() + _sha256(path).encode())
    try:
        commit = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(root.parent), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        ).stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        commit = None
    cpu_model = None
    with contextlib.suppress(OSError):
        cpu_model = next((line.split(":", 1)[1].strip()
                          for line in Path("/proc/cpuinfo").read_text().splitlines()
                          if line.startswith("model name")), None)
    return {
        "task": task, "split": split, "runs": runs,
        "data_sha256": _sha256(data / task / f"{split}.jsonl"),
        "evaluator_sha256": _sha256(Path(__file__).resolve()),
        "algotune_commit": commit, "task_code_sha256": code.hexdigest(),
        "python": sys.version, "numpy": numpy.__version__,
        "affinity": sorted(os.sched_getaffinity(0)), "cpu_model": cpu_model,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------- task, data, build

def _task(name: str):
    from AlgoTuneTasks.factory import TaskFactory  # noqa: PLC0415

    with contextlib.redirect_stdout(io.StringIO()):
        return TaskFactory(name, oracle_time_limit=None)


def _instances(data: Path, task: str, split: str, limit: int | None) -> list[dict]:
    from AlgoTuner.utils.streaming_json import stream_jsonl  # noqa: PLC0415

    path = data / task / f"{split}.jsonl"
    if not path.is_file():
        raise SystemExit(f"no {split} data for {task}: {path} (make it with make_data.py)")
    records = list(stream_jsonl(str(path), decoder_base_dir=str(path.parent)))
    return records[:limit] if limit else records


def _plain(value):
    """Memory-mapped arrays (how AlgoTune loads large ones) as ordinary arrays."""
    import numpy  # noqa: PLC0415

    if isinstance(value, numpy.memmap):
        return numpy.array(value)
    if isinstance(value, mmap.mmap):
        return bytes(value[:])
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, tuple) and not hasattr(value, "_fields"):
        return tuple(_plain(v) for v in value)
    return value


def _require_private(folder: Path) -> None:
    """The solver user must not read the instances: the folder is the evaluator's alone."""
    info = folder.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise SystemExit(f"{folder} must belong to the evaluator's user with mode 0700, "
                         "so the solver user can't read the instances")


def _work_base(run_as: tuple[int, int] | None) -> Path | None:
    """Where the work folder goes: with run_as, WORK_BASE (made if missing), which must be
    root's alone, outside the shared folders the solver user's files are swept from (the
    build folder belongs to that user while it builds); else the default temporary folder."""
    if run_as is None:
        return None
    WORK_BASE.mkdir(mode=0o711, parents=True, exist_ok=True)
    info = WORK_BASE.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit(f"{WORK_BASE} must be a folder of root's that only root can write")
    WORK_BASE.chmod(0o711)
    return WORK_BASE


def _build(solver: Path, into: Path, run_as: tuple[int, int] | None) -> str | None:
    """Copy the solver folder and compile it the way AlgoTune does; an error text or None.

    Symlinks are refused. They are looked for in the copy, which is root's and out of the
    agent's reach, so one created while copying is caught too. The build steps run solver
    code, so with run_as they run as that user, in a folder it owns until they end."""
    shutil.copytree(solver, into, ignore=IGNORED, symlinks=True)
    for path in into.rglob("*"):
        if path.is_symlink():
            return f"symlinks aren't allowed in the solver folder: {path.relative_to(into)}"
    sources = [p for p in into.iterdir() if p.suffix in (".pyx", ".pxd", ".c", ".cpp")]
    steps = []
    if sources and (into / "setup.py").is_file():
        steps.append([sys.executable, "-I", "setup.py", "build_ext", "--inplace"])
    for path in into.glob("*.py"):
        if "pythran export" in path.read_text(errors="ignore").lower():
            steps.append(["pythran", "-O3", "-march=native", path.name])
    if not steps:
        return None
    if run_as is not None:
        _chown_tree(into, *run_as)
    try:
        for step in steps:
            try:
                done = subprocess.run(
                    step, cwd=into, capture_output=True, text=True, timeout=BUILD_TIMEOUT_S,
                    stdin=subprocess.DEVNULL, env={**os.environ, "HOME": str(into)},
                    user=run_as[0] if run_as else None, group=run_as[1] if run_as else None,
                    extra_groups=[] if run_as else None, start_new_session=True)
            except (OSError, subprocess.TimeoutExpired) as error:
                return f"build failed: {' '.join(step)}: {error}"
            if done.returncode != 0:
                return f"build failed: {' '.join(step)}:\n{(done.stdout + done.stderr)[-3000:]}"
    finally:
        if run_as is not None:  # the user's processes end before it loses the folder
            _end_user_session(run_as[0])
            _chown_tree(into, 0, 0)
    for path in into.rglob("*"):
        if path.is_symlink():
            return f"the build left a symlink: {path.relative_to(into)}"
    return None


def _chown_tree(folder: Path, uid: int, gid: int) -> None:
    for root, dirs, files in os.walk(folder):
        for name in [root, *(os.path.join(root, n) for n in dirs + files)]:
            os.lchown(name, uid, gid)


def _read_only(folder: Path) -> None:
    """Modes for the solver user to read the build; refuses anything but folders and files."""
    for root, _, files in os.walk(folder):
        os.chmod(root, 0o755)
        for name in files:
            path = os.path.join(root, name)
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                raise SystemExit(f"not a regular file in the build: {path}")
            os.chmod(path, 0o755 if info.st_mode & 0o100 else 0o644)


# ---------------------------------------------------------------------------- command line

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--task", required=True)
    parser.add_argument("--solver", type=Path, default=Path("."), help="folder with solver.py")
    parser.add_argument("--data", type=Path, required=True, help="folder with <task>/<split>.jsonl")
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--runs", type=int, help=f"runs per instance (dev {DEV_RUNS}, test {TEST_RUNS})")
    parser.add_argument("--limit", type=int, help="only the first N instances (smoke tests)")
    parser.add_argument("--json", type=Path, help="write the full result here")
    parser.add_argument("--run-as", metavar="USER",
                        help="run solver code as this unprivileged user (needs root)")
    parser.add_argument("--no-reference-cache", action="store_true",
                        help="time the reference afresh, neither reading nor writing the "
                             f"reference cache ({CACHE_DIR}; used only with --run-as)")
    parser.add_argument("--algotune", type=Path, default=os.environ.get("ALGOTUNE_ROOT"),
                        help="AlgoTune checkout (default $ALGOTUNE_ROOT)")
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        parser.error("needs Linux (/proc)")
    if args.algotune is None:
        parser.error("set --algotune or $ALGOTUNE_ROOT to an AlgoTune checkout")
    sys.path.insert(0, str(Path(args.algotune).resolve()))
    runs = args.runs or (DEV_RUNS if args.split == "dev" else TEST_RUNS)
    run_as = None
    if args.run_as:
        import pwd  # noqa: PLC0415

        if os.geteuid() != 0:
            parser.error("--run-as needs the evaluator to run as root")
        account = pwd.getpwnam(args.run_as)
        if account.pw_uid == 0:
            parser.error("--run-as must name an unprivileged user")
        run_as = (account.pw_uid, account.pw_gid)
    else:
        print("note: no --run-as, so solver code runs as this user (not isolated)", flush=True)

    def progress(done: int, total: int, entry: dict) -> None:
        if entry["valid"]:
            note = (f"{entry['speedup']:.2f}x ({entry['reference_ms']:.3f} -> "
                    f"{entry['solver_ms']:.3f} ms)")
            if entry.get("speedup_capped"):
                note += f" CAPPED: {entry['note']}"
            if entry.get("reference_cached"):
                note += f" (reference from the cache, measured {entry['reference_measured_at']})"
        else:
            note = f"INVALID: {entry.get('error')}"
        print(f"  {done}/{total} seed {entry['seed']}: {note}", flush=True)

    result = evaluate(args.task, args.solver.resolve(), args.data.resolve(), args.split, runs,
                      args.limit, progress, run_as, not args.no_reference_cache)
    if args.json:
        args.json.write_text(json.dumps(result, indent=1))
    if (cache := result.get("reference_cache", {})).get("used"):
        print(f"reference cache: {cache['hits']} of {cache['hits'] + cache['misses']} "
              f"instances from the cache (key {cache['key'][:12]})")
    if result["valid"]:
        print(f"{args.task} ({args.split}, {result['instances']} instances, {runs} runs): "
              f"mean speedup {result['speedup']:.4f}x")
        print(f"##autohelix[speedup={result['speedup']:.6g}]")
        return 0
    print(f"{args.task} ({args.split}): no speedup: "
          f"{result.get('error') or 'an instance was invalid (see above)'}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
