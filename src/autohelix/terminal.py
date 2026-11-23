# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""What `run`, `resume` and bare `autohelix` print: the header (the mark beside a title and
labelled fields), one row per iteration under a column header, the live row of the
iteration under way, and the end block. Pure functions of their inputs, so the tests
render fixed fake runs (tests/snapshots/).

Colours are calm: the green ✓ and muted red ✗ glyphs, green/red changes on accepted
rows only, teal for what is live (spinner) and what to type next. Red text is kept
for real errors (an agent failure, a gate failing at the baseline, a run error)."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime

from rich.cells import cell_len
from rich.console import Group
from rich.text import Text

from autohelix.formatting import clean, format_duration
from autohelix.iteration_time import format_limit

GREEN = "#10b981"  # the logo's green: ✓, better
RED = "#d77a7a"  # muted: ✗, worse
TEAL = "#06b6d4"  # the single accent: the live spinner, next commands
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# The README mark (assets/autohelix-dark.svg) in braille, 12 columns × 6 lines, each
# cell in the colour of the logo's pixels under it ("-": blank).
_MARK = [
    ("  ⢠⠞⠛⣢⢖⠛⠳⡄  ", "- - 09b2d4 1ba0e2 2f8cee 13a9d3 08b6c2 08b6bc 0ab1d1 1e9ee3 - -"),
    ("⣠⠖⢸⠐⠚⠁⠈⠓⠒⡒⠲⣄", "0db893 0bb7a5 08b6bc 09b3d5 0caed8 0bb1d6 07b6cd 06b5cf 0dafd8 15abc0 209ce5 1d9ee2"),
    ("⢻⡠⠞      ⠳⣄⠟", "0ab7a3 0bb7a5 0cb79a - - - - - - 0db799 08b5c5 0dadd7"),
    ("⣴⠙⢦      ⡴⠊⣧", "08b5c7 0eacd8 2b91eb - - - - - - 2a90eb 2399e7 2497e7"),
    ("⠙⠦⠬⠤⢤⡀⢀⡤⠄⡇⠴⠋", "09b7af 0ab6aa 19a5cc 06b5c9 08b3d5 09b2d6 07b6cc 07b6c8 06b5cf 14a7dd 2398e6 2f8eee"),
    ("  ⠘⢦⣤⠵⠫⣤⡴⠃  ", "- - 0ab6ad 09b2ce 14a7dd 10aada 0cb1c2 0db792 09b6b0 07b5cf - -"),
]
MARK_WIDTH = 12
FIELD = 11  # "Observe    ": the width of a header field's label (and the end block's, at least)


def mark() -> list[Text]:
    """The mark's six lines."""
    lines = []
    for chars, colours in _MARK:
        line = Text()
        for char, colour in zip(chars, colours.split()):
            line.append(char, style=None if colour == "-" else f"#{colour}")
        lines.append(line)
    return lines


def cut(line: Text, width: int) -> Text:
    """`line` without trailing spaces, cut to `width` with an ellipsis."""
    line.rstrip()
    line.no_wrap = True
    line.truncate(max(width, 1), overflow="ellipsis")
    return line


def _parts(*parts: tuple[str, str | None]) -> Text:
    line = Text()
    for text, style in parts:
        line.append(text, style=style)
    return line


def field_line(label: str, *parts: tuple[str, str | None], width: int, label_width: int = 0,
               label_style: str | None = "dim") -> Text:
    """`  label  parts…`: one line of a labelled block (the end blocks), cut to `width`."""
    return cut(_parts(("  ", None), (f"{label:<{label_width or FIELD}}", label_style), *parts), width)


# --- wrapping and warnings: the same on every screen ------------------------------

MIN_TEXT = 20  # the fewest columns `hanging` wraps text into beside its prefix


def wrap(text, width: int) -> list[str]:
    """`text` in lines of at most `width` columns, broken at spaces only (its own line
    breaks kept, each line `clean`ed): a word (a path, a branch name, a command) is never
    split or cut; one wider than `width` has a line of its own, left whole for the
    terminal to wrap (`print_lines`)."""
    lines = []
    for paragraph in str(text if text is not None else "").splitlines() or [""]:
        line = ""
        for word in clean(paragraph).split(" "):
            if line and cell_len(line) + 1 + cell_len(word) > width:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}" if line else word
        lines.append(line)
    return lines


