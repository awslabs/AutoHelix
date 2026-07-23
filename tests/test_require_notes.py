"""Tests for the require_notes enforcement (Stop hook)."""

import os
import subprocess
from pathlib import Path

import pytest

from autohelix.agents import AgentConfig, RequireNotesConfig
from autohelix.config import Config

HOOK = Path(__file__).parent.parent / "src" / "autohelix" / "bin" / "check_notes.sh"


# --- config parsing -------------------------------------------------------

def test_require_notes_absent_is_none():
    assert AgentConfig.from_dict({"type": "claude"}).require_notes is None


def test_require_notes_false_is_none():
    assert AgentConfig.from_dict({"type": "claude", "require_notes": False}).require_notes is None


def test_require_notes_bare_true_uses_defaults():
    rn = AgentConfig.from_dict({"type": "claude", "require_notes": True}).require_notes
    assert rn == RequireNotesConfig(min_chars=50, max_retries=3)


def test_require_notes_dict_overrides():
    rn = AgentConfig.from_dict(
        {"type": "claude", "require_notes": {"min_chars": 200, "max_retries": 5}}
    ).require_notes
    assert rn == RequireNotesConfig(min_chars=200, max_retries=5)


# --- validation -----------------------------------------------------------

def _config(agent):
    return Config.from_dict(
        {
            "goal": "x",
            "agent": agent,
            "observables": [{"command": "echo hi", "values": {"s": "higher"}}],
        }
    )


def test_require_notes_rejected_for_codex():
    issues = _config({"type": "codex", "require_notes": True}).validate()
    assert any("require_notes" in i.message for i in issues)


def test_require_notes_allowed_for_claude():
    issues = _config({"type": "claude", "require_notes": True}).validate()
    assert not any("require_notes" in i.message for i in issues)


def test_require_notes_bad_max_retries():
    issues = _config({"type": "claude", "require_notes": {"max_retries": 0}}).validate()
    assert any("max_retries" in i.message for i in issues)


# --- the hook script behavior --------------------------------------------

def _run_hook(tmp_path, notes_text=None, retry_count=0, budget=7200, min_chars=50, max_retries=3):
    """Run check_notes.sh with a simulated environment; return (stdout, env)."""
    notes = tmp_path / "iter-1.md"
    retry = tmp_path / ".iter-1.retries"
    if notes_text is not None:
        notes.write_text(notes_text)
    retry.write_text(str(retry_count))
    env = {
        **os.environ,
        "AUTOHELIX_NOTES_FILE": str(notes),
        "AUTOHELIX_NOTES_MIN_CHARS": str(min_chars),
        "AUTOHELIX_NOTES_MAX_RETRIES": str(max_retries),
        "AUTOHELIX_NOTES_RETRY_FILE": str(retry),
        "AUTOHELIX_ITERATION_START": str(int(__import__("time").time())),
        "AUTOHELIX_ITERATION_BUDGET": str(budget),
    }
    out = subprocess.run(
        ["bash", str(HOOK)], input="{}", capture_output=True, text=True, env=env
    )
    return out.stdout.strip(), retry


def test_hook_blocks_when_empty(tmp_path):
    out, retry = _run_hook(tmp_path, notes_text=None)
    assert '"decision":"block"' in out
    assert retry.read_text() == "1"  # counter incremented


def test_hook_blocks_when_too_short(tmp_path):
    out, _ = _run_hook(tmp_path, notes_text="short", retry_count=1)
    assert '"decision":"block"' in out


def test_hook_allows_when_enough_content(tmp_path):
    out, _ = _run_hook(tmp_path, notes_text="x" * 60)
    assert out == ""  # allow = no output


def test_hook_allows_when_retry_cap_reached(tmp_path):
    out, _ = _run_hook(tmp_path, notes_text="x", retry_count=3, max_retries=3)
    assert out == ""  # graceful give-up, no infinite loop


def test_hook_allows_near_deadline(tmp_path):
    # budget 30s, started ~now -> <60s remaining -> allow even though empty
    out, _ = _run_hook(tmp_path, notes_text=None, budget=30)
    assert out == ""


def test_hook_ignores_whitespace_only(tmp_path):
    # whitespace doesn't count toward min_chars
    out, _ = _run_hook(tmp_path, notes_text="   \n\n   \t  ")
    assert '"decision":"block"' in out
