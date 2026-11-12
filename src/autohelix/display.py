# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Console output: live display of agent and reviewer sessions, and the run's
transcript (header, baseline and iteration rows)."""

import time as _time
from collections import deque

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.panel import Panel
from rich.segment import Segment
from rich.text import Text

from autohelix.agents import AgentEvent, AgentEventKind
from autohelix.formatting import clean, format_delta  # noqa: F401 - clean: re-exported


_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class LiveDisplay:
    """Manages the live Rich panel for streaming agent/reviewer output.

    Implements __rich_console__ so Rich Live auto-refresh produces
    smooth spinner animation without needing explicit render() calls.
    """

    def __init__(
        self,
        console: Console,
        title: str,
        border_style: str = "blue",
        verbose: bool = False,
        max_entries: int = 10,
        iteration: int | None = None,
    ):
        self.console = console
        self.title = title
        self.border_style = border_style
        self.verbose = verbose
        self._iteration = iteration
        self._state: dict = {
            "entries": deque(maxlen=max_entries),
            "active_label": None,
            "active_text": "",
            "active_style": "white",
            "events": 0,
            "tools": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "last_update": f"Waiting for {title.lower()} output",
        }
        self._elapsed = 0
        self._start_time = _time.monotonic()

    # -- State mutation -------------------------------------------------------

    def _append_entry(self, label: str, text: str, style: str) -> None:
        """Append a finalized line to the rolling live window."""
        if not text:
            return
        compact = truncate_for_display(text)
        self._state["entries"].append((label, compact, style))
        self._state["last_update"] = f"{label}: {truncate_for_display(text, limit=80)}"

    def _set_active_entry(self, label: str, text: str, style: str) -> None:
        """Update the currently streaming line in the live window."""
        self._state["active_label"] = label
        self._state["active_text"] = truncate_for_display(text)
        self._state["active_style"] = style
        self._state["last_update"] = (
            f"{label}: {truncate_for_display(text, limit=80)}" if text else label
        )

    def _flush_active_entry(self) -> None:
        """Move the current streaming line into the rolling history."""
        label = self._state.get("active_label")
        text = self._state.get("active_text")
        style = self._state.get("active_style", "white")
        if label and text:
            self._state["entries"].append((label, text, style))
        self._state["active_label"] = None
        self._state["active_text"] = ""
        self._state["active_style"] = "white"

    # -- Event handling -------------------------------------------------------

    def handle_event(self, event: AgentEvent) -> None:
        """Update live UI state from a normalized agent event (safe wrapper)."""
        try:
            self._handle_event(event)
        except Exception as exc:
            self._flush_active_entry()
            if self.verbose:
                self._append_entry("render", f"Skipping unrenderable event: {exc}", "dim")

    def _handle_event(self, event: AgentEvent) -> None:
        """Core event handler."""
        self._state["events"] += 1
        label, style = event_presenter(event)
        if event.kind == AgentEventKind.TOOL_USE:
            self._state["tools"] += 1

        if event.metadata.get("input_tokens"):
            self._state["input_tokens"] += event.metadata["input_tokens"]
        if event.metadata.get("output_tokens"):
            self._state["output_tokens"] += event.metadata["output_tokens"]

        if event.metadata.get("stream"):
            self._set_active_entry(label, event.text, style)
            return
        if event.metadata.get("final"):
            self._append_entry(label, event.text, style)
            self._state["active_label"] = None
            self._state["active_text"] = ""
            self._state["active_style"] = "white"
            return
        self._flush_active_entry()
        self._append_entry(label, event.text, style)

    def flush(self) -> None:
        """Flush the active entry (public wrapper for harness to call at end)."""
        self._flush_active_entry()

    # -- Rendering ------------------------------------------------------------

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Called by Rich Live on every refresh — keeps spinner smooth."""
        yield self.render()

    def render(self, elapsed: int | None = None, height: int | None = None) -> RenderableType:
        """Render the live status; `height` caps the verbose panel (older lines go first)."""
        if elapsed is not None:
            self._elapsed = elapsed
        else:
            self._elapsed = int(_time.monotonic() - self._start_time)
        if self.verbose:
            return self._render_verbose(height)
        return self._render_compact()

    def _render_compact(self) -> Text:
        """Spinner line: ⠋ iter N ─ time."""
        frame_idx = int(_time.monotonic() * 10) % len(_SPINNER_FRAMES)
        frame = _SPINNER_FRAMES[frame_idx]

        mins, secs = divmod(self._elapsed, 60)
        time_str = f"{mins}m {secs:02d}s" if mins else f"{secs}s"

        if self._iteration and self.title == "review":
            label = f"iter {self._iteration} (review)"
        elif self._iteration:
            label = f"iter {self._iteration}"
        else:
            label = self.title
        return Text(f"  {frame} {label} ─ {time_str}", style="dim")

    def _render_verbose(self, height: int | None = None) -> Panel:
        """Full rolling log with event history."""
        mins, secs = divmod(self._elapsed, 60)
        time_str = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
        header = Text.assemble(
            ("Elapsed ", "dim"),
            (time_str, "bold"),
            ("  |  Events ", "dim"),
            (str(self._state["events"]), "bold"),
            ("  |  Tools ", "dim"),
            (str(self._state["tools"]), "bold"),
        )

        body: list[Text] = [header]
        last_update = self._state.get("last_update")
        if last_update:
            body.append(Text(last_update, style="dim"))

        for label, text, style in self._state["entries"]:
            body.append(Text.assemble((f"{label}: ", style), text))

        if self._state.get("active_label") and self._state.get("active_text"):
            body.append(
                Text.assemble(
                    (f"{self._state['active_label']}: ", self._state["active_style"]),
                    self._state["active_text"],
                )
            )

        if len(body) == 2 and not self._state["entries"] and not self._state.get("active_text"):
            body.append(Text(f"Waiting for {self.title.lower()} output...", style="dim"))

        content = Group(*body)
        if height is not None:  # the borders and the header lines stay; the log keeps its newest lines
            head = 2 if last_update else 1
            content = Group(*body[:head], Tail(Group(*body[head:]), max(1, height - 2 - head)))
        return Panel(content, title=self.title, border_style=self.border_style)


