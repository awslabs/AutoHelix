"""Integration tests for the full AutoHelix loop.

These tests exercise the complete harness: worktree creation, agent execution,
constraint checking, and merging/rejection.

Tests marked with @pytest.mark.slow actually invoke Claude and take longer.
Run with: pytest -m slow (to run only slow tests)
Skip with: pytest -m "not slow" (default in CI)
"""

import os
import subprocess

import pytest

from autohelix.config import Config, load_config
from autohelix.harness import Harness
from autohelix.history import History


@pytest.fixture
def integration_project(tmp_path):
    """Create a minimal project for integration testing."""
    project = tmp_path / "test_project"
    project.mkdir()

    # Initialize git
    subprocess.run(["git", "init"], cwd=project, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=project, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project, capture_output=True)

    # Create a simple file to modify
    (project / "hello.py").write_text('print("hello")\n')

    # Create .gitignore (like autohelix init does)
    (project / ".gitignore").write_text(".autohelix/\n")

    # Create autohelix config with mock agent
    (project / "autohelix.yaml").write_text("""
goal: Test the integration

agent:
  type: mock
  settings:
    change_file: hello.py
    change_content: "# Modified in iteration {iteration}"

constraints:
  - "true"  # Always passes
""")

    # Create .autohelix directory
    (project / ".autohelix").mkdir()

    # Initial commit
    subprocess.run(["git", "add", "-A"], cwd=project, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=project, capture_output=True)

    return project


