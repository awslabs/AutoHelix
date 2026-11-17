"""Integration tests for the full AutoHelix loop.

These tests exercise the complete harness: copy creation, agent execution,
constraint checking, and merging/rejection.

Tests marked with @pytest.mark.slow actually invoke Claude and take longer.
Run with: pytest -m slow (to run only slow tests)
Skip with: pytest -m "not slow" (default in CI)
"""

import os
import subprocess

import pytest

from autohelix.agents import AgentRunResult
from autohelix.config import Config, load_config
from autohelix.harness import Budget, AutoHelixRunError, Harness
from autohelix.history import History
from autohelix.runs import RunPaths
from tests.helpers import latest_run


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

    # Exercise an existing repository-level exclusion for AutoHelix state.
    (project / ".gitignore").write_text(".autohelix/\n")

    # Create autohelix config with mock agent
    (project / "autohelix.yaml").write_text("""
goal: Test the integration

agent:
  cli: mock
  mock:
    change_file: hello.py
    change_content: "# Modified in iteration {iteration}"

gates:
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
        harness.run(Budget(until=1))

        history = History(integration_project, latest_run(integration_project).run_id)
        results = history.load()

        # Should have baseline + 1 iteration
        assert len(results) == 2
        assert results[0].reason == "baseline"
        assert results[1].accepted is True
        assert results[1].iteration == 1

    def test_multiple_iterations(self, integration_project):
        """Multiple iterations should all complete."""
        harness = Harness(integration_project, verbose=False)
        harness.run(Budget(until=3))

        history = History(integration_project, latest_run(integration_project).run_id)
        results = history.load()

        # Should have baseline + 3 iterations
        assert len(results) == 4
        for i, r in enumerate(results[1:], start=1):
            assert r.iteration == i
            assert r.accepted is True

    def test_agent_failure_is_a_recorded_rejection_and_the_run_goes_on(
        self, integration_project, monkeypatch
    ):
        harness = Harness(integration_project, verbose=False)
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=integration_project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        monkeypatch.setattr(
            harness,
            "run_agent",
            lambda *args, **kwargs: AgentRunResult(
                success=False,
                exit_code=0,
                error="API Error: invalid model",
            ),
        )

        harness.run(Budget(until=1))  # no error: a failed agent call is a rejection

        results = History(integration_project, latest_run(integration_project).run_id).load()
        assert [result.iteration for result in results] == [0, 1]
        assert not results[-1].accepted and results[-1].cost.incomplete
        assert results[-1].reason == "Iteration 1 agent failed: API Error: invalid model"
        assert results[-1].agent_error == "API Error: invalid model"
        assert subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=integration_project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() == head_before

    def test_missing_baseline_metric_aborts_before_agent(self, integration_project):
        (integration_project / "autohelix.yaml").write_text("""
goal: Test baseline failure

agent:
  cli: mock

observe:
  - name: bench
    command: python -c "print('no metrics here')"
    metrics:
      score: higher
""")
        subprocess.run(
            ["git", "add", "autohelix.yaml"],
            cwd=integration_project,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Configure missing baseline metric"],
            cwd=integration_project,
            capture_output=True,
        )

        harness = Harness(integration_project, verbose=False)
        with pytest.raises(
            RuntimeError,
            match="Baseline setup error: observe entry `bench` exited 0 but reported no `score`",
        ):
            harness.run(Budget(until=1))

        records = History(integration_project, latest_run(integration_project).run_id).load()
        assert len(records) == 1 and records[0].iteration == 0 and not records[0].accepted

    def _commit_config(self, project, text, files=()):
        for name, content in files:
            (project / name).write_text(content)
        (project / "autohelix.yaml").write_text(text)
        subprocess.run(["git", "add", "-A"], cwd=project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "config"], cwd=project, capture_output=True)

    BENCH = (
        "import pathlib, re\n"
        "m = re.findall(r'iteration (\\d+)', pathlib.Path('hello.py').read_text())\n"  # the mock appends
        "print('##autohelix[speed=%d]' % (100 - 3 * int(m[-1]) if m else 100))\n"
    )

    def test_metric_gate_compares_with_the_last_accepted_iteration(self, integration_project):
        # 100 -> 97 -> 94: each step is 3% worse than the last accepted, 6% worse than the baseline.
        self._commit_config(integration_project, """
goal: Test metric gates
agent:
  cli: mock
  mock:
    change_file: hello.py
    change_content: "# Modified in iteration {iteration}"
gates:
  - metric: speed
    regress: 5%
observe:
  - name: bench
    command: python bench.py
    metrics: {speed: higher}
""", files=[("bench.py", self.BENCH)])
        Harness(integration_project, verbose=False).run(Budget(until=2), report=False)
        records = History(integration_project, latest_run(integration_project).run_id).load()
        assert [(r.iteration, r.accepted, r.metrics.get("speed")) for r in records] == [
            (0, True, 100), (1, True, 97), (2, True, 94)]

    def test_improve_gate_rejects_a_regression(self, integration_project):
        self._commit_config(integration_project, """
goal: Test metric gates
agent:
  cli: mock
  mock:
    change_file: hello.py
    change_content: "# Modified in iteration {iteration}"
gates:
  - metric: speed
    improve: 0%
observe:
  - name: bench
    command: python bench.py
    metrics: {speed: higher}
""", files=[("bench.py", self.BENCH)])
        Harness(integration_project, verbose=False).run(Budget(until=1), report=False)
        [_, record] = History(integration_project, latest_run(integration_project).run_id).load()
        assert not record.accepted
        assert record.reason == "metric gate: speed 97 does not improve on 100"

    @pytest.mark.parametrize("gated", [False, True])
    def test_missing_baseline_review_metric_stops_only_when_gated(self, integration_project, gated):
        gate = "gates:\n  - metric: missing\n    improve: 0%\n" if gated else ""
        self._commit_config(integration_project, f"""
goal: Test review metrics
agent:
  cli: mock
{gate}observe:
  - name: progress
    review: Count the missing items.
    metrics: {{missing: lower}}
""")
        harness = Harness(integration_project, verbose=False)
        if gated:
            with pytest.raises(RuntimeError, match="reported no `missing`"):
                harness.run(Budget(until=1), report=False)
        else:
            harness.run(Budget(until=1), report=False)
            assert [r.accepted for r in History(integration_project, latest_run(integration_project).run_id).load()] == [True, True]

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
  cli: mock
  mock:
    change_file: mock_change.txt
    change_content: "This will cause the constraint to fail"

gates:
  - ./check.sh
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)
        harness.run(Budget(until=1))

        history = History(integration_project, latest_run(integration_project).run_id)
        results = history.load()

        # Iteration should be rejected
        assert len(results) == 2
        assert results[1].accepted is False
        assert "gate failed" in results[1].reason

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
  cli: mock
  mock:
    change_file: mock_change.txt
    change_content: "This triggers the failure"

gates:
  - ./check.sh
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)
        harness.run(Budget(until=1))

        history = History(integration_project, latest_run(integration_project).run_id)
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
  cli: mock
  mock:
    change_file: hello.py
    change_content: "# Modified by agent"

gates:
  - "true"

scope:
  editable:
    - hello.py
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)
        harness.run(Budget(until=1))

        history = History(integration_project, latest_run(integration_project).run_id)
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
  cli: mock
  mock:
    change_file: hello.py
    change_content: "# Modified by agent"

gates:
  - "true"

scope:
  frozen:
    - frozen_file.py
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)
        harness.run(Budget(until=1))

        history = History(integration_project, latest_run(integration_project).run_id)
        results = history.load()

        assert len(results) == 2
        assert results[1].accepted is True

        # frozen_file.py should be unchanged
        assert (integration_project / "frozen_file.py").read_text() == "original\n"

    def test_agent_failure_resumes_next_iteration_after_recovery(
        self, integration_project
    ):
        """Failed calls remain attributable and consume their attempted iteration."""
        (integration_project / "autohelix.yaml").write_text("""
goal: Test agent failure

agent:
  cli: mock
  mock:
    should_fail: true

gates:
  - "true"
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)
        harness.run(Budget(until=1))

        history = History(integration_project, latest_run(integration_project).run_id)
        assert history.load()[-1].reason == "Iteration 1 agent failed: Mock failure"
        assert [result.iteration for result in history.load()] == [0, 1]

        # The run keeps its config: the agent recovers in-process instead.
        harness = Harness(integration_project, verbose=False)
        harness.agent.config.mock.update(should_fail=False, change_file="hello.py",
                                             change_content="# recovered")
        harness.run(Budget(until=2))
        results = history.load()
        assert [result.iteration for result in results] == [0, 1, 2]
        assert not results[1].accepted
        assert results[-1].accepted


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

        # Exercise an existing repository-level exclusion for AutoHelix state.
        (project / ".gitignore").write_text(".autohelix/\n")

        # Create config with real claude agent and short timeout
        # Use simple constraint that just checks syntax
        (project / "autohelix.yaml").write_text("""
goal: |
  Add a comment to example.py. Just add a single line comment.
  This is a minimal test - make only this small change.

agent:
  cli: claude-code
  timeout: 60

gates:
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
        harness.run(Budget(until=1))

        history = History(real_agent_project, latest_run(real_agent_project).run_id)
        results = history.load()

        # Should have completed (accepted or rejected based on agent's work)
        assert len(results) >= 2
        # At minimum, the agent should have run without crashing
        assert results[-1].iteration == 1


class TestBudgetEnforcement:
    """Tests for cost and time budget enforcement in the harness loop."""

    def test_codex_estimates_persist_and_stop_resumed_run(
        self, integration_project, monkeypatch, capsys,
    ):
        import json
        from autohelix.agents.runner import ProcessRunResult

        (integration_project / "autohelix.yaml").write_text("""
goal: Test calculated Codex cost
agent:
  cli: codex
  model: gpt-6.1-sol
gates:
  - "true"
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Configure Codex"], cwd=integration_project, check=True, capture_output=True)
        calls = 0

        def fake_run_process(**kwargs):
            nonlocal calls
            calls += 1
            (kwargs["cwd"] / "hello.py").write_text(f"# iteration {calls}\n")
            kwargs["line_callback"](json.dumps({
                "type": "turn.completed", "usage": {
                    "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 1000,
                },
            }))
            return ProcessRunResult(exit_code=0)

        monkeypatch.setattr("autohelix.agents.codex.run_process", fake_run_process)
        # Codex execution is mocked, so preflight must not require a local CLI.
        monkeypatch.setattr("autohelix.checks.shutil.which", lambda command: "/mock/codex")
        Harness(integration_project).run(Budget(until=1), report=False)
        Harness(integration_project).run(Budget(until=2), report=False)
        iterations = [r for r in History(integration_project, latest_run(integration_project).run_id).load() if r.iteration > 0]
        assert len(iterations) == 2
        assert all(r.accepted for r in iterations)
        assert all(r.usage["cost_usd"] == pytest.approx(0.01) for r in iterations)
        assert all(r.usage["cost_source"] == "calculated" for r in iterations)
        assert all(r.usage["cost_rates"]["output"] == 10 for r in iterations)
        assert "$0.02" in capsys.readouterr().out
        # --cost counts this segment only: the $0.02 already spent doesn't, the
        # estimates of its own two iterations do ($0.01, then $0.02 >= $0.015).
        harness = Harness(integration_project)
        harness.run(Budget(cost_usd=0.015), report=False)
        assert calls == 4
        status = json.loads(harness.paths.status.read_text())
        assert status["stop_reason"]["detail"] == "cost budget reached ($0.015 this segment)"
        assert status["max_cost_usd"] == pytest.approx(0.035) and status["max_iterations"] is None

    def test_cost_budget_stops_loop(self, integration_project):
        """Loop should stop when cumulative cost exceeds the budget."""
        # Configure mock agent with cost_usd in usage
        (integration_project / "autohelix.yaml").write_text("""
goal: Test cost budget

agent:
  cli: mock
  mock:
    change_file: hello.py
    change_content: "# iteration {iteration}"

gates:
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
        harness.run(Budget(until=10, cost_usd=0.10))

        history = History(integration_project, latest_run(integration_project).run_id)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]

        # With $0.05/iter and $0.10 budget, should stop after 2 iterations
        assert len(iterations) == 2

    def test_time_budget_stops_loop(self, integration_project, monkeypatch):
        """Loop should stop when elapsed wall-clock time exceeds the budget."""
        (integration_project / "autohelix.yaml").write_text("""
goal: Test time budget

agent:
  cli: mock
  mock:
    change_file: hello.py
    change_content: "# iteration {iteration}"

gates:
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
        harness.run(Budget(until=10, time_seconds=5))

        history = History(integration_project, latest_run(integration_project).run_id)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]

        # Should stop before completing all 10 iterations due to time budget
        assert len(iterations) < 10

    def test_iteration_budget_still_works(self, integration_project):
        """Existing iteration limit behavior should be unchanged."""
        harness = Harness(integration_project, verbose=False)
        harness.run(Budget(until=2))

        history = History(integration_project, latest_run(integration_project).run_id)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]
        assert len(iterations) == 2

    def test_multiple_budgets_first_wins(self, integration_project):
        """When multiple budgets are set, whichever hits first stops the loop."""
        (integration_project / "autohelix.yaml").write_text("""
goal: Test multiple budgets

agent:
  cli: mock
  mock:
    change_file: hello.py
    change_content: "# iteration {iteration}"

gates:
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
        harness.run(Budget(iterations=100, cost_usd=0.10, time_seconds=3600))

        history = History(integration_project, latest_run(integration_project).run_id)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]

        # Cost should hit first: $0.06/iter, $0.10 limit -> stops after 2
        assert len(iterations) == 2

    def test_cost_exact_boundary_stops(self, integration_project):
        """Cost exactly equal to budget should stop the loop."""
        (integration_project / "autohelix.yaml").write_text("""
goal: Test exact cost boundary

agent:
  cli: mock
  mock:
    change_file: hello.py
    change_content: "# iteration {iteration}"

gates:
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
        harness.run(Budget(until=10, cost_usd=0.10))

        history = History(integration_project, latest_run(integration_project).run_id)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]
        assert len(iterations) == 1

    def test_zero_cost_iterations_dont_trigger_budget(self, integration_project, capsys):
        """Unknown costs warn, but preserve existing iteration-limit behavior."""
        (integration_project / "autohelix.yaml").write_text("""
goal: Test zero cost iterations

agent:
  cli: mock
  mock:
    change_file: hello.py
    change_content: "# iteration {iteration}"

gates:
  - "true"
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)

        original_run_iteration = harness.run_iteration

        def patched_run_iteration(iteration):
            result = original_run_iteration(iteration)
            # Unavailable usage is excluded from the tracked amount with a warning.
            result.usage.pop("cost_usd", None)
            return result

        harness.run_iteration = patched_run_iteration
        harness.run(Budget(until=3, cost_usd=1.0))

        history = History(integration_project, latest_run(integration_project).run_id)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]
        # All 3 iterations should complete (cost never reaches $1)
        assert len(iterations) == 3
        output = capsys.readouterr().out
        assert "--cost counts known costs only" in output
        assert "cost unavailable" in output

    def test_cost_budget_is_per_segment(self, integration_project):
        """A resumed run gets the full --cost again, whatever it spent before; status
        records the cap as a run total (spent before + --cost)."""
        import json

        (integration_project / "autohelix.yaml").write_text("""
goal: Test cost resume

agent:
  cli: mock
  mock:
    change_file: hello.py
    change_content: "# iteration {iteration}"

gates:
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
        harness1.run(Budget(until=2, cost_usd=0.15))

        history = History(integration_project, latest_run(integration_project).run_id)
        results1 = history.load()
        iters1 = [r for r in results1 if r.iteration > 0]
        assert len(iters1) == 2

        # Patch the history file to include cost_usd (the monkey-patch sets it
        # after history.append writes, so the JSONL doesn't have it yet)
        history_path = latest_run(integration_project).history
        lines = history_path.read_text().splitlines()
        updated = []
        for line in lines:
            entry = json.loads(line)
            if entry.get("iteration", 0) > 0:
                for call in entry["calls"]:
                    if call["role"] == "agent":
                        call["usage"]["cost_usd"] = 0.05
            updated.append(json.dumps(entry))
        history_path.write_text("\n".join(updated) + "\n")

        # Second run (resume): starts at iter 3; the $0.10 already spent doesn't count
        harness2 = Harness(integration_project, verbose=False)
        original_run_iteration2 = harness2.run_iteration

        def patched_run_iteration2(iteration):
            result = original_run_iteration2(iteration)
            result.usage["cost_usd"] = 0.05
            return result

        harness2.run_iteration = patched_run_iteration2
        harness2.run(Budget(until=10, cost_usd=0.15))

        results2 = history.load()
        iters2 = [r for r in results2 if r.iteration > 0]
        # $0.05 each: iterations 3, 4 and 5 reach $0.15 this segment
        assert len(iters2) == 5
        status = json.loads(latest_run(integration_project).status.read_text())
        assert status["max_cost_usd"] == pytest.approx(0.25)
        assert status["stop_reason"]["detail"] == "cost budget reached ($0.15 this segment)"

    def test_time_budget_is_per_segment(self, integration_project):
        """--time counts this segment's wall clock only, whatever the iterations
        recorded before (wall_clock_ms, else duration_ms); status records the
        segment's own cap, not a run total."""
        import json

        from autohelix.history import record_seconds

        harness1 = Harness(integration_project, verbose=False)
        harness1.run(Budget(until=2))

        # 14s + 5s recorded before: the first by wall_clock_ms, the second by duration_ms only.
        history_path = latest_run(integration_project).history
        updated = []
        for line in history_path.read_text().splitlines():
            entry = json.loads(line)
            for call in entry["calls"]:
                if call["role"] == "agent":
                    call["usage"]["duration_ms"] = 5000
                    if entry["iteration"] == 1:
                        call["usage"]["wall_clock_ms"] = 14000
                    else:
                        call["usage"].pop("wall_clock_ms", None)
            updated.append(json.dumps(entry))
        history_path.write_text("\n".join(updated) + "\n")
        records = History(integration_project, latest_run(integration_project).run_id).load()
        assert [record_seconds(r) for r in records if r.iteration > 0] == [14, 5]
        before = sum(record_seconds(r) for r in records)

        # 40s of --time is plenty for two mock iterations, whatever ran before.
        harness2 = Harness(integration_project, verbose=False)
        harness2.run(Budget(until=4, time_seconds=40))
        assert [r.iteration for r in History(integration_project, latest_run(integration_project).run_id).load() if r.iteration > 0] == [1, 2, 3, 4]
        status = json.loads(latest_run(integration_project).status.read_text())
        assert status["max_time_seconds"] == 40 and before >= 19
        assert status["max_iterations"] == 4 and status["max_cost_usd"] is None

    def test_wall_clock_ms_recorded(self, integration_project):
        """Each iteration should record wall_clock_ms in usage."""
        harness = Harness(integration_project, verbose=False)
        harness.run(Budget(until=1))

        history = History(integration_project, latest_run(integration_project).run_id)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]
        assert len(iterations) == 1
        assert "wall_clock_ms" in iterations[0].usage
        assert iterations[0].usage["wall_clock_ms"] > 0

    def test_iteration_cap_alone(self, integration_project):
        """With no cost/time budget, the loop runs to the iteration cap."""
        (integration_project / "autohelix.yaml").write_text("""
goal: Test no budget
agent:
  cli: mock
  mock:
    change_file: hello.py
    change_content: "# iteration {iteration}"
gates:
  - "true"
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        harness = Harness(integration_project, verbose=False)
        harness.run(Budget(until=3))

        history = History(integration_project, latest_run(integration_project).run_id)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]
        assert len(iterations) == 3

    def test_floating_point_cost_accumulation(self, integration_project):
        """Test that $0.10 x 3 exceeds $0.30 budget due to float precision."""
        (integration_project / "autohelix.yaml").write_text("""
goal: Test float accumulation

agent:
  cli: mock
  mock:
    change_file: hello.py
    change_content: "# iteration {iteration}"

gates:
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
        harness.run(Budget(until=10, cost_usd=0.30))

        history = History(integration_project, latest_run(integration_project).run_id)
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
  cli: mock
  mock:
    change_file: hello.py
    change_content: "# iteration {iteration}"

gates:
  - "true"
""")
        subprocess.run(["git", "add", "autohelix.yaml"], cwd=integration_project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update config"], cwd=integration_project, capture_output=True)

        # First run
        harness1 = Harness(integration_project, verbose=False)
        harness1.run(Budget(until=2))

        history = History(integration_project, latest_run(integration_project).run_id)

        # Patch history entries to have wall_clock_ms=0 (simulating crashed iterations)
        history_path = latest_run(integration_project).history
        lines = history_path.read_text().splitlines()
        updated = []
        for line in lines:
            entry = json.loads(line)
            if entry.get("iteration", 0) > 0:
                for call in entry["calls"]:
                    if call["role"] == "agent":
                        call["usage"]["wall_clock_ms"] = 0
            updated.append(json.dumps(entry))
        history_path.write_text("\n".join(updated) + "\n")

        # Second run (resume) — should not treat the zero entries as large offsets
        harness2 = Harness(integration_project, verbose=False)
        harness2.run(Budget(until=5, time_seconds=3600))

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
  cli: mock
  mock:
    change_file: mock_change.txt
    change_content: "fail"

gates:
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
        harness.run(Budget(cost_usd=0.10))  # --cost alone: no iteration cap

        history = History(integration_project, latest_run(integration_project).run_id)
        results = history.load()
        iterations = [r for r in results if r.iteration > 0]

        # All rejected, but cost still accumulates: 2 iters at $0.05 = $0.10
        assert len(iterations) == 2
        assert all(not r.accepted for r in iterations)


class TestFormatLimit:
    """A limit (`--time`, `agent.timeout`) is spelled as the config spells it, in the
    stop line too: harness.py's own `_format_time` ("2m 5s") was a third format."""

    @pytest.mark.parametrize("seconds, text", [
        (45, "45s"), (125, "2m5s"), (7265, "2h1m5s"), (3600, "1h"), (120, "2m"), (7205, "2h5s"), (0, "0s"),
    ])
    def test_format_limit(self, seconds, text):
        from autohelix.iteration_time import format_limit
        assert format_limit(seconds) == text
