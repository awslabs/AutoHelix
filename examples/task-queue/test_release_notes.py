"""Frozen regression test for the release-notes pipeline."""

from release_notes.render import render_release_notes


def test_empty_release_notes():
    assert render_release_notes([]) == ""