def hanging(prefix: Text | str, text, width: int, style: str | None = None, most: int | None = None) -> list[Text]:
    """`prefix` (a label, a mark: styled as given) then `text` in `style`, wrapped
    (`wrap`) to the rest of `width`, the continuation lines under the text's start. A
    prefix leaving under `MIN_TEXT` columns stands on its own line, the text under the
    prefix's start. With `most`, at most that many lines of text, the last ending in
    ` …` (words dropped, never split). Not cut: print with `print_lines`."""
    prefix = Text(prefix) if isinstance(prefix, str) else prefix
    lines = []
    if width - prefix.cell_len < MIN_TEXT and prefix.plain.strip():
        lines.append(Text().append_text(prefix))
        prefix = Text(" " * (len(prefix.plain) - len(prefix.plain.lstrip())))
    room = max(width - prefix.cell_len, MIN_TEXT)
    parts = wrap(text, room)
    if most and len(parts) > most:
        words = parts[most - 1].split(" ")
        while len(words) > 1 and cell_len(" ".join(words) + " …") > room:
            words.pop()
        parts = parts[:most - 1] + [" ".join(words) + " …"]
    blank = " " * prefix.cell_len
    first = not lines
    return lines + [(Text().append_text(prefix) if first and i == 0 else Text(blank)).append(part, style=style)
                    for i, part in enumerate(parts)]


def fold(line: Text, width: int) -> list[Text]:
    """`line` in pieces of at most `width` columns (a word wider than the screen split
    where the terminal would fold it): for screens that count their lines (`live`).
    Anything but a `Text` (a live row) is returned as it is."""
    if not isinstance(line, Text) or line.cell_len <= width or width < 1:
        return [line]
    offsets, used = [], 0
    for i, char in enumerate(line.plain):
        size = cell_len(char)
        if used + size > width:
            offsets.append(i)
            used = 0
        used += size
    return list(line.divide(offsets))


def warning(message, width: int, indent: int = 2) -> list[Text]:
    """A warning, the one form on every screen: `  ! message`, the `!` yellow, the
    message wrapped under itself (`hanging`)."""
    return hanging(Text(" " * indent).append("! ", style="yellow"), message, width)


def warn(console, message, indent: int = 2) -> None:
    """Print `warning(message)` at the console's width."""
    print_lines(console, warning(message, console.width, indent))


def show(console, text, prefix: Text | str = "", style: str | None = None) -> None:
    """Print `text` after `prefix`, wrapped at the console's width (`hanging`): what
    isn't a warning (an error line, a dim note)."""
    print_lines(console, hanging(prefix, text, console.width, style))


def rule(width: int) -> Text:
    return Text("  " + "─" * max(width - 4, 1), style="dim")


# --- numbers ------------------------------------------------------------------


