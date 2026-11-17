"""Bare `autohelix` in a real terminal: the entry screen run in a child process on a
pseudo-terminal, read single keys at a time (cbreak), with fake agents on PATH. The other
entry tests go through the numbered prompt (CliRunner's streams only claim to be TTYs)."""

import os
import select
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

pty = pytest.importorskip("pty")
termios = pytest.importorskip("termios")
fcntl = pytest.importorskip("fcntl")

from tests.helpers import git  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "src"
# The child makes the pty its controlling terminal (so Ctrl-C there is SIGINT), then runs `autohelix`.
CHILD = "import os; os.login_tty(0); from autohelix.cli import main; main()"
TIMEOUT = 30


class Terminal:
    """A child process on a pty, its output collected as it is read."""

    def __init__(self, cwd: Path, env: dict):
        master, slave = os.openpty()
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 100, 0, 0))
        self.process = subprocess.Popen([sys.executable, "-c", CHILD], cwd=cwd, env=env,
                                        stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        self.fd, self.output = master, b""

    def type(self, keys: bytes) -> None:
        os.write(self.fd, keys)

    def _read(self, wait: float) -> bool:
        """Add what the child wrote within `wait` seconds; False once it closed the pty."""
        if not select.select([self.fd], [], [], wait)[0]:
            return True
        try:
            data = os.read(self.fd, 65536)
        except OSError:  # EIO (Linux) once the child is gone
            data = b""
        self.output += data
        return bool(data)

    def _shown(self) -> str:
        return self.output.decode(errors="replace")

    def read_until(self, text: str) -> None:
        """Read until `text` is in the output since the start (a failure shows what came)."""
        deadline = time.monotonic() + TIMEOUT
        while text.encode() not in self.output:
            left = deadline - time.monotonic()
            assert left > 0, f"no {text!r} in {TIMEOUT}s; the output so far: {self._shown()}"
            assert self._read(min(left, 0.1)), f"ended without {text!r}; the output: {self._shown()}"

    def exit_code(self) -> int:
        """The child's exit code, reading its output meanwhile (a full pty would block it)."""
        try:
            deadline = time.monotonic() + TIMEOUT
            while self.process.poll() is None:
                assert time.monotonic() < deadline, f"still running after {TIMEOUT}s: {self._shown()}"
                if not self._read(0.1):
                    break
            code = self.process.wait(timeout=TIMEOUT)
            while select.select([self.fd], [], [], 0.1)[0] and self._read(0):  # what is left
                pass
            return code
        finally:
            self.close()

    def close(self) -> None:
        if self.fd is not None:  # first: a child (or the agent it became) may wait to drain its output
            os.close(self.fd)
            self.fd = None
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=TIMEOUT)


@pytest.fixture
def screen(tmp_path):
    """Start bare `autohelix` in a committed checkout with a config and no runs (two
    options: check and finish it, set up a new one), fake agents on PATH; yields a
    function taking the agents' names that returns the Terminal."""
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-q")
    (project / "main.py").write_text("x = 1\n")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "init")
    (project / "autohelix.yaml").write_text("goal: half done\n")
    started = []

    def start(agents=("claude",)) -> Terminal:
        bin_dir = tmp_path / ("bin-" + "-".join(agents))
        bin_dir.mkdir()
        (bin_dir / "git").symlink_to(shutil.which("git"))
        for agent in agents:  # prints its arguments, NUL-separated, to argv-<agent>
            (bin_dir / agent).write_text(f"#!/bin/sh\nfor a in \"$@\"; do printf '%s\\0' \"$a\"; done "
                                         f"> '{tmp_path}/argv-{agent}'\necho FAKE-AGENT-STARTED\n")
            (bin_dir / agent).chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AUTOHELIX_", "PYTHON"))}
        env.update(PATH=str(bin_dir), PYTHONPATH=str(SRC), TERM="xterm-256color", COLUMNS="100", LINES="40",
                   GIT_CEILING_DIRECTORIES=str(tmp_path))
        terminal = Terminal(project, env)
        started.append(terminal)
        return terminal

    start.argv = lambda agent="claude": (tmp_path / f"argv-{agent}").read_bytes().decode().split("\0")[:-1]
    start.started = lambda agent="claude": (tmp_path / f"argv-{agent}").exists()
    yield start
    for terminal in started:
        terminal.close()


def test_down_then_enter_starts_the_agent_with_the_second_request(screen):
    terminal = screen()
    terminal.read_until("(q: quit)")
    terminal.type(b"\x1b[B")
    terminal.type(b"\r")
    terminal.read_until("FAKE-AGENT-STARTED")
    assert terminal.exit_code() == 0
    argv = screen.argv()
    assert argv[0] == "--add-dir" and argv[-2] == "--"
    prompt = argv[-1]
    assert 'I picked "Set up a new experiment with Claude Code"' in prompt
    assert "My first request: set up a new experiment. Propose a short name for this goal" in prompt


@pytest.mark.parametrize("key, code", [(b"q", 0), (b"\x1b", 0), (b"\x03", 130)])
def test_q_esc_and_ctrl_c(screen, key, code):
    terminal = screen()
    terminal.read_until("(q: quit)")
    terminal.type(key)
    assert terminal.exit_code() == code
    assert not screen.started() and b"Aborted" not in terminal.output


@pytest.mark.parametrize("key, code", [(b"\x1b", 0), (b"\x03", 130)])
def test_esc_and_ctrl_c_at_which_agent(screen, key, code):
    terminal = screen(("claude", "codex"))
    terminal.read_until("(q: quit)")
    terminal.type(b"\r")
    terminal.read_until("Which agent?")  # drawn after the earlier keys were dropped
    terminal.type(key)
    assert terminal.exit_code() == code
    assert not screen.started("claude") and not screen.started("codex") and b"Aborted" not in terminal.output


def test_keys_typed_before_the_menu_are_dropped(screen):
    """An impatient Enter while the screen loads doesn't start an agent session."""
    terminal = screen()
    terminal.type(b"\r")  # before the child has even started Python
    terminal.read_until("(q: quit)")
    time.sleep(0.3)
    assert terminal.process.poll() is None and not screen.started()
    terminal.type(b"q")
    assert terminal.exit_code() == 0
    assert not screen.started()
