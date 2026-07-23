"""Tests for autohelix.display module."""

import pytest
from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.text import Text

from autohelix.agents import AgentEvent, AgentEventKind
from autohelix.display import LiveDisplay, event_presenter, truncate_for_display


# ---------------------------------------------------------------------------
# truncate_for_display
# ---------------------------------------------------------------------------


class TestTruncateForDisplay:
    def test_short_text_unchanged(self):
        assert truncate_for_display("hello world") == "hello world"

    def test_collapses_whitespace(self):
        assert truncate_for_display("hello\n  world\tfoo") == "hello world foo"

    def test_long_text_truncated_with_ellipsis(self):
        text = "a" * 300
        result = truncate_for_display(text, limit=100)
        assert len(result) == 100
        assert result.startswith("...")
        assert result.endswith("a" * 97)

    def test_custom_limit(self):
        text = "x" * 50
        result = truncate_for_display(text, limit=20)
        assert len(result) == 20
        assert result[:3] == "..."

    def test_exact_limit_not_truncated(self):
        text = "a" * 220
        assert truncate_for_display(text) == text

    def test_empty_string(self):
        assert truncate_for_display("") == ""


# ---------------------------------------------------------------------------
# event_presenter
# ---------------------------------------------------------------------------


class TestEventPresenter:
    def test_thinking(self):
        event = AgentEvent(kind=AgentEventKind.THINKING, text="hmm")
        assert event_presenter(event) == ("thinking", "magenta")

    def test_assistant_text(self):
        event = AgentEvent(kind=AgentEventKind.ASSISTANT_TEXT, text="hello")
        assert event_presenter(event) == ("assistant", "green")

    def test_tool_use(self):
        event = AgentEvent(kind=AgentEventKind.TOOL_USE, text="Read")
        assert event_presenter(event) == ("tool", "cyan")

    def test_tool_input_with_name(self):
        event = AgentEvent(kind=AgentEventKind.TOOL_INPUT, text="...", metadata={"tool_name": "Grep"})
        label, style = event_presenter(event)
        assert label == "Grep input"
        assert style == "cyan"

    def test_tool_input_without_name(self):
        event = AgentEvent(kind=AgentEventKind.TOOL_INPUT, text="...")
        label, _ = event_presenter(event)
        assert label == "tool input"

    def test_tool_result_success(self):
        event = AgentEvent(kind=AgentEventKind.TOOL_RESULT, text="ok")
        _, style = event_presenter(event)
        assert style == "yellow"

    def test_tool_result_error(self):
        event = AgentEvent(kind=AgentEventKind.TOOL_RESULT, text="fail", metadata={"is_error": True})
        _, style = event_presenter(event)
        assert style == "red"

    def test_warning(self):
        event = AgentEvent(kind=AgentEventKind.WARNING, text="warn")
        assert event_presenter(event) == ("warning", "yellow")

    def test_error(self):
        event = AgentEvent(kind=AgentEventKind.ERROR, text="bad")
        assert event_presenter(event) == ("error", "red")

    def test_result(self):
        event = AgentEvent(kind=AgentEventKind.RESULT, text="done")
        assert event_presenter(event) == ("result", "dim")

    def test_status(self):
        event = AgentEvent(kind=AgentEventKind.STATUS, text="running")
        assert event_presenter(event) == ("status", "dim")

    def test_unknown_string_kind(self):
        event = AgentEvent(kind="custom_event", text="x")
        label, style = event_presenter(event)
        assert label == "custom event"
        assert style == "yellow"


# ---------------------------------------------------------------------------
# LiveDisplay
# ---------------------------------------------------------------------------