def number(value) -> str:
    """A metric value with thousands separators: `17,481.5`, `120`, `0.1235`."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    if not math.isfinite(value) or (value and not 1e-4 <= abs(value) < 1e15):
        return f"{value:.3g}"
    if value == int(value):
        return f"{int(value):,}"
    decimals = 1 if abs(value) >= 100 else 3 if abs(value) >= 1 else 4
    return f"{value:,.{decimals}f}".rstrip("0").rstrip(".")


def change(value, reference) -> str:
    """How `value` compares with `reference`: `+17%`, `-0.4%`, `373×` (10 times or
    more), `0.05×` (a tenth or less); "" when unchanged or not comparable."""
    numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                  for v in (value, reference))
    if not numbers or reference == 0 or value == reference:
        return ""
    ratio = value / reference
    if ratio >= 10:
        return f"{ratio:.0f}×"
    if 0 < ratio <= 0.1:
        return f"{ratio:.2g}×"
    pct = (value - reference) / abs(reference) * 100
    return f"{pct:+.1f}%" if abs(pct) < 1 else f"{pct:+.0f}%"


def better(value, reference, direction: str | None) -> bool:
    return value < reference if direction == "lower" else value > reference


def money(cost: float | None, incomplete: bool = False) -> str:
    return "" if cost is None else f"${cost:.2f}" + "+" * incomplete


# --- the header -----------------------------------------------------------------


def header(title: list[tuple[str, str | None]], fields: list[tuple], width: int, show_mark: bool) -> list[Text]:
    """The title line (parts with styles), then `Label  value` fields (empty ones left
    out), beside the mark when `show_mark`, then a rule. A field is (label, value), cut
    to the width, or (label, value, style) to read whole (a path, a problem: style may
    be None), wrapped, the lines under the first unlabelled."""
    lines = [_parts(*title)]
    whole: set[int] = set()  # the wrapped lines: never cut (a word wider than the room stays whole)
    room = width - 2 - (MARK_WIDTH + 3) * show_mark
    for label, value, *style in fields:
        if not value:
            continue
        if not style:
            lines.append(_parts((f"{label:<{FIELD}}", "dim"), (clean(value), None)))
            continue
        wrapped = hanging(Text(f"{label:<{FIELD}}", style="dim"), value, room, style[0])
        whole.update(range(len(lines), len(lines) + len(wrapped)))
        lines += wrapped
    if show_mark:
        logo = mark()
        lines += [Text()] * max(len(logo) - len(lines), 0)
        lines = [_parts(("  ", None)).append_text(logo[i] if i < len(logo) else Text(" " * MARK_WIDTH))
                 .append("   ").append_text(line) for i, line in enumerate(lines)]
    else:
        lines = [Text("  ").append_text(line) for line in lines]
    return [line if i in whole else cut(line, width) for i, line in enumerate(lines)] + [rule(width)]


def shows_mark(console) -> bool:
    """Whether a header on `console` has the mark: a colour terminal 80 or more wide."""
    return console.is_terminal and console.color_system is not None and (console.width or 0) >= 80


def print_lines(console, lines) -> None:
    """Lines already cut to the width, printed as they are (a path longer than the
    width is left whole for the terminal to wrap)."""
    for line in lines:
        console.print(line, highlight=False, soft_wrap=True)


def title(*parts: str, version: str) -> list[tuple[str, str | None]]:
    """`AutoHelix  v0.3.0 · run krp3 · sorting · 14:02`: the name bold, the version dim."""
    shown = version if version == "dev" else f"v{version}"
    return [("AutoHelix", "bold"), (f"  {shown}", "dim"), *[(f" · {clean(part)}", None) for part in parts if part]]


def home_path(path: str) -> str:
    """A path with the home folder as `~` (`~/bin/claude-bedrock`)."""
    import os

    home = os.path.expanduser("~")
    return "~" + path[len(home):] if home not in ("", "/") and (path == home or path.startswith(home + "/")) else path


def agent_text(cli: str, model: str | None, executable: str | None = None) -> str:
    """The Agent field: `Claude Code · opus · via ~/bin/claude-bedrock`: the CLI, the
    model (`(its default)` without one), and the executable when it isn't what the CLI's
    plain name finds on PATH (`agents.plain_executable`)."""
    from autohelix.agents import CLI_NAMES, plain_executable

    parts = [CLI_NAMES.get(cli, cli), clean(model) if model else "(its default)"]
    if not plain_executable(cli, executable):
        parts.append(f"via {clean(home_path(executable))}")
    return " · ".join(parts)


def run_fields(config, budget: str, agent: str | None = None) -> list[tuple[str, str]]:
    """A run's header fields: Goal, Agent, Gates, Observe, Scope, Budget (`budget`:
    `budget_text`, the timeout added as `30m each`). `agent`: the Agent field
    (`agent_text`), else the configured CLI and model."""
    observe = []
    for entry in config.observe:
        metrics = ", ".join(f"{name} {'↓' if direction == 'lower' else '↑'}" for name, direction in entry.metrics.items())
        observe.append(f"{entry.name} ({metrics})" if metrics else entry.name)
    scope = []
    if config.editable:
        scope.append("editable: " + ", ".join(config.editable))
    if frozen := config.frozen_names():
        scope.append("frozen: " + ", ".join(frozen))
    if config.agent.timeout is not None:
        budget = " · ".join(filter(None, [budget, f"{format_limit(config.agent.timeout)} each"]))
    agent = agent or agent_text(config.agent.cli, config.agent.model)
    return [("Goal", config.title), ("Agent", agent),
            ("Gates", " · ".join(gate.text for gate in config.gates)), ("Observe", " · ".join(observe)),
            ("Scope", " · ".join(scope)), ("Budget", budget)]


def budget_text(budget, done: int) -> str:
    """The budget in effect for a segment after iteration `done` (a `harness.Budget`):
    `3 more iterations (to 8) · $5.00 more · 1h`, `no iteration cap · $5.00 more`; a new
    run says `3 iterations · $5.00`."""
    from autohelix.formatting import format_cap
    more = " more" if done else ""
    last = budget.last_iteration(done)
    if last is None:
        parts = ["no iteration cap"]
    else:
        n = max(last - done, 0)
        notes = ["default"] * budget.default + [f"to {last}"] * bool(done)
        parts = [f"{n}{more} iteration{'s' * (n != 1)}" + (f" ({', '.join(notes)})" if notes else "")]
    if budget.cost_usd is not None:
        parts.append(format_cap(budget.cost_usd) + more)
    if budget.time_seconds is not None:
        parts.append(format_limit(budget.time_seconds))
    return " · ".join(parts)


# --- rows -------------------------------------------------------------------------


@dataclass
class Row:
    """What a row shows of one iteration (the baseline is 0)."""

    iteration: int
    accepted: bool
    metrics: dict = field(default_factory=dict)
    seconds: float | None = None
    cost: float | None = None
    cost_incomplete: bool = False
    commit: str | None = None  # the commit an accepted iteration made (None: a no-op)
    reason: str | None = None
    agent_error: str | None = None
    summary: str | None = None
    gates: list = field(default_factory=list)
    observe: list = field(default_factory=list)
    stops: list = field(default_factory=list)
    discarded: list = field(default_factory=list)
    discarded_frozen: list = field(default_factory=list)

    @classmethod
    def of(cls, record) -> "Row":
        """The row of a history record (`history.IterationResult`)."""
        wall_ms = record.usage.get("wall_clock_ms", record.usage.get("duration_ms"))
        cost = record.cost
        return cls(iteration=record.iteration, accepted=record.accepted, metrics=record.metrics,
                   seconds=wall_ms / 1000 if wall_ms else None, cost=cost.cost_usd, cost_incomplete=cost.incomplete,
                   commit=record.commit, reason=record.reason, agent_error=record.agent_error,
                   summary=record.summary, gates=record.gates, observe=record.observe, stops=record.stops,
                   discarded=record.discarded, discarded_frozen=record.discarded_frozen)


LABEL = 6  # "✓ 12  "
TIME = 7  # "30m 00s"
COST = 6  # "$12.34"
CHANGE = 6  # "0.083×"
INDENT = 2 + LABEL + 2  # where the time column starts: notes and long reasons go under it
# Space between the columns after the label (time, cost, each metric, the reason) and
# between a value and its change: the widest that the width allows, else compact.
WIDE, SPACED, COMPACT = (6, 2), (4, 2), (2, 1)
REASON_ROOM = 45  # wider only if a typical reason (a metric gate's, ~43) still fits on the row after the values
SPACED_FROM = 100  # and never narrower (80 columns: always compact)
ROOM = 2  # blank lines under a live row
REASON_LINES = 3  # a row's reason or note, wrapped: at most this many lines (an agent's whole error is long)


class Table:
    """The rows of a run, in fixed columns: outcome glyph and number, time, cost, then
    each metric's value and its change from the baseline, then why it was rejected.

    Stateful, as rows arrive: `add` returns a row's lines (the column header before the
    first), and keeps the baseline and each metric's tip value (the value a metric gate
    compares with) for later rows and the end block. Columns that don't fit the width
    are dropped: cost first, then the changes. The columns get the widest spacing
    (`WIDE`, then `SPACED`) that leaves `REASON_ROOM` after them on a terminal
    `SPACED_FROM` wide or more, else compact (80 columns, many metrics)."""

    def __init__(self, directions: dict[str, str], width: int, gates: list = (), timeout: float | None = None):
        """`directions`: metric -> "higher"/"lower", in column order; `gates`: the
        config's gates (a metric gate's rejection names its threshold); `timeout`: the
        agent's (a timed-out row says it)."""
        self.directions = dict(directions)
        self.gates = list(gates)
        self.width = max(width, 40)
        self.timeout = timeout
        self.heads = [f"{name} {'↓' if direction == 'lower' else '↑'}" for name, direction in self.directions.items()]
        self.value_width = {name: max(len(head), 8) for name, head in zip(self.directions, self.heads)}
        self.show_cost = self.show_change = True
        for self.gap, self.sep in (WIDE, SPACED, COMPACT):
            if self.width >= SPACED_FROM and self._metrics_end() + REASON_ROOM <= self.width:
                break
        if self._metrics_end() > self.width:
            self.show_cost = False
        if self._metrics_end() > self.width:
            self.show_change = False
        self.rows: list[Row] = []
        self.baseline: dict = {}
        self.tip: dict[str, tuple[float, int]] = {}  # metric -> (value, iteration that measured it)
        self._headed = False

    @classmethod
    def of(cls, config, width: int) -> "Table":
        """The table of a run of `config` (`config.Config`)."""
        return cls(config.metric_directions(), width, config.gates, config.agent.timeout)

    def _metrics_start(self) -> int:
        return 2 + LABEL + 2 + TIME + (self.gap + COST) * self.show_cost + self.gap

    def _metrics_end(self) -> int:
        """The column where the reason starts on a row with values."""
        return self._metrics_start() + sum(w + (self.sep + CHANGE) * self.show_change + self.gap
                                           for w in self.value_width.values())

    def column_header(self) -> Text:
        """Each metric's name ends where its values end; its arrow hangs into the space
        after them (the change column, else the next gap: never narrower than the arrow)."""
        gap = " " * self.gap
        line = Text(" " * (2 + LABEL + 2) + f"{'time':>{TIME}}" + f"{gap}{'cost':>{COST}}" * self.show_cost, style="dim")
        after = (self.sep + CHANGE) * self.show_change  # the space after a metric's values
        hung = 0  # how much of this gap the previous arrow took
        for name, head in zip(self.directions, self.heads):
            arrow = head[len(name):]
            line.append(" " * (self.gap - hung) + f"{name:>{self.value_width[name]}}" + arrow.ljust(after), style="dim")
            hung = max(len(arrow) - after, 0)
        return cut(line, self.width)

    def heading(self) -> list[Text]:
        """The column header, the first time only."""
        lines = [] if self._headed else [self.column_header()]
        self._headed = True
        return lines

    def add(self, row: Row) -> list[Text]:
        """`row`'s lines: the row, its reason when it doesn't fit at the end, then dim notes."""
        lines = self.heading()
        if row.iteration == 0:
            glyph, label = ("·", "dim"), ("base", "dim")
        elif row.accepted:
            glyph, label = ("✓", GREEN), (str(row.iteration), None)
        else:
            glyph, label = ("✗", RED), (str(row.iteration), None)
        line = _parts(("  ", None), (glyph[0] + " ", glyph[1]), (f"{label[0]:<{LABEL - 2}}", label[1]), ("  ", None),
                      (f"{format_duration(row.seconds) if row.seconds else '':>{TIME}}", None))
        gap = " " * self.gap
        if self.show_cost:
            line.append(f"{gap}{money(row.cost, row.cost_incomplete):>{COST}}")
        dim = not row.accepted and row.iteration > 0
        for name in self.directions:
            value, reference = row.metrics.get(name), self.baseline.get(name)
            shown = number(value) if isinstance(value, (int, float)) else ""
            line.append(f"{gap}{shown:>{self.value_width[name]}}", style="dim" if dim else "bold")
            if self.show_change:
                # Rejected rows show no change: their reason says how they compare with the tip.
                delta = change(value, reference) if row.iteration and row.accepted else ""
                style = "dim" if dim or not delta else GREEN if better(value, reference, self.directions[name]) else RED
                line.append(" " * self.sep)
                line.append(f"{delta:<{CHANGE}}", style=style)
        reason, style = self._reason(row)
        line.rstrip()
        # After the values, or where they would be when there are none (rejected before
        # anything was measured); on the next line when it doesn't fit.
        start = self._metrics_end() if line.cell_len > self._metrics_start() else self._metrics_start()
        if reason and start + len(reason) <= self.width:
            line.append(" " * (start - line.cell_len))
            line.append(reason, style=style)
            reason = None
        lines.append(cut(line, self.width))
        if reason:
            lines += hanging(" " * INDENT, reason, self.width, style, most=REASON_LINES)
        for note, style in self._notes(row):
            lines += hanging(" " * INDENT, note, self.width, style, most=REASON_LINES)
        self._remember(row)
        return lines

    def earlier(self, rows: list[Row], short: str, now: datetime | None = None, shown: int = 5) -> list[Text]:
        """A resumed run's rows so far: the baseline and the last `shown` iterations
        (`… N earlier iterations` for the others), then `resumed HH:MM`."""
        hidden = [row.iteration for row in rows if row.iteration > 0][:-shown]
        lines = self.heading()
        for row in rows:
            added = self.add(row)
            if row.iteration not in hidden:
                lines += added
            if hidden and row.iteration == hidden[-1]:
                lines.append(earlier_line(len(hidden), short))
        return lines + [Text(f"  resumed {clock(now)}", style="dim")]

    def _remember(self, row: Row) -> None:
        self.rows.append(row)
        if row.iteration == 0 and row.accepted:
            self.baseline = dict(row.metrics)
        if row.accepted and (row.commit or row.iteration == 0):  # as `checks.metric_reference`
            for name, value in row.metrics.items():
                if isinstance(value, (int, float)):
                    self.tip[name] = (value, row.iteration)

    def _reason(self, row: Row) -> tuple[str, str | None]:
        """Why a row was rejected, and its style (red for an agent failure)."""
        if row.accepted or row.iteration == 0:
            return "", None
        if row.agent_error is not None:
            if clean(row.agent_error).startswith("timed out"):
                return ("agent timed out" + (f" ({format_limit(self.timeout)})" if self.timeout else ""), RED)
            return f"agent failed: {clean(row.agent_error)}", RED
        return rejection(row, self.gates, self.directions, self.tip), None

    def _notes(self, row: Row) -> list[tuple[str, str | None]]:
        """Under a row: gates failing at the baseline (red: every iteration will fail
        them), failed observe entries and metrics an entry didn't report, stop commands
        that failed (neither 0 nor 1: the condition wasn't checked), discarded paths."""
        notes = []
        if row.iteration == 0:
            notes += [(f"gate fails at the baseline: {clean(gate.get('text'))}", RED) for gate in row.gates
                      if isinstance(gate, dict) and gate.get("kind") == "command" and gate.get("passed") is False]
        for item in row.observe:
            if not isinstance(item, dict):
                continue
            if item.get("ok") is False:
                error = clean(item.get("error"))
                notes.append((f"observe {clean(item.get('name'))} failed" + (f": {error}" if error else ""), "dim"))
            elif item.get("metric_errors"):
                missing = ", ".join(clean(name) for name in item["metric_errors"])
                notes.append((f"observe {clean(item.get('name'))} reported no {missing}", "dim"))
        for k, item in enumerate(row.stops, start=1):
            if isinstance(item, dict) and item.get("outcome") == "error":
                code = item.get("return_code")
                problem = ("timed out" if item.get("timed_out") is True else "could not run" if code == -1
                           else f"exited {clean(code)}")
                notes.append((f"stop command {problem}, not checked: {clean(item.get('text'))} (stop/{k}/)", "yellow"))
        if row.discarded:
            frozen = set(row.discarded_frozen)
            shown = ", ".join(clean(p) + " (frozen)" * (p in frozen) for p in row.discarded[:3])
            more = f", … {len(row.discarded) - 3} more" if len(row.discarded) > 3 else ""
            notes.append((f"discarded, outside the scope: {shown}{more}", "dim"))
        return notes

    def live(self, iteration: int, seconds: float, what: str, frame: int = 0) -> Text:
        """The row of the iteration under way: spinner, number, time so far, the phase
        (`agent`, `observe · bench`) right after the time."""
        return live_line("base" if iteration == 0 else str(iteration), seconds, what, frame, self.width)

    # --- the end block ---

    def _label_width(self, *labels: str) -> int:
        """The end block's label column: the widest label (metric names included) + 2."""
        return max(FIELD, max(len(label) for label in [*labels, *self.directions]) + 2)

    def _wrapped(self, label: str, text: str, style: str | None, label_style: str, width: int) -> list[Text]:
        """A labelled end-block line wrapped, never cut: an error must be readable whole."""
        return hanging(_parts(("  ", None), (f"{label:<{width}}", label_style)), text, self.width, style)

    def outcome_lines(self, outcome: tuple[str, str, str | None], width: int = 0) -> list[Text]:
        """The end block's first line (`Done`, `Stopped`, `Interrupted`, `Error`: word,
        detail, style), wrapped; the detail is red for an error, else plain."""
        word, detail, style = outcome
        return self._wrapped(word, detail, style if style == RED else None, f"bold {style}" if style else "bold",
                             width or self._label_width(word))

    def ending(self, outcome: tuple[str, str, str | None], *, spent: str, landing: tuple[str, str, str | None] | None,
               commands: list[str], files: list[tuple[str, str]], folder: str = "", target: str = ""
               ) -> list[Text]:
        """After a rule: the outcome, each metric's baseline → tip value with its change,
        the run's totals, what landed (`landing`: label, text, style; the commits'
        summaries are on the run page), then what to type next (one command per line,
        with what it does: `command_hint`, `target` the branch the run lands on), the
        run's `folder` (`.autohelix/runs/<id>/`), and the files
        written (label, path)."""
        iterations = [row for row in self.rows if row.iteration > 0]
        accepted = sum(row.accepted for row in iterations)
        seconds = sum(row.seconds or 0 for row in iterations)
        width = self._label_width(outcome[0], "summary", "unlanded", "landing")

        def field(label: str, *parts: tuple[str, str | None], style: str | None = "dim") -> Text:
            return field_line(label, *parts, width=self.width, label_width=width, label_style=style)

        lines = [Text(), rule(self.width), *self.outcome_lines(outcome, width)]
        for name, direction in self.directions.items():
            base, (tip, where) = self.baseline.get(name), self.tip.get(name, (None, None))
            if tip is None:
                continue
            parts = [(number(base), None)] if base is not None else []
            if where and tip != base:
                delta = change(tip, base)
                parts = [*parts, (" → ", "dim"), (number(tip), "bold")] if base is not None else [(number(tip), "bold")]
                if delta:
                    parts.append((f"  {delta}", GREEN if better(tip, base, direction) else RED))
                parts.append((f"  iter {where}", "dim"))
            lines.append(field(name, *parts))
        totals = [f"{len(iterations)} iteration{'s' * (len(iterations) != 1)}", f"{accepted} accepted"]
        lines.append(field("run", (" · ".join(totals + [format_duration(seconds)] * bool(seconds)
                                                   + [spent] * bool(spent)), None)))
        if landing:
            label, text, landing_style = landing
            lines += self._wrapped(label, text, landing_style, "dim", width)
        lines += command_lines(Text(f"  {'next':<{width}}", style="dim"), " " * (2 + width),
                               [(command, command_hint(command, target)) for command in commands], self.width)
        if folder:  # not cut: a path is only useful whole
            lines.append(_parts(("  ", None), (f"{'folder':<{width}}", "dim"), (clean(folder), None)))
        for label, path in files:  # not cut: a path is only useful whole
            lines.append(_parts(("  ", None), (f"{label:<{width}}", "dim"), (clean(path), None)))
        return lines


