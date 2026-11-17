"""Fixtures for every collected test (tests/ and examples/)."""

import os

import pytest

from autohelix.runs import COPIES_ENV

# Switches for the suite itself, kept when the rest of `AUTOHELIX_*` is removed.
SUITE_SWITCHES = {"AUTOHELIX_UPDATE_SNAPSHOTS"}
GIT_CONFIG = ("[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n"
              # Not the user's global ignore and attributes files (~/.config/git/), read without a config.
              f"[core]\n\texcludesFile = {os.devnull}\n\tattributesFile = {os.devnull}\n")


@pytest.fixture(autouse=True)
def copies_dir(tmp_path_factory, monkeypatch):
    """Candidate copies go to a private folder beside the test's project, never the real cache."""
    base = tmp_path_factory.mktemp("copies").resolve()
    monkeypatch.setenv(COPIES_ENV, str(base))
    return base


@pytest.fixture(autouse=True)
def clean_env(copies_dir, tmp_path_factory, monkeypatch):
    """Every test starts from a known environment, not the one it was started in.

    The suite is often run inside an AutoHelix run (a `pytest` gate, an agent's shell):
    without this, `autohelix ask` called by a test would append usage records and
    transcripts to the enclosing iteration ($AUTOHELIX_USAGE_FILE, $AUTOHELIX_LOG_DIR),
    and an exported
    CLAUDE_CODE_DISABLE_AUTO_MEMORY or a global git config (signing, hooks) would change
    what tests see. Tests that need one of these set it themselves."""
    for name in list(os.environ):
        if name.startswith("AUTOHELIX_") and name not in SUITE_SWITCHES | {COPIES_ENV}:
            monkeypatch.delenv(name)
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", raising=False)
    monkeypatch.delenv("GIT_CEILING_DIRECTORIES", raising=False)  # runs prepend to it
    # A known global git config instead of the user's (signing, hooks, templates): an
    # identity and `main` as the first branch; a repository's own config still wins.
    git_config = tmp_path_factory.mktemp("git") / "config"
    git_config.write_text(GIT_CONFIG)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("LINES", "FORCE_COLOR", "NO_COLOR"):  # Rich's terminal size and colours
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COLUMNS", "80")


AWAKE_TOOLS = ("caffeinate", "systemd-inhibit")


@pytest.fixture(autouse=True)
def no_keep_awake(tmp_path_factory, monkeypatch):
    """`run`/`resume` never start the real caffeinate or systemd-inhibit in a test: in this
    process `awake._start` records the command instead (`no_keep_awake` is the list), and
    an `autohelix run` started as a child finds stand-ins first on PATH that exit at once."""
    started = []
    monkeypatch.setattr("autohelix.awake._start", lambda command: started.append(command))
    stubs = tmp_path_factory.mktemp("awake")
    for name in AWAKE_TOOLS:
        (stubs / name).write_text("#!/bin/sh\nexit 0\n")
        (stubs / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}")
    return started
