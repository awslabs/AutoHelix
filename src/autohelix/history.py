# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""History logging and retrieval."""

import json
import logging
import shutil
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class IterationResult:
    """Result of a single iteration."""

    iteration: int
    accepted: bool
    metrics: dict[str, float]
    commit: str | None = None
    reason: str | None = None
    failure_output: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


class History:
    """Manages iteration history in .autohelix/."""

    def __init__(self, project_path: Path):
        self.project_path = project_path
        self.history_path = project_path / ".autohelix" / "history.jsonl"
        self._observations_dir = project_path / ".autohelix" / "observations"
        self._cache: list[IterationResult] | None = None
        self._cache_mtime: float | None = None

    def iter_dir(self, iteration: int) -> Path:
        """Return the directory for a given iteration's observation artifacts."""
        d = self._observations_dir / f"iter-{iteration}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_stdout(self, iteration: int, output: str, index: int | None = None) -> None:
        """Save command stdout to the iteration directory.

        Args:
            iteration: The iteration number.
            output: The stdout+stderr text.
            index: If multiple observable commands, the command index (0-based).
                   None means single command (saves as stdout.txt).
        """
        d = self.iter_dir(iteration)
        if index is not None:
            path = d / f"stdout-{index}.txt"
        else:
            path = d / "stdout.txt"
        path.write_text(output)

    def save_capture(self, iteration: int, src_path: Path) -> None:
        """Copy a captured file into the iteration directory."""
        d = self.iter_dir(iteration)
        if src_path.exists():
            shutil.copy2(src_path, d / src_path.name)

    def _invalidate_cache(self) -> None:
        """Invalidate the cached results."""
        self._cache = None
        self._cache_mtime = None

    def append(self, result: IterationResult) -> None:
        """Append an iteration result to history."""
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.history_path, "a") as f:
            f.write(json.dumps(asdict(result)) + "\n")
        self._invalidate_cache()

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
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    data.setdefault("usage", {})
                    # Tolerate forward-compat fields written by a newer version.
                    data = {k: v for k, v in data.items() if k in known_fields}
                    results.append(IterationResult(**data))
                except (json.JSONDecodeError, TypeError, ValueError) as e:
                    # A crash mid-write can leave one partial line; skipping it lets
                    # the run resume and `report` work instead of failing outright.
                    logger.warning(
                        "Skipping malformed history line %d in %s: %s",
                        lineno, self.history_path, e,
                    )
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