def live_line(label: str, seconds: float, what: str, frame: int = 0, width: int = 200) -> Text:
    """A live row: spinner, label (an iteration's number; blank for the summary step),
    time so far in the time column, then the phase two spaces after it: with the time,
    not under a column (the cost and values aren't known yet)."""
    return cut(_parts(("  ", None), (SPINNER[frame % len(SPINNER)] + " ", TEAL), (f"{label:<{LABEL - 2}}", None),
                      ("  ", None), (f"{format_duration(seconds):>{TIME}}", None), ("  ", None), (clean(what), "dim")),
               width)


def with_room(renderable, under: list[Text] = ()) -> Group:
    """`renderable` (then a blank line and the lines `under` it) with `ROOM` blank lines
    under it: a live row shown at the bottom of the terminal doesn't sit on its last
    lines (gone with the live display)."""
    return Group(renderable, *[Text(), *under] * bool(under), *[Text() for _ in range(ROOM)])


def steer_commands(short: str, view: bool = True) -> list[tuple[str, str]]:
    """The commands that steer a running run from another terminal, each with what it
    does: look at it (`view`: not from the view itself), send its agent a hint, stop it
    after the current iteration."""
    return ([(f"autohelix view {short}", "watch it in your browser")] if view else []) + [
        (f'autohelix hint --run {short} "…"', "tell the agent something (next iteration)"),
        (f"autohelix stop {short}", "stop after this iteration")]


