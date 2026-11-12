# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Arrow-key menus for `autohelix setup`, with a numbered prompt as the fallback."""

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


def step(key: bytes, current: int, count: int) -> tuple[str, int]:
    """What a key press does: ("move", i), ("pick", i), ("leave", -1) or ("stay", current)."""
    if key in _UP:
        return "move", max(current - 1, 0)
    if key in _DOWN:
        return "move", min(current + 1, count - 1)
    if key in _ENTER:
        return "pick", current
    if key in _LEAVE:
        return "leave", -1
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


def choose(title: str, options: list[Option], default: int = 0) -> int:
    """The index of the option picked with ↑/↓ (or k/j) and Enter, or its number.

    Esc, Ctrl-C and EOF abort (`click.Abort`, "Aborted!"). Where stdin isn't a
    terminal that can read single keys, a numbered prompt asks instead."""
    try:
        import termios
        import tty
    except ImportError:
        return _numbered(title, options, default)
    try:
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except (OSError, ValueError, termios.error):  # io.UnsupportedOperation is both
        return _numbered(title, options, default)
    out = sys.stdout
    width = max(shutil.get_terminal_size().columns - 1, 20)
    current = default

    def line(i: int) -> str:
        label, detail = options[i]
        text = f"{'❯' if i == current else ' '} {i + 1}. {label}"
        detail = f"  {detail}" if detail else ""
        room = width - len(text)
        detail = detail if len(detail) <= room else (detail[:max(room - 1, 0)] + "…" if room > 1 else "")
        styled = click.style(text, fg="cyan", bold=True) if i == current else text
        return styled + click.style(detail, dim=True)

    def draw() -> None:
        for i in range(len(options)):
            out.write("\r\x1b[2K" + line(i) + "\n")
        out.flush()

    out.write(click.style(title, bold=True) + "\n\x1b[?25l")  # hide the cursor while choosing
    draw()
    picked = -1
    try:
        tty.setcbreak(fd, termios.TCSANOW)  # single keys, no echo, typeahead kept; Ctrl-C still interrupts
        action = "stay"
        while action not in ("pick", "leave"):
            for key in keys(os.read(fd, 32)):
                action, current = step(key, current, len(options))
                if action in ("pick", "leave"):
                    picked = current if action == "pick" else -1
                    break
                if action == "move":
                    out.write(f"\x1b[{len(options)}A")
                    draw()
    except KeyboardInterrupt:
        picked = -1
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        # Fold the menu into one line: the title and the answer.
        out.write(f"\x1b[{len(options) + 1}A\r\x1b[J" + click.style(title, bold=True))
        out.write((" " + click.style(options[picked][0], fg="cyan") if picked >= 0 else "") + "\n\x1b[?25h")
        out.flush()
    if picked < 0:
        raise click.Abort()
    return picked


def _numbered(title: str, options: list[Option], default: int) -> int:
    click.echo(title)
    for i, (label, detail) in enumerate(options, 1):
        click.echo(f"  {i}. {label}" + (click.style(f"  {detail}", dim=True) if detail else ""))
    return click.prompt("Choose", type=click.IntRange(1, len(options)), default=default + 1) - 1
