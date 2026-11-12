# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The arrow-key menu `autohelix setup` asks with."""


import click
import pytest

from autohelix import menu

OPTIONS = [("With Claude Code", "it asks"), ("Empty config", "the template"), ("Third", "")]


@pytest.mark.parametrize("key, current, expected", [
    (b"\x1b[B", 0, ("move", 1)), (b"j", 1, ("move", 2)), (b"\x1b[B", 2, ("move", 2)),
    (b"\x1b[A", 1, ("move", 0)), (b"k", 0, ("move", 0)), (b"\x1bOA", 2, ("move", 1)),
    (b"\r", 2, ("pick", 2)), (b"\n", 0, ("pick", 0)), (b"2", 0, ("pick", 1)),
    (b"9", 0, ("stay", 0)), (b"x", 1, ("stay", 1)),
    (b"\x1b", 1, ("leave", -1)), (b"", 0, ("leave", -1)), (b"\x03", 0, ("leave", -1)),
])
def test_step(key, current, expected):
    assert menu.step(key, current, 3) == expected


def test_without_a_terminal_a_numbered_prompt_asks(monkeypatch):
    from click.testing import CliRunner

    @click.command()
    def command():
        click.echo(f"picked {menu.choose('How?', OPTIONS, default=1)}")

    result = CliRunner().invoke(command, input="\n")
    assert "How?\n  1. With Claude Code  it asks\n  2. Empty config  the template\n  3. Third\nChoose [2]:" in result.output
    assert "picked 1" in result.output
    assert "picked 2" in CliRunner().invoke(command, input="3\n").output


def test_keys_typed_together_are_split():
    assert menu.keys(b"jj\x1b[B\r") == [b"j", b"j", b"\x1b[B", b"\r"]
    assert menu.keys(b"\x1b") == [b"\x1b"]
    assert menu.keys(b"") == [b""]
