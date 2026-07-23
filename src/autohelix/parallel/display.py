# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Live display for parallel exploration runs.

Polls each worker's history.jsonl and renders one status line per worker,
updating in place via Rich Live. Designed to feel like the normal harness
output but condensed to a dashboard.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.text import Text

from autohelix.formatting import format_delta
from autohelix.history import History


_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass
class WorkerSnapshot:
    """Current state of a worker as read from its history."""

    name: str
    budget: int
    iteration: int = 0
    best_score: float | None = None
    best_metric: str | None = None
    baseline: float | None = None
    done: bool = False
    error: str | None = None
    direction: str = "higher"


class ParallelDisplay:
    """Renders live status lines for N parallel workers."""

    def __init__(self, worker_names: list[str], budgets: list[int], directions: list[str]):
        self._workers = [
            WorkerSnapshot(name=name, budget=budget, direction=direction)
            for name, budget, direction in zip(worker_names, budgets, directions)
        ]
        self._start_time = time.monotonic()

    @property
    def workers(self) -> list[WorkerSnapshot]:
        return self._workers

    def update_worker(self, index: int, history_path: Path) -> None:
        """Read a worker's history and update its snapshot."""
        w = self._workers[index]
        hist = History(history_path)

        try:
            results = hist.load()
        except (OSError, ValueError):
            return

        if not results:
            return

        baseline_r = next((r for r in results if r.iteration == 0), None)
        if baseline_r and baseline_r.metrics:
            metric = next(iter(baseline_r.metrics))
            w.baseline = baseline_r.metrics[metric]
            w.best_metric = metric

        iters = [r for r in results if r.iteration > 0]
        if iters:
            w.iteration = max(r.iteration for r in iters)

            # Find best score among accepted
            accepted = [r for r in iters if r.accepted]
            if accepted and w.best_metric:
                values = [r.metrics.get(w.best_metric) for r in accepted if w.best_metric in r.metrics]
                if values:
                    if w.direction == "lower":
                        w.best_score = min(v for v in values if v is not None)
                    else:
                        w.best_score = max(v for v in values if v is not None)

        if w.iteration >= w.budget:
            w.done = True

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield self.render()

    def render(self) -> Text:
        elapsed = int(time.monotonic() - self._start_time)
        mins, secs = divmod(elapsed, 60)
        time_str = f"{mins}m {secs:02d}s" if mins else f"{secs}s"

        frame_idx = int(time.monotonic() * 10) % len(_SPINNER_FRAMES)
        spinner = _SPINNER_FRAMES[frame_idx]

        lines = Text()
        for w in self._workers:
            lines.append(f"  {w.name}", style="bold")
            lines.append(f" │ iter {w.iteration}/{w.budget}", style="dim")

            if w.best_score is not None and w.best_metric:
                score_str = f" │ {w.best_metric}: {w.best_score:g}"
                lines.append(score_str)
                if w.baseline is not None:
                    delta = format_delta(w.best_score, w.baseline)
                    if delta:
                        lines.append(f" ({delta})")

            lines.append(" │ ")
            if w.error:
                lines.append("error", style="red")
            elif w.done:
                lines.append("done", style="green")
            else:
                lines.append(f"{spinner} running", style="dim")

            lines.append("\n")

        # Footer: elapsed + status
        footer_parts = [f"  {time_str}"]
        all_done = all(w.done or w.error for w in self._workers)
        if all_done:
            footer_parts.append("complete")
        lines.append(" │ ".join(footer_parts), style="dim")

        return lines
