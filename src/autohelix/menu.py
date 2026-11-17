# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Arrow-key menus for bare `autohelix` (the entry screen), with a numbered prompt as
the fallback: `choose` (one of several options) and `confirm` (Enter or q)."""

from __future__ import annotations

import os
import shutil
import sys

import click

Option = tuple[str, str]  # label, dimmed detail ("" for none)

_UP = (b"\x1b[A", b"\x1bOA", b"k")
_DOWN = (b"\x1b[B", b"\x1bOB", b"j")
_ENTER = (b"\r", b"\n")
_LEAVE = (b"", b"\x1b", b"\x03", b"\x04")  # EOF, Esc, Ctrl-C, Ctrl-D
_QUIT = (b"q", b"Q")
QUIT_HINT = "(q: quit)"


class Quit(Exception):
    """q pressed: the user is done (exit 0), unlike Esc or Ctrl-C (`click.Abort`)."""


def step(key: bytes, current: int, count: int) -> tuple[str, int]:
    """What a key press does: ("move", i), ("pick", i), ("quit", -1), ("leave", -1) or
    ("stay", current)."""
    if key in _UP:
        return "move", max(current - 1, 0)
    if key in _DOWN:
        return "move", min(current + 1, count - 1)
    if key in _ENTER:
        return "pick", current
    if key in _LEAVE:
        return "leave", -1
    if key in _QUIT:
        return "quit", -1
    if key.isdigit() and 1 <= int(key) <= count:
        return "pick", int(key) - 1
    return "stay", current


def keys(data: bytes) -> list[bytes]:
    """One read's keys: typed fast or pasted, several arrive together (`b""` is EOF)."""
    if not data:
        return [b""]
    found, i = [], 0
    while i < len(data):
        size = 3 if data[i:i + 1] == b"\x1b" and data[i + 1:i + 2] in (b"[", b"O") and i + 2 < len(data) else 1
        found.append(data[i:i + size])
        i += size
    return found


def option_line(options: list[Option], i: int, current: int, width: int) -> str:
    """`  ❯ With an agent   it asks …`: option `i` of the arrow-key menu, labels aligned,
    the detail dim and cut to `width`, the pointer and bold label on `current`."""
    label, detail = options[i]
    text = f"  {'❯' if i == current else ' '} {label:<{max(len(label) for label, _ in options)}}"
    detail = f"   {detail}" if detail else ""
    room = width - len(text)
    detail = detail if len(detail) <= room else (detail[:max(room - 1, 0)] + "…" if room > 1 else "")
    styled = click.style(text[:3], fg="cyan") + click.style(text[3:], bold=True) if i == current else text
    return styled + click.style(detail, dim=True)


def choose(title: str, options: list[Option], default: int = 0) -> int:
    """The index of the option picked with ↑/↓ (or k/j) and Enter, or its number; the
    title (none when "") above the options, `(q: quit)` under them.

    q raises `Quit`; Esc, Ctrl-C and EOF abort (`click.Abort`, "Aborted!"). Where stdin
    isn't a terminal that can read single keys, a numbered prompt asks instead."""
    fd, saved = _terminal()
    if fd is None:
        return _numbered(title, options, default)
    import termios
    import tty

    out = sys.stdout
    width = max(shutil.get_terminal_size().columns - 1, 20)
    current = default

    def line(i: int) -> str:
        return option_line(options, i, current, width)

    def draw() -> None:
        for i in range(len(options)):
            out.write("\r\x1b[2K" + line(i) + "\n")
        out.flush()

    if title:
        out.write("  " + click.style(title, bold=True) + "\n")
    out.write("\x1b[?25l")  # hide the cursor while choosing
    draw()
    out.write("  " + click.style(QUIT_HINT, dim=True) + "\n")
    picked, action = -1, "stay"
    try:
        tty.setcbreak(fd, termios.TCSANOW)  # single keys, no echo, typeahead kept; Ctrl-C still interrupts
        while action not in ("pick", "leave", "quit"):
            for key in keys(os.read(fd, 32)):
                action, current = step(key, current, len(options))
                if action in ("pick", "leave", "quit"):
                    picked = current if action == "pick" else -1
                    break
                if action == "move":
                    out.write(f"\x1b[{len(options) + 1}A")
                    draw()
                    out.write("\n")
    except KeyboardInterrupt:
        picked, action = -1, "leave"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        # Fold the menu into one line: the title and the answer (none for q).
        out.write(f"\x1b[{len(options) + 1 + bool(title)}A\r\x1b[J")
        answer = click.style(options[picked][0], fg="cyan") if picked >= 0 else ""
        shown = " ".join(filter(None, [click.style(title, bold=True) if title else "", answer]))
        out.write(("  " + shown + "\n" if shown else "") + "\x1b[?25h")
        out.flush()
    if action == "quit":
        raise Quit()
    if picked < 0:
        raise click.Abort()
    return picked


def confirm(question: str) -> None:
    """`  <question> Enter/q`: returns on Enter, raises `Quit` on q (`click.Abort` on Esc,
    Ctrl-C or EOF); other keys are ignored. A prompt asks where single keys can't be read."""
    shown = "  " + click.style(question, bold=True) + click.style(" Enter/q", dim=True)
    fd, saved = _terminal()
    if fd is None:
        while True:
            answer = click.prompt(shown, default="", show_default=False, prompt_suffix=" ").strip().lower()
            if answer in ("", "q"):
                break
        if answer == "q":
            raise Quit()
        return
    import termios
    import tty

    sys.stdout.write(shown + "\x1b[?25l")
    sys.stdout.flush()
    action = "stay"
    try:
        tty.setcbreak(fd, termios.TCSANOW)
        while action == "stay":
            for key in keys(os.read(fd, 32)):
                action = ("pick" if key in _ENTER else "quit" if key in _QUIT else "leave" if key in _LEAVE
                          else "stay")
                if action != "stay":
                    break
    except KeyboardInterrupt:
        action = "leave"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\n\x1b[?25h")
        sys.stdout.flush()
    if action == "quit":
        raise Quit()
    if action == "leave":
        raise click.Abort()


def _terminal():
    """stdin's descriptor and its saved terminal settings, or (None, None) where single
    keys can't be read (no termios, not a terminal)."""
    try:
        import termios
    except ImportError:
        return None, None
    try:
        fd = sys.stdin.fileno()
        return fd, termios.tcgetattr(fd)
    except (OSError, ValueError, termios.error):  # io.UnsupportedOperation is both
        return None, None


def _numbered(title: str, options: list[Option], default: int) -> int:
    if title:
        click.echo("  " + click.style(title, bold=True))
    labels = max(len(label) for label, _ in options)
    for i, (label, detail) in enumerate(options, 1):
        click.echo(f"  {i}. {label:<{labels}}" + (click.style(f"   {detail}", dim=True) if detail else ""))
    choices = click.Choice([str(i) for i in range(1, len(options) + 1)] + ["q"], case_sensitive=False)
    answer = click.prompt("  Choose (q: quit)", type=choices, default=str(default + 1), show_choices=False)
    if answer.lower() == "q":
        raise Quit()
    return int(answer) - 1
