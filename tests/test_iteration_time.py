"""Tests for per-iteration time budgets."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

import autohelix.iteration_time
from autohelix.config import parse_duration
from autohelix.iteration_time import format_limit


def test_one_duration_parser():
    """iteration_time's optional variant (None, empty and zero meaning unset) is gone:
    every duration goes through config.parse_duration, where those are errors."""
    assert not hasattr(autohelix.iteration_time, "parse_duration")
    for unset in (None, "", 0, -5, "0m"):
        with pytest.raises(ValueError):
            parse_duration(unset)


def test_parse_duration_numbers():
    assert parse_duration(30) == 30
    assert parse_duration(3600) == 3600
    assert parse_duration("120") == 120


def test_parse_duration_compound_strings():
    assert parse_duration("30m") == 1800
    assert parse_duration("1h") == 3600
    assert parse_duration("1h30m") == 5400
    assert parse_duration("2h15m30s") == 8130
    assert parse_duration("45s") == 45
    assert parse_duration("1h 30m") == 5400


def test_parse_duration_order_independent():
    # Shares config's order-independent grammar: units may appear in any order.
    assert parse_duration("30m1h") == 5400


def test_parse_duration_invalid():
    with pytest.raises(ValueError):
        parse_duration("abc")
    with pytest.raises(ValueError):
        parse_duration("1d")
    with pytest.raises(ValueError):
        parse_duration(True)
    with pytest.raises(ValueError):
        parse_duration("1h2h")  # duplicate unit


def test_format_limit():
    assert format_limit(0) == "0s"
    assert format_limit(45) == "45s"
    assert format_limit(60) == "1m"
    assert format_limit(3600) == "1h"
    assert format_limit(5400) == "1h30m"
    assert format_limit(8130) == "2h15m30s"


def test_config_parses_agent_timeout():
    from autohelix.config import Config

    cfg = Config.from_dict({
        "goal": "test",
        "gates": ["echo ok"],
        "agent": {"timeout": "1h"},
    })
    assert cfg.agent.timeout == 3600


def test_config_no_agent_timeout_defaults_none():
    from autohelix.config import Config

    cfg = Config.from_dict({
        "goal": "test",
        "gates": ["echo ok"],
    })
    assert cfg.agent.timeout is None


@pytest.mark.parametrize("agent_type", ["claude", "codex", "opencode"])
def test_agent_timeout_allowed(agent_type):
    from autohelix.config import Config

    data = {
        "goal": "test",
        "gates": ["echo ok"],
        "agent": {"backend": agent_type, "command": f"/usr/bin/{agent_type}", "timeout": "1h"},
    }
    cfg = Config.from_dict(data)
    errors = [issue for issue in cfg.validate() if issue.level == "error"]
    assert errors == []


def test_codex_without_hook_support_warns_and_runs_without_hints(tmp_path, monkeypatch, capsys):
    from autohelix.agents.codex import CodexAgent
    from autohelix.harness import Harness

    project = tmp_path / "project"
    project.mkdir()
    (project / "autohelix.yaml").write_text("""
goal: test
agent:
  backend: codex
  command: /usr/bin/codex
  timeout: 1h
gates:
  - "true"
