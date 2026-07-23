# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Live display rendering for agent and reviewer sessions."""

import time as _time
from collections import deque

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.panel import Panel
from rich.text import Text

from autohelix.agents import AgentEvent, AgentEventKind


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

    def render(self, elapsed: int | None = None) -> RenderableType:
        """Render the live status."""
        if elapsed is not None:
            self._elapsed = elapsed
        else:
            self._elapsed = int(_time.monotonic() - self._start_time)
        if self.verbose:
            return self._render_verbose()
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

    def _render_verbose(self) -> Panel:
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

        return Panel(Group(*body), title=self.title, border_style=self.border_style)


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
