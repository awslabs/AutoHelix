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
_LEAVE = (b"", b"\x04")  # EOF, Ctrl-D
_QUIT = (b"q", b"Q", b"\x1b")  # Esc too
_INTERRUPT = b"\x03"  # Ctrl-C, where it arrives as a key (the terminal usually sends SIGINT instead)
QUIT_HINT = "(q: quit)"


class Quit(Exception):
    """q or Esc pressed: the user is done (exit 0), unlike EOF (`click.Abort`) or Ctrl-C
    (`KeyboardInterrupt`)."""


def step(key: bytes, current: int, count: int) -> tuple[str, int]:
    """What a key press does: ("move", i), ("pick", i), ("quit", -1), ("leave", -1) or
    ("stay", current); Ctrl-C raises `KeyboardInterrupt`."""
    if key == _INTERRUPT:
        raise KeyboardInterrupt
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
    the detail dim (left out when it doesn't fit in `width` whole), the pointer and bold
    label on `current`."""
    label, detail = options[i]
    text = f"  {'❯' if i == current else ' '} {label:<{max(len(label) for label, _ in options)}}"
    detail = f"   {detail}" if detail and len(text) + 3 + len(detail) <= width else ""
    styled = click.style(text[:3], fg="cyan") + click.style(text[3:], bold=True) if i == current else text
    return styled + click.style(detail, dim=True)


def choose(title: str, options: list[Option], default: int = 0) -> int:
    """The index of the option picked with ↑/↓ (or k/j) and Enter, or its number; the
    title (none when "") above the options, `(q: quit)` under them.

    q or Esc raises `Quit`, EOF aborts (`click.Abort`, "Aborted!"), Ctrl-C raises
    `KeyboardInterrupt`. Keys typed before the menu is drawn are dropped. Where stdin
    isn't a terminal that can read single keys, a numbered prompt asks instead."""
    fd, saved = _terminal()
    if fd is None:
        return _numbered(title, options, default)
    import termios

    out = sys.stdout
    width = max(shutil.get_terminal_size().columns - 1, 20)
    current = default

    def draw() -> None:
        for i in range(len(options)):
            out.write("\r\x1b[2K" + option_line(options, i, current, width) + "\n")
        out.flush()

    picked, action = -1, "stay"
    _single_keys(fd)
    try:
        if title:
            out.write("  " + click.style(title, bold=True) + "\n")
        out.write("\x1b[?25l")  # hide the cursor while choosing
        draw()
        out.write("  " + click.style(QUIT_HINT, dim=True) + "\n")
        out.flush()
        while action not in ("pick", "leave", "quit"):
            for key in keys(_read(fd)):
                action, current = step(key, current, len(options))
                if action in ("pick", "leave", "quit"):
                    picked = current if action == "pick" else -1
                    break
                if action == "move":
                    out.write(f"\x1b[{len(options) + 1}A")
                    draw()
                    out.write("\n")
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
    """`  <question> Enter/q`: returns on Enter, raises `Quit` on q or Esc (`click.Abort`
    on EOF, `KeyboardInterrupt` on Ctrl-C); other keys are ignored, and so are keys typed
    before the question. A prompt asks where single keys can't be read."""
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

    action = "stay"
    _single_keys(fd)
    try:
        sys.stdout.write(shown + "\x1b[?25l")
        sys.stdout.flush()
        while action == "stay":
            for key in keys(_read(fd)):
                if key == _INTERRUPT:
                    raise KeyboardInterrupt
                action = ("pick" if key in _ENTER else "quit" if key in _QUIT else "leave" if key in _LEAVE
                          else "stay")
                if action != "stay":
                    break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\n\x1b[?25h")
        sys.stdout.flush()
    if action == "quit":
        raise Quit()
    if action == "leave":
        raise click.Abort()


def _single_keys(fd: int) -> None:
    """Read `fd` a key at a time, unechoed (Ctrl-C still interrupts), and drop what was
    typed before: keys pressed while the screen loaded must not pick an option."""
    import termios
    import tty

    tty.setcbreak(fd, termios.TCSANOW)
    termios.tcflush(fd, termios.TCIFLUSH)


def _read(fd: int) -> bytes:
    """The next keys on `fd`. An Esc that may begin an arrow key's sequence waits briefly
    for the rest (a slow link can split it), so it isn't taken for Esc alone."""
    import select

    data = os.read(fd, 32)
    while data.endswith((b"\x1b", b"\x1b[", b"\x1bO")) and select.select([fd], [], [], 0.05)[0]:
        more = os.read(fd, 32)
        if not more:
            break
        data += more
    return data


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