class Tail:
    """The last `height` lines of a renderable, as laid out at the width given."""

    def __init__(self, renderable: RenderableType, height: int):
        self.renderable = renderable
        self.height = height

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        lines = console.render_lines(self.renderable, options.update(height=None), pad=False)
        for line in lines[-self.height:]:
            yield from line
            yield Segment.line()


# -- Pure functions (easily testable) -----------------------------------------


def truncate_for_display(text: str, limit: int = 220) -> str:
    """Keep live panel lines compact while preserving the most recent content."""
    single_line = " ".join(text.split())
    if len(single_line) <= limit:
        return single_line
    return "..." + single_line[-(limit - 3):]


def event_presenter(event: AgentEvent) -> tuple[str, str]:
    """Map normalized events to a live-panel label and style."""
    if event.kind == AgentEventKind.THINKING:
        return "thinking", "magenta"
    if event.kind == AgentEventKind.ASSISTANT_TEXT:
        return "assistant", "green"
    if event.kind == AgentEventKind.TOOL_USE:
        return "tool", "cyan"
    if event.kind == AgentEventKind.TOOL_INPUT:
        tool_name = str(event.metadata.get("tool_name") or "tool")
        return f"{tool_name} input", "cyan"
    if event.kind == AgentEventKind.TOOL_RESULT:
        return "tool result", "red" if event.metadata.get("is_error") else "yellow"
    if event.kind == AgentEventKind.WARNING:
        return "warning", "yellow"
    if event.kind == AgentEventKind.ERROR:
        return "error", "red"
    if event.kind == AgentEventKind.RESULT:
        return "result", "dim"
    if event.kind == AgentEventKind.STATUS:
        return "status", "dim"
    if isinstance(event.kind, str) and event.kind:
        return event.kind.replace("_", " "), "yellow"
    return "raw", "yellow"


# -- The run's transcript: header, a baseline line, two lines per iteration -------

LABEL = 11  # "  iter 12  ": where the duration column (and the summary line) starts