""")

    for args in (["init", "-q"], ["config", "user.email", "test@example.com"],
                 ["config", "user.name", "Test"], ["add", "autohelix.yaml"],
                 ["commit", "-qm", "initial config"]):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)

    # Preflight runs before the run is allocated; let it find the agent command.
    monkeypatch.setattr("autohelix.checks.shutil.which", lambda command: command)
    monkeypatch.setattr(
        CodexAgent,
        "_validate_time_left_hook_support",
        lambda self, hook_path: "Time-left hints for Codex need codex-cli >= 0.141.0; found 0.140.0.",
    )

    harness = Harness(project)
    try:
        assert harness._codex_time_left_hook_path is None
        assert harness._time_left_script is not None  # still there to run on demand
        assert harness.config.agent.timeout == 3600
        out = " ".join(capsys.readouterr().out.split())  # wrapped at the width
        assert "found 0.140.0" in out and "Running without time-left hints" in out
    finally:
        harness.close()


TIME_LEFT = Path(__file__).parent.parent / "src" / "autohelix" / "bin" / "time_left.sh"
CLAUDE_HOOK = Path(__file__).parent.parent / "src" / "autohelix" / "bin" / "claude_hook_time_left.sh"


def run_time_left(start_offset: int | None = None, budget: int | None = None) -> str:
    from autohelix.iteration_time import format_limit

    env = os.environ.copy()
    if start_offset is not None and budget is not None:
        env["AUTOHELIX_ITERATION_START"] = str(1000 - start_offset)
        env["AUTOHELIX_ITERATION_BUDGET"] = str(budget)
        env["AUTOHELIX_ITERATION_BUDGET_TEXT"] = format_limit(budget)  # as the harness sets it
    else:
        env.pop("AUTOHELIX_ITERATION_START", None)
        env.pop("AUTOHELIX_ITERATION_BUDGET", None)
    # Freeze Bash's date as well as the start timestamp; crossing a second
    # between the two must not change exact formatting assertions.
    with tempfile.TemporaryDirectory() as directory:
        date = Path(directory) / "date"
        date.write_text("#!/bin/sh\necho 1000\n")
        date.chmod(0o755)
        env["PATH"] = f"{directory}{os.pathsep}{env['PATH']}"
        result = subprocess.run(
            [str(TIME_LEFT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
    return result.stdout.strip()


def test_time_left_no_env():
    assert "no iteration deadline" in run_time_left().lower()


# The hook reports facts (time left + the two things the agent can't otherwise
# know: hard kill at the deadline, notes persist) and lets the agent decide what
# to do. No WARNING/URGENT tiers — the ticking number is the escalation.
def _asserts_system_facts(out: str) -> None:
    assert "hard-killed" in out
    assert "persist" in out


def test_time_left_normal():
    out = run_time_left(start_offset=270, budget=3600)
    assert "55m left" in out
    _asserts_system_facts(out)


def test_time_left_hours_and_minutes():
    # 4h budget, 20m in → 3h40m left, shown as hours + minutes.
    out = run_time_left(start_offset=1200, budget=14400)
    assert "3h" in out and "left of 4h budget" in out
    _asserts_system_facts(out)


def test_time_left_sub_minute_shown_in_seconds():
    # 60s budget, 5s in → 55s left, shown in seconds (not "0m").
    out = run_time_left(start_offset=5, budget=60)
    assert "55s left" in out
    _asserts_system_facts(out)


def test_time_left_states_the_budget_exactly():
    """It used to round to whole hours or minutes: "1h 30m left of 1h budget". The
    budget is spelled as the config and flags spell a limit (`format_limit`)."""
    assert "1h 30m left of 1h30m budget" in run_time_left(start_offset=0, budget=5400)
    assert "45s left of 45s budget" in run_time_left(start_offset=0, budget=45)
    assert "of 1m30s budget" in run_time_left(start_offset=0, budget=90)


def test_the_claude_hook_logs_no_tool_calls(tmp_path):
    """No audit log of every tool call's payload, in the run folder or /tmp."""
    run = tmp_path / "run"
    run.mkdir()
    env = {**os.environ, "AUTOHELIX_RUN_DIR": str(run), "AUTOHELIX_TIME_LEFT_SCRIPT": str(TIME_LEFT),
           "AUTOHELIX_ITERATION_START": "1", "AUTOHELIX_ITERATION_BUDGET": "60"}
    result = subprocess.run([str(CLAUDE_HOOK)], env=env, input='{"tool_input": "' + "x" * 1000 + '"}',
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert list(run.iterdir()) == []
    assert "AUDIT" not in CLAUDE_HOOK.read_text() and "audit" not in (CLAUDE_HOOK.parent / "opencode_timer_pertool.js").read_text()


def test_time_left_passed():
    out = run_time_left(start_offset=4200, budget=3600)
    assert "Deadline passed" in out
    assert "600s ago" in out
    _asserts_system_facts(out)


def test_claude_hook_emits_valid_json():
    import time as _time

    env = os.environ.copy()
    env["AUTOHELIX_ITERATION_START"] = str(int(_time.time()) - 300)
    env["AUTOHELIX_ITERATION_BUDGET"] = "3600"
    env["AUTOHELIX_ITERATION_BUDGET_TEXT"] = "1h"
    env["AUTOHELIX_TIME_LEFT_SCRIPT"] = str(TIME_LEFT)
    result = subprocess.run(
        [str(CLAUDE_HOOK)],
        env=env,
        input='{"hook_event_name":"PostToolUse"}',
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "left of 1h budget" in data["hookSpecificOutput"]["additionalContext"]


def test_the_agent_gets_its_budget_formatted(project, monkeypatch):
    """`agent.timeout: 1h30m` reaches the time-left hint as "1h30m" (as the config spells
    it; it was "1h 30m", a third duration format), not rounded."""
    from autohelix.agents.mock import MockAgent
    from autohelix.harness import Budget, Harness
    from tests.test_runs import configure

    seen = []
    real = MockAgent.run

    def run(self, *args, **kwargs):
        seen.append(os.environ.get("AUTOHELIX_ITERATION_BUDGET_TEXT"))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(MockAgent, "run", run)
    data = __import__("yaml").safe_load((project / "autohelix.yaml").read_text())
    configure(project, agent={**data["agent"], "timeout": "1h30m"})
    harness = Harness(project, land_mode="manual")
    harness.run(Budget(until=1), report=False)
    harness.close()
    assert seen == ["1h30m"]
    assert "AUTOHELIX_ITERATION_BUDGET_TEXT" not in os.environ  # restored after the call