class TestLiveDisplay:
    def _make_display(self, **kwargs):
        console = Console(force_terminal=True, width=120)
        return LiveDisplay(console=console, title="Test Agent", **kwargs)

    def test_render_returns_renderable(self):
        d = self._make_display()
        result = d.render(0)
        # Compact mode returns Text, verbose returns Panel
        assert isinstance(result, (Text, Panel))

    def test_verbose_render_shows_title(self):
        d = self._make_display(verbose=True)
        panel = d.render(0)
        assert isinstance(panel, Panel)
        assert panel.title is not None

    def test_render_elapsed_formatting(self):
        d = self._make_display()
        # Render at 65 seconds -> "1m 05s"
        d.render(65)
        # Just ensure it doesn't crash; the elapsed is stored internally

    def test_handle_event_increments_counters(self):
        d = self._make_display()
        d.handle_event(AgentEvent(kind=AgentEventKind.ASSISTANT_TEXT, text="hi"))
        assert d._state["events"] == 1
        assert d._state["tools"] == 0

    def test_handle_tool_use_increments_tools(self):
        d = self._make_display()
        d.handle_event(AgentEvent(kind=AgentEventKind.TOOL_USE, text="Read"))
        assert d._state["tools"] == 1

    def test_stream_then_final(self):
        d = self._make_display()
        d.handle_event(AgentEvent(
            kind=AgentEventKind.ASSISTANT_TEXT, text="partial",
            metadata={"stream": True},
        ))
        assert d._state["active_text"] == "partial"
        assert len(d._state["entries"]) == 0

        d.handle_event(AgentEvent(
            kind=AgentEventKind.ASSISTANT_TEXT, text="full message",
            metadata={"final": True},
        ))
        assert len(d._state["entries"]) == 1
        assert d._state["active_text"] == ""

    def test_flush_moves_active_to_entries(self):
        d = self._make_display()
        d.handle_event(AgentEvent(
            kind=AgentEventKind.THINKING, text="pondering",
            metadata={"stream": True},
        ))
        assert len(d._state["entries"]) == 0
        d.flush()
        assert len(d._state["entries"]) == 1
        assert d._state["active_label"] is None

    def test_flush_noop_when_nothing_active(self):
        d = self._make_display()
        d.flush()  # should not crash
        assert len(d._state["entries"]) == 0

    def test_max_entries_respected(self):
        d = self._make_display(max_entries=3)
        for i in range(5):
            d.handle_event(AgentEvent(kind=AgentEventKind.STATUS, text=f"msg {i}"))
        assert len(d._state["entries"]) == 3

    def test_handle_event_safe_on_bad_event(self):
        """Rendering errors should not propagate."""
        d = self._make_display(verbose=True)
        # Create an event with a kind that is not a string (edge case)
        event = AgentEvent(kind=AgentEventKind.ASSISTANT_TEXT, text="ok")
        # Override metadata to something that causes issues
        event.metadata = None  # type: ignore[assignment]
        # Should not raise
        d.handle_event(event)

    def test_border_style_and_title_customizable(self):
        d = self._make_display(border_style="magenta", verbose=True)
        panel = d.render(0)
        assert isinstance(panel, Panel)
        assert panel.border_style is not None

    def test_render_with_no_elapsed_uses_stored(self):
        d = self._make_display()
        d.render(42)
        result = d.render()  # no elapsed arg -> reuse 42
        assert isinstance(result, (Text, Panel))

    def test_compact_mode_returns_text(self):
        """Compact mode renders a single Text line (no Panel)."""
        d = self._make_display(verbose=False)
        d.handle_event(AgentEvent(kind=AgentEventKind.TOOL_USE, text="Read"))
        d.handle_event(AgentEvent(kind=AgentEventKind.ASSISTANT_TEXT, text="hello"))
        result = d.render(90)
        assert isinstance(result, Text)

    def test_verbose_mode_uses_group(self):
        """Verbose mode renders a Panel with multiple lines (Group)."""
        from rich.console import Group
        d = self._make_display(verbose=True)
        d.handle_event(AgentEvent(kind=AgentEventKind.TOOL_USE, text="Read"))
        panel = d.render(10)
        assert isinstance(panel, Panel)
        assert isinstance(panel.renderable, Group)

    def test_compact_mode_shows_iteration_and_time(self):
        """Compact mode shows iteration label and elapsed time."""
        d = self._make_display(verbose=False, iteration=3)
        result = d.render(65)
        assert isinstance(result, Text)
        plain = result.plain
        assert "iter 3" in plain
        assert "1m" in plain


