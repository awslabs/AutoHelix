# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""run_process itself: the loop that runs every agent, ask and review call."""

import os
import resource
import time

from autohelix.agents.runner import run_process


def _run(tmp_path, script, **kwargs):
    lines: list[str] = []
    started = time.monotonic()
    result = run_process(
        cmd=["sh", "-c", script],
        cwd=tmp_path,
        log_path=tmp_path / "agent.log",
        line_callback=lines.append,
        **kwargs,
    )
    return result, lines, time.monotonic() - started


def _own_cpu() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def test_lines_and_exit_code(tmp_path):
    result, lines, _ = _run(tmp_path, "echo one; echo; echo two; exit 3")
    assert (result.exit_code, result.timed_out) == (3, False)
    assert lines == ["one", "two"]
    assert (tmp_path / "agent.log").read_text() == "one\n\ntwo\n"


def test_final_line_without_newline_is_delivered(tmp_path):
    result, lines, _ = _run(tmp_path, "echo first; printf last")
    assert result.exit_code == 0
    assert lines == ["first", "last"]


def test_output_then_sleep_is_killed_at_the_timeout(tmp_path):
    result, lines, elapsed = _run(tmp_path, "echo hi; sleep 30", timeout_seconds=1)
    assert (result.exit_code, result.timed_out) == (124, True)
    assert lines == ["hi"]
    assert elapsed < 10
    assert "[autohelix] Timeout" in (tmp_path / "agent.log").read_text()


def test_closed_output_is_still_killed_at_the_timeout(tmp_path):
    """A child that closes its output and keeps running: timed out, no busy loop."""
    cpu_before = _own_cpu()
    result, lines, elapsed = _run(
        tmp_path, "echo hi; exec 1>&- 2>&-; sleep 30", timeout_seconds=1)
    assert (result.exit_code, result.timed_out) == (124, True)
    assert lines == ["hi"]
    assert elapsed < 10
    # The harness waited instead of spinning on the closed pipe.
    assert _own_cpu() - cpu_before < 0.5 * elapsed


def test_closed_output_then_exit_returns_its_code(tmp_path):
    result, lines, _ = _run(
        tmp_path, "echo hi; exec 1>&- 2>&-; sleep 1; exit 5", timeout_seconds=20)
    assert (result.exit_code, result.timed_out) == (5, False)
    assert lines == ["hi"]


def test_timeout_kills_the_whole_process_group(tmp_path):
    marker = tmp_path / "pid"
    result, _, _ = _run(
        tmp_path, f"sleep 30 & echo $! > {marker}; wait", timeout_seconds=1)
    assert result.timed_out
    pid = int(marker.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("background child survived the timeout")
