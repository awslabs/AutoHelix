# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""History logging and retrieval."""

import json
import logging
import os
import shutil
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path, PurePosixPath
from typing import Any

from autohelix.costs import CostSummary, summarize_costs
from autohelix.runs import RunPaths, SCHEMA

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
    constraints: list[dict[str, Any]] = field(default_factory=list)
    # One item per gate, in list order: {"text", "kind": "command"|"metric", "passed":
    # true|false|null (not reached)}; commands that ran add "return_code" and
    # "duration_s", a failed one its "output" tail; checked metric gates add "value",
    # "reference" and, failed, "reason". Empty if evaluation never started.
    gates: list[dict[str, Any]] = field(default_factory=list)
    # One item per observe entry that ran, in order: {"name", "ok", "return_code",
    # "error", "metrics", "duration_s"}.
    observe: list[dict[str, Any]] = field(default_factory=list)

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


class History:
    """Manages iteration history in .autohelix/."""

    def __init__(self, project_path: Path, run_id: str | None = None):
        self.project_path = project_path
        self.paths = RunPaths(project_path, run_id)
        self.history_path = self.paths.history
        self._observations_dir = self.paths.observations
        self._cache: list[IterationResult] | None = None
        self._cache_mtime: float | None = None

    def iter_dir(self, iteration: int) -> Path:
        """Return the directory for a given iteration's observation artifacts."""
        d = self._observations_dir / f"iter-{iteration}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def entry_dir(self, iteration: int, name: str) -> Path:
        """`observations/iter-N/<name>/`, one observe entry's output, created."""
        if not name or name.startswith(".") or "/" in name or os.sep in name:
            raise ValueError(f"Not an observe entry name: {name!r}")
        d = self.iter_dir(iteration) / name
        d.mkdir(exist_ok=True)
        return d

    def clear_observations(self, iteration: int) -> None:
        """Remove what an earlier attempt at this iteration (a baseline retry) stored."""
        d = self._observations_dir / f"iter-{iteration}"
        if d.is_symlink() or d.is_file():
            d.unlink()
        elif d.is_dir():
            shutil.rmtree(d)

    def save_stdout(self, iteration: int, name: str, output: str) -> None:
        """Save an observe entry's stdout+stderr as `observations/iter-N/<name>/stdout.txt`."""
        (self.entry_dir(iteration, name) / "stdout.txt").write_text(output)

    def save_capture(self, iteration: int, name: str, root: Path, capture: str) -> str | None:
        """Copy a captured file (or folder) from `root` (the checks copy) into the entry's
        folder at the same relative path, so captures with one basename don't collide.
        Returns why it was skipped, or None (saved, or nothing there to save). Paths
        escaping the copy, and symlinks (at or above the path, or inside a folder), are
        never followed."""
        rel = PurePosixPath(capture)
        parts = [part for part in rel.parts if part not in ("", ".")]
        if rel.is_absolute() or not parts or ".." in parts:
            return f"capture {capture!r} skipped: not a path inside the copy"
        if parts == ["stdout.txt"]:
            return f"capture {capture!r} skipped: stdout.txt holds the entry's output"
        source = root
        for part in parts:
            source = source / part
            if source.is_symlink():
                return f"capture {capture!r} skipped: {'/'.join(source.relative_to(root).parts)} is a symlink"
        if not source.exists():
            return None
        target = self.entry_dir(iteration, name).joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True,
                            ignore=lambda folder, names: [n for n in names
                                                          if os.path.islink(os.path.join(folder, n))])
        elif source.is_file():
            shutil.copy2(source, target)
        else:
            return f"capture {capture!r} skipped: not a file or folder"
        return None

    def previous_observations(self, name: str, before: int) -> Path | None:
        """`observations/iter-K/<name>/` of the most recent accepted iteration K < `before`
        (the baseline and accepted no-ops count) that has one; rejected iterations never
        count."""
        for record in reversed(self.load()):
            if record.accepted and record.iteration < before:
                d = self._observations_dir / f"iter-{record.iteration}" / name
                if d.is_dir() and not d.is_symlink():
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
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.history_path, "a") as f:
            f.write(json.dumps(self._record(result)) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._invalidate_cache()

    def update(self, result: IterationResult) -> None:
        """Replace one existing record, preserving postaccept stop calls."""
        results = self.load()
        if not any(r.iteration == result.iteration for r in results):
            raise ValueError(f"No history record for iteration {result.iteration}")
        self._rewrite([result if r.iteration == result.iteration else r for r in results])

    def _rewrite(self, results: list[IterationResult]) -> None:
        temporary = self.history_path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            stream.write("".join(json.dumps(self._record(r)) + "\n" for r in results))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.history_path)
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

    def get_recent(self, n: int = 5) -> list[IterationResult]:
        """Get the n most recent iteration results."""
        results = self.load()
        return results[-n:] if len(results) > n else results

    def format_summary(self) -> str:
        """Format a summary of the history for the agent prompt."""
        results = self.load()
        if not results:
            return "No previous iterations."

        lines = []
        for r in self.get_recent(5):
            if r.accepted:
                metrics_str = ", ".join(f"{k}={v}" for k, v in r.metrics.items())
                lines.append(f"- iter {r.iteration}: {metrics_str} (accepted)")
            else:
                lines.append(f"- iter {r.iteration}: rejected ({r.reason})")
                # Include failure output so agent can understand what went wrong
                if r.failure_output:
                    for line in r.failure_output.splitlines():
                        lines.append(f"    {line}")

        return "\n".join(lines)
