# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""History logging and retrieval."""

import json
import logging
import os
import shutil
import stat
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path, PurePosixPath
from typing import Any

from autohelix.config import capture_problem
from autohelix.formatting import SUMMARY_LIMIT, clean, clean_block
from autohelix.costs import CostSummary, summarize_costs
from autohelix.runs import RunPaths, SCHEMA, iteration_folder

logger = logging.getLogger(__name__)


@dataclass
class IterationResult:
    """Result of a single iteration."""

    iteration: int
    accepted: bool
    metrics: dict[str, float] = field(default_factory=dict)
    commit: str | None = None
    reason: str | None = None
    failure_output: str | None = None
    # Set when a stop command exited 0 after this iteration.
    stop: dict[str, str] | None = None
    schema: int = SCHEMA
    started_at: float | None = None
    ended_at: float | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    # One item per gate, in list order: {"text", "kind": "command"|"metric", "passed":
    # true|false|null (not reached)}; commands that ran add "return_code" and
    # "duration_s", a failed one its "output" tail; checked metric gates add "value",
    # "reference" and, failed, "reason". Empty if evaluation never started.
    gates: list[dict[str, Any]] = field(default_factory=list)
    # One item per observe entry that ran, in order: {"name", "ok", "return_code",
    # "error", "metrics", "duration_s"}.
    observe: list[dict[str, Any]] = field(default_factory=list)
    # The attempt itself: the agent's one-line summary (commit_summary.txt, whenever its
    # copy was frozen, accepted or not), the commit it was built on (A), the frozen tree
    # (T), the out-of-scope paths dropped at freeze (project-relative), and for a
    # rejected attempt with a tree the ref keeping it (refs/autohelix-rejected/<id>/<n>).
    summary: str | None = None
    base: str | None = None
    tree: str | None = None
    discarded: list[str] = field(default_factory=list)
    discarded_frozen: list[str] = field(default_factory=list)  # those of `discarded` that are frozen
    # Why the main agent's call failed or timed out (the attempt was rejected unchecked).
    agent_error: str | None = None
    rejected_ref: str | None = None

    @property
    def usage(self):
        """Main-agent usage, derived from the authoritative calls."""
        return next((call.get("usage") or {} for call in self.calls if call["role"] == "agent"), {})

    @property
    def command_usage(self):
        return [call.get("usage") or {} for call in self.calls if call["role"] == "command"]

    @property
    def cost_usages(self) -> list[dict[str, Any]]:
        return [call.get("usage") or {} for call in self.calls]

    @property
    def cost(self) -> CostSummary:
        return summarize_costs(self.cost_usages)


def _copy_error(error: BaseException) -> str:
    """One line for a copy failure (shutil.Error carries a list of per-file errors)."""
    if isinstance(error, shutil.Error) and error.args and isinstance(error.args[0], list):
        failures = error.args[0]
        first = failures[0][-1] if failures and isinstance(failures[0], tuple) else error
        more = f" (and {len(failures) - 1} more)" if len(failures) > 1 else ""
        return f"{first}{more}"
    return str(error)