def steer_text(short: str) -> str:
    """The steering commands on one line (a piped run's `Steer` header field)."""
    return " · ".join(command for command, _ in steer_commands(short))


def steer_lines(short: str, width: int, indent: str = "  ", view: bool = True,
                ctrl_c: str = "stops now (accepted work is kept)") -> list[Text]:
    """What steers a running run, under its live row (`run`/`resume`: the header has
    long scrolled away; `view --terminal`, without `view`): `from another terminal:`,
    the commands with what they do (`command_lines`), then what Ctrl-C does here."""
    return [cut(Text(f"{indent}from another terminal:", style="dim"), width),
            *command_lines(Text(indent + "  "), indent + "  ", steer_commands(short, view), width),
            cut(Text(f"{indent}Ctrl-C here {ctrl_c}", style="dim"), width)]


def command_lines(first: Text, rest: str, commands: list[tuple[str, str]], width: int) -> list[Text]:
    """Commands one per line, after `first` (the next ones after `rest`, as wide), each
    with what it does, dim, in one column after the widest `autohelix` command (a longer
    command, `git merge autohelix/<id>`, has its hint after it). The hints are left out
    when one doesn't fit; a command is cut only past the terminal's width."""
    gap = 4
    column = max((len(c) for c, _ in commands if c.startswith("autohelix ")),
                 default=max((len(c) for c, _ in commands), default=0))
    ends = [max(column, len(c)) + gap + len(hint) for c, hint in commands if hint]
    hints = first.cell_len + max(ends, default=0) <= width
    lines = []
    for i, (command, hint) in enumerate(commands):
        line = first.copy() if i == 0 else Text(rest)
        line.append(command, style=TEAL)
        if hints and hint:
            line.append(" " * (max(column, len(command)) - len(command) + gap)).append(hint, style="dim")
        lines.append(cut(line, width))
    return lines