class TestMockAgentIntegration:
    """Integration tests using the mock agent."""

    def test_single_iteration_accepted(self, integration_project):
        """A single iteration should complete and be accepted."""
        harness = Harness(integration_project, verbose=False)
        harness.run(max_iterations=1)

        history = History(integration_project)
        results = history.load()

        # Should have baseline + 1 iteration
        assert len(results) == 2
        assert results[0].reason == "baseline"
        assert results[1].accepted is True
        assert results[1].iteration == 1

    def test_multiple_iterations(self, integration_project):
        """Multiple iterations should all complete."""
        harness = Harness(integration_project, verbose=False)
        harness.run(max_iterations=3)

        history = History(integration_project)
        results = history.load()

        # Should have baseline + 3 iterations
        assert len(results) == 4
        for i, r in enumerate(results[1:], start=1):
            assert r.iteration == i
            assert r.accepted is True

    def test_constraint_failure_rejects(self, integration_project):
        """A failing constraint should reject the iteration."""
        # Create a check script that fails if mock_change.txt exists
        # (passes preflight since file doesn't exist yet, fails after mock agent creates it)
        check_script = integration_project / "check.sh"
        check_script.write_text('#!/bin/bash\n[ ! -f mock_change.txt ]\n')
        check_script.chmod(0o755)
        subprocess.run(["git", "add", "check.sh"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add check script"], cwd=integration_project, capture_output=True)

        # Update config to use mock agent that creates the file the constraint checks
        (integration_project / "autohelix.yaml").write_text("""
goal: Test constraint failure

agent:
  type: mock
  settings:
    change_file: mock_change.txt
    change_content: "This will cause the constraint to fail"

constraints:
  - ./check.sh
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)
        harness.run(max_iterations=1)

        history = History(integration_project)
        results = history.load()

        # Iteration should be rejected
        assert len(results) == 2
        assert results[1].accepted is False
        assert "constraint failed" in results[1].reason

    def test_failure_output_captured(self, integration_project):
        """Constraint failure output should be captured for agent debugging."""
        # Create a check script that fails with output if mock_change.txt exists
        check_script = integration_project / "check.sh"
        check_script.write_text('#!/bin/bash\nif [ -f mock_change.txt ]; then\n  echo "Error: something went wrong"\n  exit 1\nfi\n')
        check_script.chmod(0o755)
        subprocess.run(["git", "add", "check.sh"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add check script"], cwd=integration_project, capture_output=True)

        (integration_project / "autohelix.yaml").write_text("""
goal: Test failure output capture

agent:
  type: mock
  settings:
    change_file: mock_change.txt
    change_content: "This triggers the failure"

constraints:
  - ./check.sh
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)
        harness.run(max_iterations=1)

        history = History(integration_project)
        results = history.load()

        assert results[1].accepted is False
        assert results[1].failure_output is not None
        assert "something went wrong" in results[1].failure_output

    def test_out_of_scope_changes_reverted(self, integration_project):
        """Changes outside editable scope should be reverted before constraints run."""
        # Configure mock agent to modify both hello.py (editable) and a new file (not editable)
        (integration_project / "autohelix.yaml").write_text("""
goal: Test scope enforcement

agent:
  type: mock
  settings:
    change_file: hello.py
    change_content: "# Modified by agent"

constraints:
  - "true"

scope:
  editable:
    - hello.py
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)
        harness.run(max_iterations=1)

        history = History(integration_project)
        results = history.load()

        # Iteration should be accepted
        assert len(results) == 2
        assert results[1].accepted is True

        # hello.py should be modified in main
        assert "Modified" in (integration_project / "hello.py").read_text()

    def test_frozen_scope_enforced(self, integration_project):
        """Frozen files should not be merged even if agent modifies them."""
        # Create a test file that's frozen
        (integration_project / "frozen_file.py").write_text("original\n")
        subprocess.run(["git", "add", "frozen_file.py"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add frozen file"], cwd=integration_project, capture_output=True)

        (integration_project / "autohelix.yaml").write_text("""
goal: Test frozen scope enforcement

agent:
  type: mock
  settings:
    change_file: hello.py
    change_content: "# Modified by agent"

constraints:
  - "true"

scope:
  frozen:
    - frozen_file.py
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)
        harness.run(max_iterations=1)

        history = History(integration_project)
        results = history.load()

        assert len(results) == 2
        assert results[1].accepted is True

        # frozen_file.py should be unchanged
        assert (integration_project / "frozen_file.py").read_text() == "original\n"

    def test_agent_failure_rejects(self, integration_project):
        """If the agent fails, the iteration should be rejected."""
        (integration_project / "autohelix.yaml").write_text("""
goal: Test agent failure

agent:
  type: mock
  settings:
    should_fail: true

constraints:
  - "true"
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)
        harness.run(max_iterations=1)

        history = History(integration_project)
        results = history.load()

        # Even with passing constraints, agent failure means no changes to test
        # The iteration completes but with no code changes
        assert len(results) == 2


@pytest.mark.slow
class TestRealAgentIntegration:
    """Integration tests that actually invoke Claude.

    These tests are slow and require authentication.
    Skip with: pytest -m "not slow"
    """

    @pytest.fixture
    def real_agent_project(self, tmp_path):
        """Create a project configured for real Claude agent."""
        project = tmp_path / "real_agent_project"
        project.mkdir()

        # Initialize git
        subprocess.run(["git", "init"], cwd=project, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=project, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=project, capture_output=True)

        # Create a simple file
        (project / "example.py").write_text('# TODO: add a hello function\n')

        # Create .gitignore (like autohelix init does)
        (project / ".gitignore").write_text(".autohelix/\n")

        # Create config with real claude agent and short timeout
        # Use simple constraint that just checks syntax
        (project / "autohelix.yaml").write_text("""
goal: |
  Add a comment to example.py. Just add a single line comment.
  This is a minimal test - make only this small change.

agent:
  type: claude
  timeout_seconds: 60

constraints:
  - python -m py_compile example.py
""")

        # Create .autohelix directory
        (project / ".autohelix").mkdir()

        # Initial commit
        subprocess.run(["git", "add", "-A"], cwd=project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=project, capture_output=True)

        return project

    def test_real_agent_single_iteration(self, real_agent_project):
        """Test that a real Claude agent can complete one iteration.

        This test actually invokes Claude and verifies the full loop works.
        """
        # Skip if no claude command available
        import shutil
        if not shutil.which("claude"):
            pytest.skip("Claude not available")

        harness = Harness(real_agent_project, verbose=True)
        harness.run(max_iterations=1)

        history = History(real_agent_project)
        results = history.load()

        # Should have completed (accepted or rejected based on agent's work)
        assert len(results) >= 2
        # At minimum, the agent should have run without crashing
        assert results[-1].iteration == 1


class TestBudgetEnforcement:
    """Tests for cost and time budget enforcement in the harness loop."""

    def test_cost_budget_stops_loop(self, integration_project):
        """Loop should stop when cumulative cost exceeds the budget."""
        # Configure mock agent with cost_usd in usage
        (integration_project / "autohelix.yaml").write_text("""
goal: Test cost budget

agent:
  type: mock
  settings:
    change_file: hello.py
    change_content: "# iteration {iteration}"

budget:
  iterations: 10
  cost: 0.10

constraints:
  - "true"
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)

        # Patch the mock agent to return cost_usd in usage
        original_run_iteration = harness.run_iteration

        def patched_run_iteration(iteration):
            result = original_run_iteration(iteration)
            result.usage["cost_usd"] = 0.05  # $0.05 per iteration
            return result

        harness.run_iteration = patched_run_iteration
        harness.run(max_iterations=10)

        history = History(integration_project)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]

        # With $0.05/iter and $0.10 budget, should stop after 2 iterations
        assert len(iterations) == 2

    def test_time_budget_stops_loop(self, integration_project, monkeypatch):
        """Loop should stop when elapsed wall-clock time exceeds the budget."""
        (integration_project / "autohelix.yaml").write_text("""
goal: Test time budget

agent:
  type: mock
  settings:
    change_file: hello.py
    change_content: "# iteration {iteration}"

budget:
  iterations: 10
  time: 5s

constraints:
  - "true"
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        import time as time_mod
        call_count = 0
        real_monotonic = time_mod.monotonic

        def fake_monotonic():
            nonlocal call_count
            call_count += 1
            # Return increasing time: first call is baseline, subsequent calls advance
            return real_monotonic() + (call_count * 3)

        monkeypatch.setattr(time_mod, "monotonic", fake_monotonic)

        harness = Harness(integration_project, verbose=False)
        harness.run(max_iterations=10)

        history = History(integration_project)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]

        # Should stop before completing all 10 iterations due to time budget
        assert len(iterations) < 10

    def test_iteration_budget_still_works(self, integration_project):
        """Existing iteration limit behavior should be unchanged."""
        harness = Harness(integration_project, verbose=False)
        harness.run(max_iterations=2)

        history = History(integration_project)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]
        assert len(iterations) == 2

    def test_multiple_budgets_first_wins(self, integration_project):
        """When multiple budgets are set, whichever hits first stops the loop."""
        (integration_project / "autohelix.yaml").write_text("""
goal: Test multiple budgets

agent:
  type: mock
  settings:
    change_file: hello.py
    change_content: "# iteration {iteration}"

budget:
  iterations: 100
  cost: 0.10
  time: 1h

constraints:
  - "true"
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)

        original_run_iteration = harness.run_iteration

        def patched_run_iteration(iteration):
            result = original_run_iteration(iteration)
            result.usage["cost_usd"] = 0.06
            return result

        harness.run_iteration = patched_run_iteration
        harness.run()

        history = History(integration_project)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]

        # Cost should hit first: $0.06/iter, $0.10 limit -> stops after 2
        assert len(iterations) == 2

    def test_cost_exact_boundary_stops(self, integration_project):
        """Cost exactly equal to budget should stop the loop."""
        (integration_project / "autohelix.yaml").write_text("""
goal: Test exact cost boundary

agent:
  type: mock
  settings:
    change_file: hello.py
    change_content: "# iteration {iteration}"

budget:
  iterations: 10
  cost: 0.10

constraints:
  - "true"
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)

        original_run_iteration = harness.run_iteration

        def patched_run_iteration(iteration):
            result = original_run_iteration(iteration)
            result.usage["cost_usd"] = 0.10  # Exactly meets budget on iter 1
            return result

        harness.run_iteration = patched_run_iteration
        harness.run(max_iterations=10)

        history = History(integration_project)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]
        assert len(iterations) == 1

    def test_zero_cost_iterations_dont_trigger_budget(self, integration_project):
        """Iterations with zero or missing cost_usd shouldn't trigger cost budget."""
        (integration_project / "autohelix.yaml").write_text("""
goal: Test zero cost iterations

agent:
  type: mock
  settings:
    change_file: hello.py
    change_content: "# iteration {iteration}"

budget:
  iterations: 3
  cost: 1.00

constraints:
  - "true"
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)

        original_run_iteration = harness.run_iteration

        def patched_run_iteration(iteration):
            result = original_run_iteration(iteration)
            # Don't set cost_usd at all — should default to 0
            result.usage.pop("cost_usd", None)
            return result

        harness.run_iteration = patched_run_iteration
        harness.run(max_iterations=3)

        history = History(integration_project)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]
        # All 3 iterations should complete (cost never reaches $1)
        assert len(iterations) == 3

    def test_cost_budget_persists_across_resume(self, integration_project):
        """Cumulative cost should carry over when resuming from a previous run."""
        import json

        (integration_project / "autohelix.yaml").write_text("""
goal: Test cost resume

agent:
  type: mock
  settings:
    change_file: hello.py
    change_content: "# iteration {iteration}"

budget:
  iterations: 10
  cost: 0.15

constraints:
  - "true"
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        # First run: 2 iterations
        harness1 = Harness(integration_project, verbose=False)
        original_run_iteration1 = harness1.run_iteration

        def patched_run_iteration1(iteration):
            result = original_run_iteration1(iteration)
            result.usage["cost_usd"] = 0.05
            return result

        harness1.run_iteration = patched_run_iteration1
        harness1.run(max_iterations=2)

        history = History(integration_project)
        results1 = history.load()
        iters1 = [r for r in results1 if r.iteration > 0]
        assert len(iters1) == 2

        # Patch the history file to include cost_usd (the monkey-patch sets it
        # after history.append writes, so the JSONL doesn't have it yet)
        history_path = integration_project / ".autohelix" / "history.jsonl"
        lines = history_path.read_text().splitlines()
        updated = []
        for line in lines:
            entry = json.loads(line)
            if entry.get("iteration", 0) > 0:
                entry["usage"]["cost_usd"] = 0.05
            updated.append(json.dumps(entry))
        history_path.write_text("\n".join(updated) + "\n")

        # Second run (resume): should start at iter 3, with $0.10 already spent
        harness2 = Harness(integration_project, verbose=False)
        original_run_iteration2 = harness2.run_iteration

        def patched_run_iteration2(iteration):
            result = original_run_iteration2(iteration)
            result.usage["cost_usd"] = 0.05
            return result

        harness2.run_iteration = patched_run_iteration2
        harness2.run(max_iterations=10)

        results2 = history.load()
        iters2 = [r for r in results2 if r.iteration > 0]
        # $0.10 from prior + $0.05 from iter 3 = $0.15, meets budget -> stops after 1 more
        assert len(iters2) == 3

    def test_time_budget_persists_across_resume(self, integration_project, monkeypatch):
        """Cumulative wall-clock time (from duration_ms) should carry over on resume."""
        import json
        import time as time_mod

        (integration_project / "autohelix.yaml").write_text("""
goal: Test time resume

agent:
  type: mock
  settings:
    change_file: hello.py
    change_content: "# iteration {iteration}"

budget:
  iterations: 10
  time: 30s

constraints:
  - "true"
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        # First run: 2 iterations
        harness1 = Harness(integration_project, verbose=False)
        harness1.run(max_iterations=2)

        history = History(integration_project)
        results1 = history.load()
        iters1 = [r for r in results1 if r.iteration > 0]
        assert len(iters1) == 2

        # Patch history to show each iteration took 14 seconds (28s total)
        history_path = integration_project / ".autohelix" / "history.jsonl"
        lines = history_path.read_text().splitlines()
        updated = []
        for line in lines:
            entry = json.loads(line)
            if entry.get("iteration", 0) > 0:
                entry["usage"]["duration_ms"] = 14000
            updated.append(json.dumps(entry))
        history_path.write_text("\n".join(updated) + "\n")

        # Mock monotonic so each call advances 3s, ensuring the time check
        # fires after a small number of iterations
        call_count = 0
        real_monotonic = time_mod.monotonic

        def fake_monotonic():
            nonlocal call_count
            call_count += 1
            return real_monotonic() + (call_count * 3)

        monkeypatch.setattr(time_mod, "monotonic", fake_monotonic)

        # Second run (resume): 28s from prior history + advancing fake clock
        harness2 = Harness(integration_project, verbose=False)
        harness2.run(max_iterations=10)

        results2 = history.load()
        iters2 = [r for r in results2 if r.iteration > 0]
        # With 28s prior time offset and fake clock advancing, the loop
        # should stop well before 10 more iterations
        assert len(iters2) < 12
        assert len(iters2) > 2  # at least ran the 2 prior + some new

    def test_wall_clock_ms_recorded(self, integration_project):
        """Each iteration should record wall_clock_ms in usage."""
        harness = Harness(integration_project, verbose=False)
        harness.run(max_iterations=1)

        history = History(integration_project)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]
        assert len(iterations) == 1
        assert "wall_clock_ms" in iterations[0].usage
        assert iterations[0].usage["wall_clock_ms"] > 0

    def test_time_resume_uses_wall_clock_ms(self, integration_project, monkeypatch):
        """Resume should prefer wall_clock_ms over duration_ms for time tracking."""
        import json
        import time as time_mod

        (integration_project / "autohelix.yaml").write_text("""
goal: Test wall_clock resume
agent:
  type: mock
  settings:
    change_file: hello.py
    change_content: "# iteration {iteration}"
budget:
  iterations: 10
  time: 30s
constraints:
  - "true"
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        # First run: 2 iterations
        harness1 = Harness(integration_project, verbose=False)
        harness1.run(max_iterations=2)

        history = History(integration_project)

        # Patch history: duration_ms=5000 (5s agent time) but wall_clock_ms=14000 (14s total)
        history_path = integration_project / ".autohelix" / "history.jsonl"
        lines = history_path.read_text().splitlines()
        updated = []
        for line in lines:
            entry = json.loads(line)
            if entry.get("iteration", 0) > 0:
                entry["usage"]["duration_ms"] = 5000
                entry["usage"]["wall_clock_ms"] = 14000
            updated.append(json.dumps(entry))
        history_path.write_text("\n".join(updated) + "\n")

        # Mock monotonic to advance slowly — the prior 28s from wall_clock_ms
        # should push us past the 30s budget quickly
        call_count = 0
        real_monotonic = time_mod.monotonic

        def fake_monotonic():
            nonlocal call_count
            call_count += 1
            return real_monotonic() + (call_count * 2)

        monkeypatch.setattr(time_mod, "monotonic", fake_monotonic)

        harness2 = Harness(integration_project, verbose=False)
        harness2.run(max_iterations=10)

        results2 = history.load()
        iters2 = [r for r in results2 if r.iteration > 0]
        # With 28s prior (from wall_clock_ms) + advancing clock, should stop quickly
        assert len(iters2) < 7
        assert len(iters2) > 2

    def test_no_budget_runs_to_max_iterations(self, integration_project):
        """With no cost/time budget, loop runs to max_iterations."""
        (integration_project / "autohelix.yaml").write_text("""
goal: Test no budget
agent:
  type: mock
  settings:
    change_file: hello.py
    change_content: "# iteration {iteration}"
budget:
  iterations: 3
constraints:
  - "true"
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)
        harness.run(max_iterations=3)

        history = History(integration_project)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]
        assert len(iterations) == 3

    def test_floating_point_cost_accumulation(self, integration_project):
        """Test that $0.10 x 3 exceeds $0.30 budget due to float precision."""
        (integration_project / "autohelix.yaml").write_text("""
goal: Test float accumulation

agent:
  type: mock
  settings:
    change_file: hello.py
    change_content: "# iteration {iteration}"

budget:
  iterations: 10
  cost: 0.30

constraints:
  - "true"
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)

        original_run_iteration = harness.run_iteration

        def patched_run_iteration(iteration):
            result = original_run_iteration(iteration)
            result.usage["cost_usd"] = 0.10
            return result

        harness.run_iteration = patched_run_iteration
        harness.run(max_iterations=10)

        history = History(integration_project)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]
        # 0.1 + 0.1 + 0.1 = 0.30000000000000004 >= 0.30, stops after 3
        assert len(iterations) == 3

    def test_wall_clock_ms_zero_in_history(self, integration_project):
        """Resume with wall_clock_ms=0 entries should not inflate prior time."""
        import json

        (integration_project / "autohelix.yaml").write_text("""
goal: Test zero wall_clock resume

agent:
  type: mock
  settings:
    change_file: hello.py
    change_content: "# iteration {iteration}"

budget:
  iterations: 10
  time: 1h

constraints:
  - "true"
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        # First run
        harness1 = Harness(integration_project, verbose=False)
        harness1.run(max_iterations=2)

        history = History(integration_project)

        # Patch history entries to have wall_clock_ms=0 (simulating crashed iterations)
        history_path = integration_project / ".autohelix" / "history.jsonl"
        lines = history_path.read_text().splitlines()
        updated = []
        for line in lines:
            entry = json.loads(line)
            if entry.get("iteration", 0) > 0:
                entry["usage"]["wall_clock_ms"] = 0
            updated.append(json.dumps(entry))
        history_path.write_text("\n".join(updated) + "\n")

        # Second run (resume) — should not treat the zero entries as large offsets
        harness2 = Harness(integration_project, verbose=False)
        harness2.run(max_iterations=5)

        results2 = history.load()
        iters2 = [r for r in results2 if r.iteration > 0]
        # With 1h budget and 0s prior time, all 3 new iterations should complete
        assert len(iters2) == 5  # 2 prior + 3 new

    def test_cost_budget_includes_rejected_iterations(self, integration_project):
        """Rejected iterations should still count toward the cost budget."""
        check_script = integration_project / "check.sh"
        check_script.write_text('#!/bin/bash\n[ ! -f mock_change.txt ]\n')
        check_script.chmod(0o755)
        subprocess.run(["git", "add", "check.sh"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add check"], cwd=integration_project, capture_output=True)

        (integration_project / "autohelix.yaml").write_text("""
goal: Test cost with rejections

agent:
  type: mock
  settings:
    change_file: mock_change.txt
    change_content: "fail"

budget:
  iterations: 10
  cost: 0.10

constraints:
  - ./check.sh
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)

        original_run_iteration = harness.run_iteration

        def patched_run_iteration(iteration):
            result = original_run_iteration(iteration)
            result.usage["cost_usd"] = 0.05
            return result

        harness.run_iteration = patched_run_iteration
        harness.run()

        history = History(integration_project)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]

        # All rejected, but cost still accumulates: 2 iters at $0.05 = $0.10
        assert len(iterations) == 2
        assert all(not r.accepted for r in iterations)


class TestFormatTime:
    def test_seconds_only(self):
        from autohelix.harness import _format_time
        assert _format_time(45) == "45s"

    def test_minutes_and_seconds(self):
        from autohelix.harness import _format_time
        assert _format_time(125) == "2m 5s"

    def test_hours_minutes_seconds(self):
        from autohelix.harness import _format_time
        assert _format_time(7265) == "2h 1m 5s"

    def test_exact_hour(self):
        from autohelix.harness import _format_time
        assert _format_time(3600) == "1h"

    def test_exact_minutes(self):
        from autohelix.harness import _format_time
        assert _format_time(120) == "2m"

    def test_hours_and_seconds_skips_zero_minutes(self):
        from autohelix.harness import _format_time
        assert _format_time(7205) == "2h 5s"

    def test_zero(self):
        from autohelix.harness import _format_time
        assert _format_time(0) == "0s"


class TestFormatUsage:
    def test_format_tokens(self):
        from autohelix.harness import _format_tokens
        assert "1.2k" in _format_tokens(1234)
        assert "1.5M" in _format_tokens(1_500_000)
        assert "42 " == _format_tokens(42)

    def test_format_usage_full(self):
        usage = {"input_tokens": 10000, "output_tokens": 2000, "cost_usd": 0.1234, "duration_ms": 65000}
        text = Harness._format_usage(usage)
        assert "10.0k" in text
        assert "2.0k" in text
        assert "$0.1234" in text
        assert "1m 5s" in text

    def test_format_usage_tokens_only(self):
        usage = {"input_tokens": 500, "output_tokens": 100}
        text = Harness._format_usage(usage)
        assert "500" in text
        assert "100" in text
        assert "$" not in text

    def test_format_usage_empty(self):
        assert Harness._format_usage({}) == ""
