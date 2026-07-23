"""Tests for per-iteration time budgets."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from autohelix.iteration_time import format_duration, parse_duration


def test_parse_duration_none_or_empty():
    assert parse_duration(None) is None
    assert parse_duration("") is None


def test_parse_duration_numbers():
    assert parse_duration(30) == 30
    assert parse_duration(3600) == 3600
    assert parse_duration(0) is None
    assert parse_duration(-5) is None
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


def test_format_duration():
    assert format_duration(0) == "0s"
    assert format_duration(45) == "45s"
    assert format_duration(60) == "1m"
    assert format_duration(3600) == "1h"
    assert format_duration(5400) == "1h30m"
    assert format_duration(8130) == "2h15m30s"


def test_config_parses_iteration_time():
    from autohelix.config import Config

    cfg = Config.from_dict({
        "goal": "test",
        "constraints": ["echo ok"],
        "observables": [{"command": "echo 1", "values": {"score": "higher"}}],
        "budget": {"iterations": 10, "iteration_time": "1h"},
    })
    assert cfg.iteration_time_seconds == 3600


def test_config_no_iteration_time_defaults_none():
    from autohelix.config import Config

    cfg = Config.from_dict({
        "goal": "test",
        "constraints": ["echo ok"],
        "budget": {"iterations": 10},
    })
    assert cfg.iteration_time_seconds is None


@pytest.mark.parametrize("agent_type", ["claude", "codex", "opencode"])
def test_iteration_time_allowed(agent_type):
    from autohelix.config import Config

    data = {
        "goal": "test",
        "constraints": ["echo ok"],
        "agent": {"type": agent_type, "command": f"/usr/bin/{agent_type}"},
        "budget": {"iteration_time": "1h"},
    }
    cfg = Config.from_dict(data)
    errors = [issue for issue in cfg.validate(raw_data=data) if issue.level == "error"]
    assert errors == []


def test_codex_iteration_time_exits_before_run_if_version_unsupported(tmp_path, monkeypatch):
    from autohelix.agents.codex import CodexAgent
    from autohelix.harness import Harness

    project = tmp_path / "project"
    project.mkdir()
    (project / "autohelix.yaml").write_text("""
goal: test
agent:
  type: codex
  command: /usr/bin/codex
budget:
  iteration_time: 1h
constraints:
  - "true"
""")

    monkeypatch.setattr(
        CodexAgent,
        "_validate_time_left_hook_support",
        lambda self, hook_path: "budget.iteration_time for Codex requires codex-cli >= 0.141.0; found 0.140.0.",
    )

    with pytest.raises(SystemExit) as exc:
        Harness(project)

    assert "Codex iteration_time setup failed" in str(exc.value)


TIME_LEFT = Path(__file__).parent.parent / "src" / "autohelix" / "bin" / "time_left.sh"
CLAUDE_HOOK = Path(__file__).parent.parent / "src" / "autohelix" / "bin" / "claude_hook_time_left.sh"


def run_time_left(start_offset: int | None = None, budget: int | None = None) -> str:
    env = os.environ.copy()
    if start_offset is not None and budget is not None:
        import time as _time

        env["AUTOHELIX_ITERATION_START"] = str(int(_time.time()) - start_offset)
        env["AUTOHELIX_ITERATION_BUDGET"] = str(budget)
    else:
        env.pop("AUTOHELIX_ITERATION_START", None)
        env.pop("AUTOHELIX_ITERATION_BUDGET", None)
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
    # ~55m remaining; allow a minute either way for sub-second clock drift between
    # the test stamping START and the script reading `date` (truncating math).
    assert any(f"{m}m left" in out for m in (54, 55, 56))
    _asserts_system_facts(out)


def test_time_left_hours_and_minutes():
    # 4h budget, 20m in → ~3h39m left, shown as hours + minutes.
    out = run_time_left(start_offset=1200, budget=14400)
    assert "3h" in out and "left of 4h budget" in out
    _asserts_system_facts(out)


def test_time_left_sub_minute_shown_in_seconds():
    # 60s budget, 5s in → 55s left, shown in seconds (not "0m").
    out = run_time_left(start_offset=5, budget=60)
    assert "55s left" in out
    _asserts_system_facts(out)


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