def outcome(stop: dict | None, error: BaseException | None, interrupted_at: int | None) -> tuple[str, str, str | None]:
    """The end block's first line: (word, detail, style). `Error` (red) for an
    exception, `Interrupted` (yellow) for Ctrl-C or `autohelix stop --now`, `Done` for
    a budget or a stop condition, `Stopped` for `autohelix stop` (red when the agent
    kept failing or failed for its infrastructure). `stop`: the status's stop reason."""
    stop = stop or {}
    if isinstance(error, Exception):
        return "Error", clean(error) or type(error).__name__, RED
    if error is not None or interrupted_at is not None:
        by = "by autohelix stop --now " if stop.get("kind") == "request" and stop.get("now") else ""
        where = f"at iteration {interrupted_at}" if interrupted_at else "at the baseline"
        return "Interrupted", f"{by}{where} · progress saved", "yellow"
    word = "Done" if stop.get("kind") in ("budget", "condition") else "Stopped"
    return word, stop.get("detail") or "", RED if stop.get("kind") in ("agent", "infrastructure") else None


def rejection(row: Row, gates: list, directions: dict, tip: dict[str, tuple[float, int]]) -> str:
    """Why `row` was rejected, in the words every screen uses: the failing gate
    (`pytest -q failed (exit 1)`, `ask: keeps the API? answered no`), a metric gate as `speed 6.5% worse than iter 3
    (5% allowed)` (with the config's `gates`), else the harness's reason without its
    `gate failed:` prefix."""
    gates = list(gates) if len(row.gates) == len(gates) else [None] * len(row.gates)
    for item, gate in zip(row.gates, gates):
        if not isinstance(item, dict) or item.get("passed") is not False:
            continue
        if item.get("kind") == "metric":
            if hasattr(gate, "metric") and (text := _metric_rejection(gate, item, tip, directions.get(gate.metric))):
                return text
        elif item.get("kind") == "command" and not item.get("reason"):
            return failed_gate(clean(item.get("text")), item.get("return_code"), item.get("timed_out") is True)
    reason = clean(row.reason or "rejected")
    for prefix in ("gate failed: ", "metric gate: "):
        reason = reason.removeprefix(prefix)
    return reason