def _line(width: int, *parts: tuple[str, str]) -> Text:
    """One console line from (text, style) parts, cut to `width` with an ellipsis."""
    line = Text(no_wrap=True, overflow="ellipsis")
    for text, style in parts:
        line.append(text, style=style)
    line.truncate(width, overflow="ellipsis")
    return line


def header_lines(config, run_id: str, label: str, max_iterations: int, width: int = 100) -> list[Text]:
    """`AutoHelix · run <short> · <label>` (the run's name, else the project's), then Goal, Agent, Gates, Observe, Scope and
    Budget lines (each only if it has something to say), each cut to `width`."""
    from autohelix.iteration_time import format_duration
    from autohelix.runs import short_id

    title = Text("  ")
    title.append("AutoHelix", style="bold")
    title.append(f" · run {short_id(run_id)} · {clean(label)}")
    agent = config.agent.backend + (f" ({config.agent.model})" if config.agent.model else "")
    if config.agent.timeout is not None:
        agent += f" · {format_duration(config.agent.timeout)} per iteration"
    observe = []
    for entry in config.observe:
        metrics = ", ".join(f"{name} {'↑' if direction == 'higher' else '↓'}"
                            for name, direction in entry.metrics.items())
        observe.append(f"{entry.name} ({metrics})" if metrics else entry.name)
    scope = []
    if config.editable:
        scope.append("editable: " + ", ".join(config.editable))
    if config.frozen:
        scope.append("frozen: " + ", ".join(config.frozen))
    budget = [f"{max_iterations} iteration{'s' * (max_iterations != 1)}"]
    if config.max_cost_usd is not None:
        cost = config.max_cost_usd
        budget.append(f"${cost:.0f}" if cost == int(cost) else f"${cost:.2f}")
    if config.max_time_seconds is not None:
        budget.append(format_duration(config.max_time_seconds))
    rows = [("Goal", config.title), ("Agent", agent), ("Gates", " · ".join(g.text for g in config.gates)),
            ("Observe", " · ".join(observe)), ("Scope", " · ".join(scope)), ("Budget", " · ".join(budget))]
    lines = [title]
    for label, value in rows:
        if value:
            lines.append(_line(max(width, 40), ("  ", ""), (f"{label:<9}", "dim"), (clean(value), "")))
    return lines


