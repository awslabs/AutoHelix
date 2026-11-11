# AutoHelix Development

AutoHelix is an agent-powered iterative optimization tool. It runs AI agents in a loop — each iteration is isolated in a git worktree, validated, and only accepted when it passes. See `README.md` for user-facing docs. For setting up training/eval (ML benchmark) cells, see `docs/ml-experiments.md`.

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
- `src/autohelix/cli.py` - CLI commands (init, run, land, abandon, clear, watch, report)
- `src/autohelix/config.py` - configuration dataclasses and parsing
- `src/autohelix/agents/` - agent backends (claudecode, codex, opencode, mock)
- `src/autohelix/iteration.py` - iteration worktree lifecycle: prepare, scope, accept, notes/cleanup
- `src/autohelix/sandbox.py` - git worktree management and scope enforcement
- `src/autohelix/checks.py` - constraint and metric execution
- `src/autohelix/history.py` - iteration history (JSONL)
- `src/autohelix/runs.py` - run identity/paths, ownership locks, ref recovery, landing/abandonment
- `src/autohelix/checkpoint.py` - pinned Git contexts, immutable content checkpoints, shared write scope and judge guard
- `src/autohelix/display.py` - live Rich display
- `src/autohelix/run_status.py` - live run status (`.autohelix/runs/<id>/runtime/status.json`)
- `src/autohelix/viewer.py` + `viewer_resources/` - `autohelix view` web viewer
- `src/autohelix/replay.py` - parse saved agent logs into events (watch, view)
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
autohelix land         # Fast-forward the run tip onto its starting branch
autohelix abandon      # Retain run evidence; refuse further resume
autohelix clear        # Alias for abandoning the current run
autohelix watch        # Follow a run in the terminal (second terminal)
autohelix view         # Live web view of a run (127.0.0.1)
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
- Iteration isolation: `iteration.py` coordinates each worktree's preparation, scope enforcement, acceptance, and cleanup using `Sandbox`'s git/file operations. Worktrees are detached at the run tip. All harness worktree Git operations use creation-time absolute git-dir/work-tree pins, including after marker deletion; replaced roots/admin directories fail closed. Acceptance uses a private index and commit-tree with an explicit parent, bypassing mutable HEAD/index and commit hooks; it CAS-updates `refs/autohelix/<id>`, and durably records history under a deferred-SIGINT transaction. A no-op is accepted without a commit. Run segments land separately on the target branch.
- Scope enforcement: `revert_out_of_scope()` in sandbox.py reverts agent changes outside `scope.editable` before constraints run.
- Notes persist across rejections: `save_notes()` copies from worktree to `.autohelix/runs/<id>/notes/` in a `finally` block. `notes/` is the sole agent-owned region and the only thing that round-trips; `prepare_worktree()` seeds worktrees with `notes/` plus read-only reference (observations, hints, latest review as `review.md`).
- Run identity: `RunPaths` is the authority for state layout. Selection resumes the most recent open run for a config path; `--new` creates a separate run and `--run` selects exactly. All writing lifecycle operations hold the per-run process lock. Status PID is advisory. Legacy top-level histories are refused for writes; no migration is attempted.
- Agent normalization: after every main/reviewer call, detach HEAD, restore the owned run ref (without dereferencing symbolic refs), and mixed-reset to the expected tip while retaining working-tree content. Scope/checks validate that content. Judges (reviewer and ask) may write runtime feedback only; candidate edits or unverifiable/unfinished judge calls invalidate preaccept validation. Ordinary reviewer failures remain advisory.
- Recovery/accounting: under ownership, repair only a torn unterminated final history record and recover published commits from run/iteration trailers. Each attempted main-agent failure records a rejected iteration and consumes its number; failures before the call do not. Baseline retries remain iteration 0 and retain earlier call charges. Calls with unavailable usage remain unknown.
- Resume/landing: only initial baseline capture requires a clean checkout. After baseline, metrics/stop/report use run candidate checkouts. Auto landing runs before report, with a finally fallback preserving original errors. Abandon retains evidence and unlanded refs; purge removes state/ref while persistent lock files remain outside the purged directory.
- Parallel runs: `autohelix parallel` (see `src/autohelix/parallel/`) runs multiple workers on the same goal, each on a real worker branch with its own stable run id; workers share notes via `.autohelix/peer_notes/`.
- Time limits: `agent.timeout_seconds` is a hard kill timeout (no warning to the agent — can cut it off mid-step). `budget.iteration_time` (claude only) is a strict improvement over `timeout_seconds`: it enforces the same hard deadline (`min(timeout_seconds, iteration_time)`) but *also* installs Claude Code hooks (`PostToolUse`/`UserPromptSubmit`) that inject remaining time into the agent's context via `$AUTOHELIX_TIME_LEFT_SCRIPT` (which points to `<project>/.autohelix/runtime/bin/time_left.sh`, copied from `src/autohelix/bin/` at run start), so the agent can wrap up and write notes before being killed. `budget.time` caps total run wall-clock, checked before each iteration starts (doesn't interrupt one in progress). See `harness.py:_install_time_left()`.

## Gotchas

- **Editable install:** the package is installed with `pip install -e .`, so edits under `src/autohelix/` are live immediately — no reinstall needed. Only re-run `pip install -e .` if `pyproject.toml` dependencies or console-script entry points change.
- **Agent edits the main repo instead of the worktree:** iterations run in a git worktree, but if an editable install (`.pth`) or a hardcoded absolute path resolves imports/writes back to the *main* repo, AutoHelix logs a best-effort warning about tracked main-checkout changes. This inspection cannot abort validation and does not revert user files. Keep per-iteration writable paths inside the worktree, and put long-lived outputs (models, large artifacts) at a fixed absolute path *outside* the repo entirely.
- **Starting a fresh experiment:** `autohelix run --new` creates new state and baseline. `abandon`/`clear` only end the run lifecycle; code and external outputs stay intact. Roll code back with git and remove external outputs separately when that is your intended reset. First baseline capture must be committed-clean.