def failed_gate(text: str, code, timed_out: bool = False) -> str:
    """A failed command gate: `pytest -q failed (exit 1)`, `pytest -q timed out`; an
    `ask:` gate answering no (exit 1) `ask: keeps the API? answered no`."""
    if timed_out:
        return f"{text} timed out"
    if text.startswith("ask:") and code == 1:
        return f"{text} answered no"
    exit_code = isinstance(code, int) and not isinstance(code, bool) and code > 0
    return f"{text} failed" + (f" (exit {code})" if exit_code else "")


def earlier_line(hidden: int, short: str | None = None) -> Text:
    """`… 4 earlier iterations · autohelix view krp3`: rows left out above."""
    more = f" · autohelix view {short}" if short else ""
    return Text(f"  … {hidden} earlier iteration{'s' * (hidden != 1)}{more}", style="dim")


def commits(n: int) -> str:
    return f"{n} commit{'s' * (n != 1)}"


def landed(count: int, target: str) -> tuple[str, str, str | None]:
    """The end block's landing line (label, text, style): `landed  3 commits on main`,
    `landed  nothing (no accepted changes)`."""
    return ("landed", f"{commits(count)} on {target}", None) if count else ("landed", "nothing (no accepted changes)", "dim")


def unlanded(count: int, branch: str, note: str = "", style: str | None = None) -> tuple[str, str, str | None]:
    """`unlanded  1 commit on autohelix/<id> (manual landing)`."""
    return "unlanded", f"{commits(count)} on {branch}" + (f" ({note})" if note else ""), style