class Transcript:
    """The console lines for a run's evaluations, in fixed columns: iteration, duration,
    cost, each command metric with its change from the baseline, then the outcome
    (`✓ accepted`, or `✗` and the failing gate or reason) and review numbers as a
    change from the last accepted value (`missing 3 → 2`). Below a row, dimmed: the
    agent's summary and failed observe entries. Empty columns stay blank."""

    def __init__(self, config, width: int):
        self.directions = config.metric_directions()
        reviewed = {name for entry in config.observe if entry.review is not None for name in entry.metrics}
        self.columns = [name for name in self.directions if name not in reviewed]
        self.tracked = [name for name in self.directions if name in reviewed]
        self.width = max(width, 40)
        self.timeout = config.agent.timeout

    def _metrics(self, metrics: dict, baseline: dict | None) -> list[tuple[str, str]]:
        parts = []
        for name in self.columns:
            value = metrics.get(name)
            if not isinstance(value, (int, float)):
                parts.append((" " * (len(name) + 1 + 9 + 8), ""))
                continue
            delta, style = "", ""
            reference = (baseline or {}).get(name)
            if baseline is not None and isinstance(reference, (int, float)):
                delta = format_delta(value, reference).replace(" ", "")
                if delta:
                    better = value > reference if self.directions.get(name) != "lower" else value < reference
                    style = "green" if better else "red"
            parts.append((f"{name} {value:<9g}", ""))
            parts.append((f"{delta:<8}", style))
        return parts

    def baseline(self, record) -> list[Text]:
        """The baseline's line (its metrics, review numbers), and failed entries."""
        parts = [(f"{'  baseline':<{LABEL + 9 + 8}}", "dim"), *self._metrics(record.metrics, None)]
        tracked = self._tracked(record.metrics, {})
        if tracked:
            parts.append((tracked, "dim"))
        return [_line(self.width, *parts), *self._notes(record, summary=False)]

    def row(self, record, baseline: dict, previous: dict) -> list[Text]:
        """An iteration's two lines (more with failed observe entries). `baseline`: the
        baseline's metrics; `previous`: the last accepted values before this one."""
        from autohelix.formatting import format_duration

        wall_ms = record.usage.get("wall_clock_ms", record.usage.get("duration_ms"))
        duration = format_duration(wall_ms / 1000) if wall_ms else ""
        cost = record.cost.cost_usd
        cost_text = "" if cost is None else f"${cost:.2f}" + ("+" if record.cost.incomplete else "")
        parts = [(f"  {'iter ' + str(record.iteration):<{LABEL - 2}}", "dim"), (f"{duration:>7}  ", ""),
                 (f"{cost_text:>6}  ", ""), *self._metrics(record.metrics, baseline)]
        tracked = self._tracked(record.metrics, previous)
        used = sum(len(text) for text, _ in parts) + len(tracked) + 2
        if record.accepted:
            parts.append(("✓ accepted", "green"))
        else:
            reason = Text("✗ " + self._rejection(record), style="red", no_wrap=True)
            reason.truncate(max(self.width - used, 12), overflow="ellipsis")
            parts.append((reason.plain, "red"))
        if tracked:
            parts.append((tracked, "dim"))
        return [_line(self.width, *parts), *self._notes(record)]

    def _rejection(self, record) -> str:
        """`agent timed out (30m)`, `agent failed: <error>`, or the failing gate / reason."""
        error = getattr(record, "agent_error", None)
        if error is None:
            return outcome_reason(record.reason)
        if clean(error).startswith("timed out"):
            from autohelix.iteration_time import format_duration

            return f"agent timed out ({format_duration(self.timeout)})" if self.timeout else "agent timed out"
        return f"agent failed: {clean(error)}"

    def _tracked(self, metrics: dict, previous: dict) -> str:
        shown = []
        for name in self.tracked:
            value = metrics.get(name)
            if not isinstance(value, (int, float)):
                continue
            before = previous.get(name)
            shown.append(f"{name} {before:g} → {value:g}" if isinstance(before, (int, float)) and before != value
                         else f"{name} {value:g}")
        return ("  · " + " · ".join(shown)) if shown else ""

    def _notes(self, record, summary: bool = True) -> list[Text]:
        """The dim lines under a row: the agent's summary; at the baseline, gates that
        failed (they don't reject there, but every iteration will fail them); failed
        observe entries and metrics an entry didn't report; discarded paths."""
        lines = []
        indent = " " * LABEL
        if summary and record.summary:
            lines.append(_line(self.width, (indent + clean(record.summary), "dim")))
        if record.iteration == 0:
            failed_gates = [f"gate failed at the baseline: {clean(gate.get('text'))}"
                            for gate in record.gates if gate.get("kind") == "command" and gate.get("passed") is False]
            for note in failed_gates:
                lines.append(_line(self.width, (indent + note, "yellow")))
        failed = []
        for item in record.observe:
            if not item.get("ok", True):
                failed.append(f"observe {clean(item.get('name'))} failed ({clean(item.get('error'))})")
            elif item.get("metric_errors"):
                missing = ", ".join(clean(name) for name in item["metric_errors"])
                failed.append(f"observe {clean(item.get('name'))} reported no {missing}")
        if failed:
            lines.append(_line(self.width, (indent + " · ".join(failed), "dim")))
        if record.discarded:
            frozen = set(record.discarded_frozen)
            shown = ", ".join(clean(p) + (" (frozen)" if p in frozen else "") for p in record.discarded[:3])
            more = f", … ({len(record.discarded)} paths)" if len(record.discarded) > 3 else ""
            count = len(record.discarded)
            lines.append(_line(self.width, (f"{indent}discarded {count} out-of-scope change{'s' * (count != 1)}: "
                                            f"{shown}{more}", "dim")))
        return lines


def outcome_reason(reason: str | None) -> str:
    """A rejection as the row's status shows it: the failing gate's text, or the reason."""
    reason = clean(reason or "rejected")
    for prefix in ("gate failed: ", "metric gate: "):
        if reason.startswith(prefix):
            return reason.removeprefix(prefix)
    return reason