def _write_record_file(path: Path, record: dict) -> None:
    """Replace `path` atomically with `record`, pretty-printed. The temporary file is
    created fresh (never through something planted at its name); the rename replaces
    a symlink at `path` itself, never its target."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if os.path.lexists(temporary):
        temporary.unlink()
    with open(temporary, "x") as stream:
        stream.write(json.dumps(record, indent=2) + "\n")
    temporary.replace(path)


def write_records(paths: RunPaths, records: list[dict], *, mode: str = "append",
                  iterations=None) -> None:
    """The one writer of history records. `mode` "append" appends `records` to
    history.jsonl, "replace" makes them its whole content (atomically), "derived"
    leaves it alone (`records` is its content as it stands); then, last, rewrites the
    `record.json` of each affected iteration (`iterations`; when appending, those of
    `records`) from the last record with its number, or removes it if none is left
    (a torn record's iteration). history.jsonl stays the source of truth: a
    record.json marks an iteration folder whose record is complete."""
    history = paths.history
    history.parent.mkdir(parents=True, exist_ok=True)
    lines = "".join(json.dumps(record) + "\n" for record in records)
    if mode == "derived":
        pass
    elif mode == "replace":
        temporary = history.with_suffix(".tmp")
        with temporary.open("w") as stream:
            stream.write(lines)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(history)
    elif mode == "append":
        with open(history, "a") as stream:
            stream.write(lines)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        raise ValueError(f"Unknown mode: {mode!r}")
    affected = {record["iteration"] for record in records} if iterations is None else set(iterations)
    for iteration in sorted(affected):
        record = next((r for r in reversed(records) if r["iteration"] == iteration), None)
        if record is not None:
            _write_record_file(iteration_folder(paths, iteration) / "record.json", record)
        else:
            path = paths.record(iteration)
            if os.path.lexists(path) and not (paths.iterations.is_symlink()
                                              or paths.iteration_dir(iteration).is_symlink()):
                path.unlink()


def stale_records(paths: RunPaths, records: list[dict]) -> set[int]:
    """The iterations whose record.json is missing, reached through a symlink, or not
    the last record with their number in `records` (history.jsonl's content): left so
    by a process killed between its history write and the record.json after it."""
    stale = set()
    for iteration in {record["iteration"] for record in records}:
        last = next(r for r in reversed(records) if r["iteration"] == iteration)
        path = paths.record(iteration)
        try:
            if os.path.realpath(path) != os.path.abspath(path) or not path.is_file():
                raise OSError("not a regular file reached without symlinks")
            if json.loads(path.read_text()) == last:
                continue
        except (OSError, ValueError):
            pass
        stale.add(iteration)
    return stale


class History:
    """Manages iteration history in .autohelix/."""

    def __init__(self, project_path: Path, run_id: str | None = None):
        self.project_path = project_path
        self.paths = RunPaths(project_path, run_id)
        self.history_path = self.paths.history
        self._cache: list[IterationResult] | None = None
        self._cache_mtime: float | None = None

    def entry_dir(self, iteration: int, name: str) -> Path:
        """`iterations/NNN/observe/<name>/`, one observe entry's output (`observe/` is
        created as a real folder; the entry's folder is not)."""
        if not name or name.startswith(".") or "/" in name or os.sep in name:
            raise ValueError(f"Not an observe entry name: {name!r}")
        iteration_folder(self.paths, iteration)
        d = self.paths.observe_dir(iteration)
        if d.is_symlink() or d.is_file():  # planted: never write through it
            d.unlink()
        d.mkdir(exist_ok=True)
        return self.paths.observe_entry(iteration, name)

    def supersede(self, iteration: int) -> Path | None:
        """At an attempt's start: move what an earlier attempt at this iteration left
        (a killed iteration, a baseline retry), `iterations/NNN/`, to
        `.internal/attempts/NNN-<k>/` (the first unused k >= 1). Returns where it went,
        or None if there was nothing (something planted there is removed)."""
        # Outermost first: anything below a planted link would be reached through it.
        for planted in (self.paths.iterations, self.paths.iteration_dir(iteration)):
            if planted.is_symlink() or planted.is_file():
                planted.unlink()
                return None
        folder = self.paths.iteration_dir(iteration)
        if not folder.is_dir():
            return None
        for planted in (self.paths.internal, self.paths.attempts):
            if planted.is_symlink() or planted.is_file():
                planted.unlink()
        self.paths.attempts.mkdir(parents=True, exist_ok=True)
        k = 1
        while True:
            target = self.paths.attempt_dir(iteration, k)
            if not os.path.lexists(target):
                try:
                    os.rename(folder, target)
                    break
                except FileExistsError:
                    pass
            k += 1
        self._repoint_notes(target)
        return target

    def _repoint_notes(self, moved: Path) -> None:
        """The moved folder's `notes` link was relative to `iterations/NNN/`: point it at
        the same version from `.internal/attempts/NNN-k/`, or drop it if it isn't a
        link to a notes version."""
        link = moved / "notes"
        if not os.path.lexists(link):
            return
        try:
            parts = os.readlink(link).split("/")
        except OSError:  # not a link: planted
            parts = None
        versions = os.path.relpath(self.paths.notes_versions, self.paths.iteration_dir(0)).split(os.sep)
        if link.is_dir() and not link.is_symlink():
            shutil.rmtree(link)
            return
        link.unlink()
        if parts and parts[:-1] == versions and parts[-1] not in ("", ".", ".."):
            os.symlink(os.path.relpath(self.paths.notes_versions / parts[-1], moved), link)

    def stage_output(self, stage: Path, stdout: str, stderr: str, root: Path,
                     captures: list[str]) -> list[str]:
        """Write an observe entry's output into `stage` (an empty folder): `stdout.txt`,
        `stderr.txt` (only if non-empty), and each captured file or folder from `root`
        (the checks copy) at its relative path, so captures with one basename don't
        collide. Returns the problems with captures (skipped, or partly copied): never
        raises for one. Paths escaping the copy or naming repository/harness state are
        refused; symlinks (at or above the path, or inside a folder) are never followed;
        anything but regular files and folders (FIFOs, sockets, devices) is skipped."""
        (stage / "stdout.txt").write_text(stdout)
        if stderr:
            (stage / "stderr.txt").write_text(stderr)
        problems = []
        for capture in captures:
            try:
                problem = self._stage_capture(stage, root, capture)
            except (OSError, shutil.Error) as e:
                problem = f"capture {capture!r} not saved completely: {_copy_error(e)}"
            if problem:
                problems.append(problem)
        return problems

    @staticmethod
    def _stage_capture(stage: Path, root: Path, capture: str) -> str | None:
        if problem := capture_problem(capture):
            return f"capture {capture!r} skipped: {problem}"
        parts = [part for part in PurePosixPath(capture).parts if part not in ("", ".")]
        source = root
        for part in parts:
            source = source / part
            if source.is_symlink():
                return f"capture {capture!r} skipped: {'/'.join(source.relative_to(root).parts)} is a symlink"
        try:
            mode = source.lstat().st_mode
        except FileNotFoundError:
            return None  # nothing there to save
        target = stage.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if stat.S_ISDIR(mode):
            def ignore(folder, names):  # only regular files and folders, never followed
                kept = set()
                for name in names:
                    try:
                        if stat.S_ISREG(m := os.lstat(os.path.join(folder, name)).st_mode) or stat.S_ISDIR(m):
                            kept.add(name)
                    except OSError:
                        pass
                return [name for name in names if name not in kept]

            shutil.copytree(source, target, symlinks=True, ignore=ignore, dirs_exist_ok=True)
        elif stat.S_ISREG(mode):
            shutil.copy2(source, target)
        else:
            return f"capture {capture!r} skipped: not a regular file or folder"
        return None

    def install_observation(self, iteration: int, name: str, stage: Path) -> None:
        """Make `stage` the entry's `iterations/NNN/observe/<name>/`, replacing whatever is
        there (e.g. something an earlier command wrote by absolute path)."""
        target = self.entry_dir(iteration, name)
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        shutil.move(str(stage), str(target))

    def previous_observations(self, name: str, before: int) -> Path | None:
        """`iterations/KKK/observe/<name>/` of the most recent accepted iteration K < `before`
        (the baseline and accepted no-ops count) in which that entry succeeded; rejected
        iterations, and failed runs of the entry, never count."""
        for record in reversed(self.load()):
            if not record.accepted or record.iteration >= before:
                continue
            if not any(item.get("name") == name and item.get("ok") for item in record.observe):
                continue
            d = self.paths.observe_entry(record.iteration, name)
            if d.is_dir() and os.path.realpath(d) == os.path.abspath(d):  # reached without symlinks
                return d
        return None

    def _invalidate_cache(self) -> None:
        """Invalidate the cached results."""
        self._cache = None
        self._cache_mtime = None

    @staticmethod
    def _record(result: IterationResult) -> dict:
        return {**asdict(result), "cost": asdict(result.cost)}

    def append(self, result: IterationResult) -> None:
        """Append an iteration result to history."""
        write_records(self.paths, [self._record(result)])
        self._invalidate_cache()

    def update(self, result: IterationResult) -> None:
        """Replace one existing record, preserving postaccept stop calls."""
        results = self.load()
        if not any(r.iteration == result.iteration for r in results):
            raise ValueError(f"No history record for iteration {result.iteration}")
        self._rewrite([result if r.iteration == result.iteration else r for r in results], {result.iteration})

    def _rewrite(self, results: list[IterationResult], iterations) -> None:
        write_records(self.paths, [self._record(r) for r in results], mode="replace", iterations=iterations)
        self._invalidate_cache()

    @property
    def cost_usages(self) -> list[dict[str, Any]]:
        """All recorded run calls."""
        return [usage for result in self.load() for usage in result.cost_usages]

    def load(self) -> list[IterationResult]:
        """Load all iteration results from history (cached by file mtime)."""
        if not self.history_path.exists():
            self._cache = []
            self._cache_mtime = None
            return []

        current_mtime = self.history_path.stat().st_mtime
        if self._cache is not None and self._cache_mtime == current_mtime:
            return self._cache

        known_fields = {f.name for f in fields(IterationResult)}
        results = []
        with open(self.history_path) as f:
            lines = f.readlines()
            for lineno, line in enumerate(lines, start=1):
                terminated = line.endswith("\n")
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if (not isinstance(data, dict) or type(data.get("iteration")) is not int
                            or data["iteration"] < 0 or type(data.get("accepted")) is not bool):
                        raise ValueError("invalid iteration record")
                    # Tolerate forward-compat fields written by a newer version.
                    data = {k: v for k, v in data.items() if k in known_fields}
                    results.append(IterationResult(**data))
                except (json.JSONDecodeError, TypeError, ValueError) as e:
                    if lineno != len(lines) or terminated:
                        raise RuntimeError(f"Corrupt history record {lineno}: {self.history_path}") from e
                    logger.warning("Incomplete final history record in %s: %s", self.history_path, e)
        self._cache = results
        self._cache_mtime = current_mtime
        return results

    def get_last_iteration(self) -> int:
        """Get the last iteration number, or 0 if no history."""
        results = self.load()
        if not results:
            return 0
        return max(r.iteration for r in results)

    def get_best_metrics(
        self, directions: dict[str, str] | None = None
    ) -> dict[str, tuple[float, int]]:
        """Get the best value for each metric and which iteration achieved it.

        Returns dict of metric_name -> (best_value, iteration).

        Args:
            directions: Optional mapping of metric name to "higher" or "lower".
                        Defaults to "higher" for unknown metrics.
        """
        directions = directions or {}
        results = self.load()
        best: dict[str, tuple[float, int]] = {}

        for r in results:
            if not r.accepted:
                continue
            for name, value in r.metrics.items():
                lower_is_better = directions.get(name, "higher") == "lower"
                if name not in best:
                    best[name] = (value, r.iteration)
                elif lower_is_better and value < best[name][0]:
                    best[name] = (value, r.iteration)
                elif not lower_is_better and value > best[name][0]:
                    best[name] = (value, r.iteration)

        return best

    def latest(self, before: int | None = None) -> IterationResult | None:
        """The latest iteration's record (below `before`): the highest number and, of
        records sharing it (a retried baseline), the last one written."""
        records = [r for r in self.load() if before is None or r.iteration < before]
        if not records:
            return None
        top = max(r.iteration for r in records)
        return [r for r in records if r.iteration == top][-1]

    def get_recent(self, n: int = 5) -> list[IterationResult]:
        """Get the n most recent iteration results."""
        results = self.load()
        return results[-n:] if len(results) > n else results

    def format_summary(self, without_output: int | None = None) -> str:
        """The last 5 iterations for the agent prompt, one line each: metrics (accepted)
        or the rejection reason, then the agent's one-line summary; below it a
        rejection's failure output (not for iteration `without_output`, which the prompt
        shows in full) and any observe entry that failed. Agent-written text is cleaned."""
        results = self.load()
        if not results:
            return "No previous iterations."

        lines = []
        for r in self.get_recent(5):
            if r.accepted:
                metrics_str = ", ".join(f"{k}={v}" for k, v in r.metrics.items())
                line = f"- iter {r.iteration}: {metrics_str + ' ' if metrics_str else ''}(accepted)"
            else:
                line = f"- iter {r.iteration}: rejected ({r.reason})"
            lines.append(clean_block(line).replace("\n", " ")
                         + (f" — {clean(r.summary, SUMMARY_LIMIT)}" if r.summary else ""))
            # Include failure output so agent can understand what went wrong
            if not r.accepted and r.failure_output and r.iteration != without_output:
                for output_line in clean_block(r.failure_output).splitlines():
                    lines.append(f"    {output_line}")
            for item in r.observe:
                if not item.get("ok", True):
                    lines.append(f"    observe {clean(item.get('name'))} failed: {clean(item.get('error'))}")

        return "\n".join(lines)
