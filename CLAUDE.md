# AutoHelix Development

AutoHelix is an agent-powered iterative optimization tool. It runs AI agents in a loop — each iteration is isolated in a git worktree, validated, and only merged when it passes. See `README.md` for user-facing docs. For setting up training/eval (ML benchmark) cells, see `docs/ml-experiments.md`.

## Setup

Activate the virtual environment before running any commands:

```bash
source .venv/bin/activate
```

If the venv doesn't exist yet:

```bash
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

## Project Layout

- `src/autohelix/harness.py` - main iteration loop (start here)
- `src/autohelix/cli.py` - CLI commands (init, run, clear, watch, report)
- `src/autohelix/config.py` - configuration dataclasses and parsing
- `src/autohelix/agents/` - agent backends (claudecode, codex, opencode, mock)
- `src/autohelix/sandbox.py` - git worktree management and scope enforcement
- `src/autohelix/checks.py` - constraint and metric execution
- `src/autohelix/history.py` - iteration history (JSONL)
- `src/autohelix/display.py` - live Rich display
- `src/autohelix/prompt_template.py` - agent prompt rendering
- `tests/` - test suite (pytest)
- `examples/` - example projects (sorting, ml-recipe, writing,
  task-queue, research, workflow-optimization, algotune, posttrain,
  kernelbench, nested-autohelix)
- `scripts/run_dev_test.sh` - quick real-agent test against a bundled example (supports `--parallel N`)
- `docs/` - user-facing documentation (plain markdown)

## CLI Commands

```
autohelix init         # Initialize project (creates .autohelix/ and config template)
autohelix run          # Start or resume optimization loop
autohelix parallel     # Run multiple workers in parallel on the same goal
autohelix clear        # Archive state, start fresh
autohelix watch        # Live-tail agent output (second terminal)
autohelix hint         # Send a hint to the running agent (picked up next iteration)
autohelix report       # Agent-written analysis of the run
```

## Testing

```bash
pytest                    # run all fast tests
pytest -m slow            # run slow tests (requires real agent)
bash scripts/run_dev_test.sh              # manual test with real agent (sorting, 1 iteration)
bash scripts/run_dev_test.sh sorting --parallel 2  # exercise `autohelix parallel` with identical workers
```

## Docs

User-facing docs are plain markdown in `docs/` (see `docs/README.md` for the index).

## Examples Warning

**Do not commit optimized example code.** Examples should always contain the slow baseline version so users can see the optimization potential. If you run an example and it gets optimized, do NOT commit those changes to the main repo.

## Architecture Notes

- Entry points: `autohelix init` scaffolds `.autohelix/` and writes a config template. `autohelix run` loads config and starts the harness loop.
- Iteration isolation: each iteration runs in a git worktree. On acceptance, the worktree branch is merged into main. On rejection, it's discarded.
- Scope enforcement: `revert_out_of_scope()` in sandbox.py reverts agent changes outside `scope.editable` before constraints run.
- Notes persist across rejections: `save_notes()` copies from worktree to main `.autohelix/notes/` in a `finally` block. `notes/` is the sole agent-owned region and the only thing that round-trips; `prepare_worktree()` seeds worktrees with `notes/` plus read-only reference (observations, hints, latest review as `review.md`).
- Config mismatch: if you switch configs, `run` prompts to archive old state automatically.
- Parallel runs: `autohelix parallel` (see `src/autohelix/parallel/`) runs multiple workers on the same goal, each in its own branch namespace; workers share notes via `.autohelix/peer_notes/`.
- Time limits: `agent.timeout_seconds` is a hard kill timeout (no warning to the agent — can cut it off mid-step). `budget.iteration_time` (claude only) is a strict improvement over `timeout_seconds`: it enforces the same hard deadline (`min(timeout_seconds, iteration_time)`) but *also* installs Claude Code hooks (`PostToolUse`/`UserPromptSubmit`) that inject remaining time into the agent's context via `$AUTOHELIX_TIME_LEFT_SCRIPT` (which points to `<project>/.autohelix/runtime/bin/time_left.sh`, copied from `src/autohelix/bin/` at run start), so the agent can wrap up and write notes before being killed. `budget.time` caps total run wall-clock, checked before each iteration starts (doesn't interrupt one in progress). See `harness.py:_install_time_left()`.

## Gotchas

- **Editable install:** the package is installed with `pip install -e .`, so edits under `src/autohelix/` are live immediately — no reinstall needed. Only re-run `pip install -e .` if `pyproject.toml` dependencies or console-script entry points change.
- **Agent edits the main repo instead of the worktree:** iterations run in a git worktree, but if an editable install (`.pth`) or a hardcoded absolute path resolves imports/writes back to the *main* repo, the iteration is rejected with "Main repo has uncommitted changes." Keep per-iteration writable paths inside the worktree, and put long-lived outputs (models, large artifacts) at a fixed absolute path *outside* the repo entirely.
- **Resetting a run is not just `autohelix clear`:** `clear` only archives `.autohelix/` run state — it does NOT touch code or external outputs. A full reset = `clear` + `git reset --hard <initial-commit>` (roll back the agent's per-iteration commits) + delete any outputs written outside the repo (e.g. model checkpoints). Then the tree must be committed-clean or `run` refuses to start. See "Resetting a run" in README.md.
