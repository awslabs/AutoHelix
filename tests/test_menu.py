# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The arrow-key menu and the Enter/q question bare `autohelix` asks with."""


import click
import pytest

from autohelix import menu

OPTIONS = [("With Claude Code", "it asks"), ("Empty config", "the template"), ("Third", "")]


@pytest.mark.parametrize("key, current, expected", [
    (b"\x1b[B", 0, ("move", 1)), (b"j", 1, ("move", 2)), (b"\x1b[B", 2, ("move", 2)),
    (b"\x1b[A", 1, ("move", 0)), (b"k", 0, ("move", 0)), (b"\x1bOA", 2, ("move", 1)),
    (b"\r", 2, ("pick", 2)), (b"\n", 0, ("pick", 0)), (b"2", 0, ("pick", 1)),
    (b"9", 0, ("stay", 0)), (b"x", 1, ("stay", 1)),
    (b"", 0, ("leave", -1)), (b"\x04", 0, ("leave", -1)),
    (b"q", 1, ("quit", -1)), (b"Q", 0, ("quit", -1)), (b"\x1b", 1, ("quit", -1)),
])
def test_step(key, current, expected):
    assert menu.step(key, current, 3) == expected


def test_ctrl_c_as_a_key_interrupts():
    with pytest.raises(KeyboardInterrupt):
        menu.step(b"\x03", 0, 3)


def test_a_detail_that_does_not_fit_whole_is_left_out():
    options = [("Ask", "it summarizes them"), ("Set up a new one", "it drafts")]
    assert click.unstyle(menu.option_line(options, 0, 0, 80)) == "  ❯ Ask                it summarizes them"
    assert click.unstyle(menu.option_line(options, 0, 0, 41)) == "  ❯ Ask                it summarizes them"
    assert click.unstyle(menu.option_line(options, 0, 0, 40)) == "  ❯ Ask             "
    assert click.unstyle(menu.option_line(options, 1, 0, 40)) == "    Set up a new one   it drafts"


def test_without_a_terminal_a_numbered_prompt_asks(monkeypatch):
    from click.testing import CliRunner

    @click.command()
    def command():
        try:
            click.echo(f"picked {menu.choose('How?', OPTIONS, default=1)}")
        except menu.Quit:
            click.echo("quit")

    result = CliRunner().invoke(command, input="\n")
    shown = "\n".join(line.rstrip() for line in result.output.splitlines())
    assert ("  How?\n  1. With Claude Code   it asks\n  2. Empty config       the template\n  3. Third\n"
            "  Choose (q: quit) [2]:") in shown
    assert "picked 1" in result.output
    assert "picked 2" in CliRunner().invoke(command, input="3\n").output
    assert "quit" in CliRunner().invoke(command, input="q\n").output
    assert "picked 0" in CliRunner().invoke(command, input="7\n1\n").output  # asked again
    assert CliRunner().invoke(command, input="").exit_code == 1  # EOF: Aborted!


def test_no_title_no_title_line():
    from click.testing import CliRunner

    @click.command()
    def command():
        menu.choose("", OPTIONS)

    assert CliRunner().invoke(command, input="\n").output.startswith("  1. With Claude Code")


def test_confirm_enter_or_q():
    from click.testing import CliRunner

    @click.command()
    def command():
        try:
            menu.confirm("Set up?")
            click.echo("yes")
        except menu.Quit:
            click.echo("quit")

    result = CliRunner().invoke(command, input="\n")
    assert result.output.startswith("  Set up? Enter/q ") and result.output.endswith("yes\n")
    assert CliRunner().invoke(command, input="q\n").output.endswith("quit\n")
    assert CliRunner().invoke(command, input="x\n\n").output.endswith("yes\n")  # asked again
    assert CliRunner().invoke(command, input="").exit_code == 1


def test_keys_typed_together_are_split():
    assert menu.keys(b"jj\x1b[B\r") == [b"j", b"j", b"\x1b[B", b"\r"]
    assert menu.keys(b"\x1b") == [b"\x1b"]
    assert menu.keys(b"") == [b""]