def not_landed(error) -> tuple[str, str, str | None]:
    """`landing  failed: <why>` (yellow): the run's commits stay on its branch."""
    return "landing", f"failed: {clean(error)}", "yellow"


def next_commands(short: str, view: bool = True, also=()) -> list[str]:
    """What to type after a run: look at it (`view`: not from the view itself), continue
    it, then `also`: what lands commits that wait (`runs.LandingWords.commands`:
    `autohelix land <id>` when the target can fast-forward, `git merge autohelix/<id>`
    when it diverged)."""
    return ([f"autohelix view {short}"] if view else []) + [f"autohelix resume {short}", *also]


def command_hint(command: str, target: str = "") -> str:
    """What a command `next_commands` suggests does, in a few words: the end block's
    column after it. The landing commands are `runs.landing_words`' three shapes;
    `target`, the branch they land on (else `the branch`)."""
    branch = target or "the branch"
    if command.startswith("autohelix view "):
        return "the run's page in your browser"
    if command.startswith("autohelix resume "):
        return "one more iteration (-n N for more)"
    if command.startswith("autohelix land "):
        return f"put its commits on {branch}"
    if command.startswith("git merge "):
        return f"merge its commits into {branch} by hand"
    if command.startswith("git cherry-pick "):
        return f"apply its newer commits to {branch} by hand"
    return ""


def shown_path(path) -> str:
    """A file the end block names: relative to the current folder when inside it."""
    from pathlib import Path

    try:
        return str(Path(path).relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _metric_rejection(gate, item: dict, tip: dict, direction: str | None) -> str | None:
    value, reference = item.get("value"), item.get("reference")
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (value, reference)) or not reference:
        return None
    where = tip.get(gate.metric, (None, None))[1]
    against = "the baseline" if where == 0 else f"iter {where}" if where else number(reference)
    gain = reference - value if direction == "lower" else value - reference  # > 0: better
    pct = abs(gain) / abs(reference) * 100
    if gate.mode == "regress":
        return f"{gate.metric} {pct:.1f}% worse than {against} ({gate.threshold_pct:g}% allowed)"
    if gain <= 0:
        return f"{gate.metric} no better than {against}"
    return f"{gate.metric} only {pct:.1f}% better than {against} ({gate.threshold_pct:g}% needed)"


class LiveRow:
    """The iteration under way as a live renderable (`Table.live`): its time and the
    spinner's frame come from `clock` when it is drawn, so it moves at the display's
    rate whatever builds the screen. One clock from `start` (default: now), the phase
    (`agent`, `gates`, `observe`, `stop`), and for `observe` the entry (`observe · bench`)."""

    def __init__(self, table: Table, iteration: int, start: float | None = None, clock=time.monotonic):
        self.table, self.clock = table, clock
        self.restart(iteration, start)

    def restart(self, iteration: int, start: float | None = None) -> None:
        self.iteration, self.start = iteration, self.clock() if start is None else start
        self.phase, self.detail = "agent" if iteration else "gates", ""

    def set(self, phase: str, detail: str = "") -> None:
        self.phase, self.detail = phase, detail

    def line(self, now: float | None = None) -> Text:
        """The row at `now` (by `clock`; default: now)."""
        now = self.clock() if now is None else now
        what = f"{self.phase} · {self.detail}" if self.detail else self.phase
        return self.table.live(self.iteration, max(now - self.start, 0), what, int(now * 10))

    def __rich_console__(self, console, options):
        yield self.line()


def clock(now: datetime | None = None) -> str:
    return f"{now or datetime.now():%H:%M}"


def version() -> str:
    """The installed package's version, or `dev`."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as package_version

    try:
        return package_version("autohelix")
    except PackageNotFoundError:
        return "dev"
