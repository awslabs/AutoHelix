"""The suite doesn't inherit the environment it was started in (root conftest.py `clean_env`).

Run inside an AutoHelix run (a `pytest` gate, an agent's shell), the harness's variables
would otherwise send tests' `autohelix ask` usage records and transcripts to the
enclosing iteration."""

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Tests that write through these variables when they leak in, or fail under them.
LEAKY = [
    "tests/test_judge.py",
    "tests/test_review.py::test_harness_commands_run_autohelix_through_the_launcher",
    "tests/test_iteration_time.py::test_claude_hook_emits_valid_json",
    "tests/test_agents.py::TestClaudeCodeAutoMemory",
    "tests/test_copies_e2e.py::test_swallowed_failed_ask_passes_and_keeps_its_usage",
]


def test_each_test_starts_from_a_clean_environment(copies_dir, tmp_path_factory):
    harness = {name for name in os.environ if name.startswith("AUTOHELIX_")}
    assert harness == {"AUTOHELIX_COPIES_ROOT", "AUTOHELIX_HOOK_AUDIT_LOG"}
    assert os.environ["AUTOHELIX_COPIES_ROOT"] == str(copies_dir)
    assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY" not in os.environ
    assert os.environ["GIT_CONFIG_GLOBAL"].startswith(str(tmp_path_factory.getbasetemp()))
    assert os.environ["COLUMNS"] == "80"


def test_a_run_inside_a_harness_command_writes_nothing_outside_its_tmp_path(tmp_path):
    outer = tmp_path / "outer"  # what an enclosing run's gate would point at
    (outer / "logs").mkdir(parents=True)
    (outer / "run" / "logs").mkdir(parents=True)
    signing = tmp_path / "gitconfig"  # a global config whose commits can't be signed
    signing.write_text("[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n")
    env = {
        **os.environ,
        "AUTOHELIX_USAGE_FILE": str(outer / "usage.jsonl"),
        "AUTOHELIX_LOG_DIR": str(outer / "logs"),
        "AUTOHELIX_RUN_DIR": str(outer / "run"),
        "AUTOHELIX_RUN_ID": "20260101-0000-outer-abcd",
        "AUTOHELIX_PROJECT": str(outer),
        "AUTOHELIX_CONFIG": str(outer / "config.yaml"),
        "AUTOHELIX_CANDIDATE": "0" * 40,
        "AUTOHELIX_BASE": "0" * 40,
        "AUTOHELIX_SCRATCH": str(outer / "scratch"),
        "AUTOHELIX_COMMAND_DEADLINE": str(time.time() + 30),  # little time left in the outer gate
        "AUTOHELIX_HOOK_AUDIT_LOG": str(outer / "hook_audit.log"),
        "AUTOHELIX_COPIES_ROOT": str(outer / "copies"),
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "GIT_CONFIG_GLOBAL": str(signing),
        "COLUMNS": "37",
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"--basetemp={tmp_path / 'inner'}", *LEAKY],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    written = sorted(str(p.relative_to(outer)) for p in outer.rglob("*") if p.is_file())
    assert written == []
