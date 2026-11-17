"""`run` and `resume` keep the machine awake while their segment runs (`autohelix.awake`).

The root conftest.py replaces `awake._start` (and puts stand-ins first on PATH), so no
test starts the real caffeinate or systemd-inhibit; `real_start` is the module's own,
taken before that fixture runs, used here only with harmless commands."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from autohelix import awake
from autohelix.awake import _start as real_start, inhibit_command, keep_awake
from tests.helpers import cli


def test_the_command_per_platform():
    assert inhibit_command(4321, "darwin") == ["caffeinate", "-i", "-w", "4321"]
    linux = inhibit_command(4321, "linux")
    assert linux[0] == "systemd-inhibit" and "--what=idle:sleep" in linux and "--mode=block" in linux
    # Held while a `tail` waits for this process: gone when it ends, killed too.
    assert linux[linux.index("--mode=block") + 1:] == ["tail", "--pid=4321", "-f", "/dev/null"]
    assert inhibit_command(4321, "win32") is None


def test_run_and_resume_hold_the_block_for_this_process(project, no_keep_awake):
    assert cli(project, "run", "-n", "1", "--no-report").exit_code == 0
    assert cli(project, "resume", "-n", "1", "--no-report").exit_code == 0
    expected = inhibit_command(os.getpid(), sys.platform)
    assert expected is None or no_keep_awake == [expected, expected]


def test_run_check_holds_nothing(project, no_keep_awake):
    assert cli(project, "run", "--check").exit_code == 0
    assert no_keep_awake == []


def _holder(tmp_path):
    """A stand-in block in its own session: `sh` with a child that writes its pid."""
    marker = tmp_path / "child.pid"
    process = real_start(["sh", "-c", f'sleep 60 & echo $! > "{marker}"; wait'])
    deadline = time.monotonic() + 10
    while not marker.exists() or not marker.read_text().strip():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return process, int(marker.read_text())


def _gone(pid):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("failing", [False, True])
def test_the_block_ends_with_the_segment_whole_group_included(project, tmp_path, monkeypatch, failing):
    held = []

    def start(command):
        process, child = _holder(tmp_path)
        held.append((process, child))
        return process

    monkeypatch.setattr(awake, "_start", start)
    if failing:  # the segment fails: released all the same
        monkeypatch.setattr("autohelix.harness.Harness.run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    result = cli(project, "run", "-n", "1", "--no-report")
    assert result.exit_code == (1 if failing else 0), result.output
    if inhibit_command(os.getpid(), sys.platform) is None:
        return
    (process, child), = held
    assert process.returncode is not None  # waited for
    assert _gone(child)  # the tool's own child (systemd-inhibit's `tail`) too


def test_the_block_survives_ctrl_c_in_the_terminal(tmp_path):
    """Its own session: a SIGINT to the run's process group doesn't end it early."""
    process, child = _holder(tmp_path)
    try:
        assert os.getpgid(process.pid) == process.pid != os.getpgid(0)
    finally:
        awake._release(process)
    assert _gone(child)


def test_a_missing_or_refused_tool_means_no_block_and_no_message(tmp_path, monkeypatch, capfd):
    assert real_start(["autohelix-no-such-keep-awake-tool"]) is None
    refused = tmp_path / "refuse"
    refused.write_text("#!/bin/sh\necho 'Failed to inhibit: Access denied' >&2\nexit 1\n")
    refused.chmod(0o755)
    monkeypatch.setattr(awake, "inhibit_command", lambda pid, platform: [str(refused)])
    monkeypatch.setattr(awake, "_start", real_start)
    with keep_awake():
        time.sleep(0.2)  # it has refused by now
    monkeypatch.setattr(awake, "inhibit_command", lambda pid, platform: ["autohelix-no-such-keep-awake-tool"])
    with keep_awake():
        pass
    captured = capfd.readouterr()
    assert captured.out == captured.err == ""


def test_a_child_run_finds_the_stand_ins_first():
    """An `autohelix run` a test starts as a child process never reaches the real tool."""
    for name in ("caffeinate", "systemd-inhibit"):
        found = subprocess.run(["sh", "-c", f"command -v {name}"], capture_output=True, text=True).stdout.strip()
        assert found and Path(found).read_text() == "#!/bin/sh\nexit 0\n"
