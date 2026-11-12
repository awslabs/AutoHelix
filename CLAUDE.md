# AutoHelix Development

AutoHelix is an agent-powered iterative optimization tool. It runs AI agents in a loop — each iteration is isolated in private copies of the code, validated, and only accepted when it passes. See `README.md` for user-facing docs. For setting up training/eval (ML benchmark) cells, see `docs/ml-experiments.md`.

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
- `src/autohelix/cli.py` - CLI commands (setup, run, resume, land, archive, delete, view, dashboard, hint, ask, review, report)
- `src/autohelix/config.py` - configuration dataclasses and parsing
- `src/autohelix/agents/` - agent backends (claudecode, codex, opencode, mock)
- `src/autohelix/iteration.py` - one iteration's copies: agent copy, freeze to a tree, evaluation copies, accept, notes/cleanup
- `src/autohelix/sandbox.py` - child-process environments (PYTHONPATH remap) and checkout readiness checks
- `src/autohelix/checks.py` - gate, observe and stop command execution; metric gate comparison
- `src/autohelix/judge.py` / `review.py` - `autohelix ask` (yes/no) and `autohelix review` (feedback), read-only agent calls
- `src/autohelix/launcher.py` - the `autohelix` launcher put first on harness commands' PATH
- `src/autohelix/history.py` - iteration history (JSONL)
- `src/autohelix/runs.py` - run identity/paths and selection, ownership locks, ref recovery, landing, archive/delete, copies sweep
- `src/autohelix/workspace.py` - candidate copies (`Repo`, `Copy`, `Fingerprint`), tree snapshots, write scope and judge guard
- `src/autohelix/display.py` - live Rich display
- `src/autohelix/run_status.py` - live run status (`.autohelix/runs/<id>/runtime/status.json`)
- `src/autohelix/live.py` - live terminal `view` (run table + the log the run status points at) and `dashboard`
- `src/autohelix/viewer.py` + `viewer_resources/` - `autohelix view --web` / `dashboard --web` app (`/` run list, `/run/<id>/` one run)
- `src/autohelix/run_table.py` - `view` / `dashboard` terminal text (the `--json` payloads, `ProjectRuns.runs()` / `.run()`)
- `src/autohelix/replay.py` - parse saved agent logs into events (live view, web view)
- `src/autohelix/prompt_template.py` - agent prompt rendering (built-in template, or the config's `prompt_template:` file; `.autohelix/prompt.md` is no longer read)
- `tests/` - test suite (pytest)
- `examples/` - example projects (sorting, ml-recipe, writing,
  task-queue, research, workflow-optimization, algotune, posttrain,
  kernelbench)
- `scripts/run_dev_test.sh` - quick real-agent test against a bundled example (extra flags go to `autohelix run`)
- `docs/` - user-facing documentation (plain markdown)

## CLI Commands

```
autohelix setup [NAME] # Write autohelix.yaml (or autohelix/NAME.yaml): the template, or an agent session with the setup skill
autohelix run [NAME]   # Start a new run of autohelix.yaml (or autohelix/NAME.yaml) (--group NAME: share notes/code with its group)
autohelix resume [ID]  # Continue a run with its stored config (-n N more, --until N total; --group / --land change the stored ones)
autohelix land         # Fast-forward the run tip onto its starting branch
autohelix archive      # Hide a run from the dashboard and a bare resume; keeps everything
autohelix delete       # Remove a run's state and ref (--force with unlanded commits)
autohelix view         # Follow a run live in the terminal; --once / --json snapshot; --web page (127.0.0.1)
autohelix dashboard    # The project's runs, live in the terminal; --once / --json; --web (same server as view)
autohelix hint         # Send a hint to the running agent (picked up next iteration)
autohelix ask          # Yes/no judgment by a read-only agent (exit 0/1/2); `ask:` gates use it
autohelix review       # Read-only review of a change (feedback + ##autohelix metrics); `review:` observe entries use it
autohelix report       # Agent-written analysis of the run
```

## Testing

```bash
pytest -n auto            # run all fast tests in parallel (~3 min; plain `pytest` is ~11 min)
pytest -m slow            # run slow tests (requires real agent)
bash scripts/run_dev_test.sh              # manual test with real agent (sorting, 1 iteration)
```

## Docs

User-facing docs are plain markdown in `docs/` (see `docs/README.md` for the index).

## Examples Warning

**Do not commit optimized example code.** Examples should always contain the slow baseline version so users can see the optimization potential. If you run an example and it gets optimized, do NOT commit those changes to the main repo.

## Architecture Notes

- Entry points: `autohelix setup [NAME]` writes `autohelix.yaml` / `autohelix/NAME.yaml` (the template, or execs Claude Code/Codex/OpenCode with a first prompt pointing at the bundled skill; `init` is a hidden alias). `autohelix run [NAME]` finds the config (`config.find_config`), creates `.autohelix/` on first use and starts the harness loop.
- Iteration isolation: `iteration.py` runs every phase in its own fresh `workspace.Copy` (agent, checks, each review, stop; baseline and report too): a private folder holding one tree, with its own `.git` borrowing the user's objects via alternates. The harness reads copies only from outside through `Repo` and a harness index kept in a private per-process folder; it never runs git inside a copy after a call used it. The agent copy is frozen to a tree T (pinned at `refs/autohelix-tmp/<id>`); checks run on a fresh copy of T with HEAD at A and T's files verified after every command; accept commits exactly T on A (`commit-tree`, no hooks), CAS-updates `refs/autohelix/<id>`, and records history in a deferred-SIGINT section. A no-op is accepted without a commit. A rejected attempt's T (also after a failed or timed-out agent call) is kept as `refs/autohelix-rejected/<id>/<n>` (`keep_rejected`; removed by `delete`), and the next agent copy gets the latest rejection's `base..T` (`workspace.diff_args`, independent of the user's git config) as `rejected.patch` (over 2 MiB: none, the prompt's `rejected_diff` names the `git diff`). An attempt whose T equals its base keeps no ref. Run segments land separately on the target branch.
- Evaluation (`harness._evaluate`; user docs in `docs/config.md`): gate commands in list order (first failure rejects; `ask:` entries render to `autohelix ask [flags] -- Q`), then `observe:` entries top to bottom (`review:` entries render to `autohelix review [flags] -- P`); after each entry, the metric gates on its metrics, compared with the last accepted commit-creating record (`checks.metric_reference`). A failed observe entry is recorded and contributes no metrics; a missing gated metric fails. The baseline runs everything and rejects nothing; a missing command metric is a setup error. Old config keys are errors naming the replacement (`config.RENAMED_TOP_LEVEL_KEYS`, `agents.RENAMED_AGENT_KEYS`).
- Copy location: copies live outside the project, at `<base>/<dirname>-<hash8>/<run-id>/iter-N-<role>`, where base is `$AUTOHELIX_COPIES_ROOT` or `${XDG_CACHE_HOME:-~/.cache}/autohelix/copies` (`runs.copies_base()`). `runs.claim_copies()` fixes the folder for a process under the run lock (created on first use by `ensure_copies`: private 0700, owned, no symlinks below the base; a shared base must be sticky), base recorded in `runs/<id>/copies` for observers (`RunPaths.copies`), `GIT_CEILING_DIRECTORIES` set there. Segment end, standalone report and delete remove it; the project's folder stays with a `project.json` record (path, host) so `runs.sweep_copies()`, at the start of every `run`/`resume`, can remove leftovers of killed processes (runs whose lock is free) and other projects' folders whose checkout is gone (same host, over a day old). The root `conftest.py` points the base at a temporary folder for every test, examples included.
- Copy layout: nothing in a copy looks like project state. Agent copies get the harness's material in `<copy>/.autohelix-iteration/` (`workspace.ITERATION_DIR`): `prompt.md` (the rendered prompt), `notes/`, `history.jsonl`, `hints.md`, `observations/` (`iter-K/<name>/` per observe entry), `logs/`, `runtime/state/`, `rejected.patch` (after a rejection), `commit_summary.txt` (written by the agent), and `runs/<id>/` for other runs of the group (`run.json`, `history.jsonl`, `notes/`). Checks copies get only `candidate.diff` (the complete change, new files included); a review's own copy (made by `autohelix review` in `$AUTOHELIX_SCRATCH`) `candidate.diff` (`--binary`) and, with `--previous`, `previous.md`; stop copies get none of it. Observe output is staged in the command's call folder after it ended, then replaces `runs/<id>/observations/iter-N/<name>/`; `$AUTOHELIX_PREVIOUS` and `$AUTOHELIX_OBSERVATIONS` are per-call copies. A copy's `.git` lists branches, tags, remotes and only this run's ref plus its group's (`Copy.create(refs=...)`); other runs' commits are reachable by hash, not discoverable.
- Scope enforcement: `Copy.snapshot()` resets out-of-scope paths to A's entries in the harness index only (nothing is written into the copy), with `--attr-source=A` when git supports it. `.autohelix/` and `.autohelix-iteration/` are excluded from `add -A` by pathspec (unless already ignored: git refuses an exclude naming an ignored folder), so their contents are never hashed into the user's object store. Submodule changes reject the iteration.
- Notes persist across rejections: freezing (or closing) the agent copy saves `notes/` as a new version, `.autohelix/runs/<id>/notes.<iteration>/`, then swaps the `runs/<id>/notes` symlink to it (`iteration.save_notes()`; deletions honoured, all versions kept; a pre-version real `notes/` folder becomes `notes.old`). `notes/` is the sole agent-owned region and the only thing that round-trips; `iteration.prepare_state()` seeds agent copies with `notes/` plus read-only reference.
- Run identity: `RunPaths` is the authority for state layout. Ids are `YYYYMMDD-HHMM-[name-]xxxx`; the suffix is unique across the repo's run refs and every command accepts it (`runs.resolve_run`). Runs have no lifecycle state, only an `archived` flag (old `lifecycle: abandoned` reads as archived). `run` always creates (`select_run(new=True)`, warnings only); `resume` takes an id or the config's most recently active non-archived run, preferring runs with a baseline. `name` defaults to the config file's stem (`select_run`); the config path in `run.json` is what `resume <id>`, `view`, `dashboard` and `report` read (`read_stored_config_path`; there is no `-c`). All writing operations hold the per-run process lock. Status PID is advisory. Legacy top-level histories are refused for writes; no migration is attempted.
- Read-only evaluation: gate, observe and stop commands, reviews and `ask` must not change candidate files. A gate or observe command's change rejects (checks copy verified after every command; the command's gate item or observe record is marked failed). Stop commands run after acceptance: the acceptance stays, the change is an error (`AutoHelixRunError`). `ask` and `review` fingerprint their directory around each call (`DirectoryGuard`) and mark the usage record `integrity_violation` (sticky) on a change, which rejects the iteration even though observe failures otherwise don't. `review` has three layers: the backend's read-only mode, the guard (plus `candidate.diff`/`previous.md` digests), and its own fresh copy of `$AUTOHELIX_CANDIDATE` (never the checks copy, where commands leave files and run candidate code). Ordinary review failures are recorded and continue; scripts own `ask` exit codes.
- Launcher: harness commands run with PYTHONPATH remapped to the copy, so `iteration.call_folder()` writes `bin/autohelix` (`launcher.py`: the harness's interpreter with `-I`, its own package first on sys.path) and puts it first on PATH; candidate code can't replace modules inside `ask`/`review`. Each command also gets `AUTOHELIX_CANDIDATE` (T as a tree), `AUTOHELIX_BASE` (A), `AUTOHELIX_PROJECT`, `AUTOHELIX_SCRATCH`, and runs in its own process group, killed whole when it ends.
- Recovery/accounting: under ownership, repair only a torn unterminated final history record and recover published commits from run/iteration trailers. Each attempted main-agent failure (an unsuccessful or timed-out call) records a rejected, unchecked iteration (`agent_error`), consumes its number, and the loop continues; `harness.MAX_CONSECUTIVE_AGENT_FAILURES` (3) in a row within a segment stop the run (stop reason kind `agent`). Failures before the call (copy, prompt) do not consume the number and stop the run with an error. Baseline retries remain iteration 0 and retain earlier call charges. Calls with unavailable usage remain unknown.
- Resume/landing: only initial baseline capture requires a clean checkout. After baseline, gates/observe/stop/report use run candidate copies. Auto landing runs before report, with a finally fallback preserving original errors. The pin is deleted at every segment end. Archive retains evidence and unlanded refs; delete removes state/ref while persistent lock files remain outside the deleted directory.
- Groups: `run --group NAME` (stored in `run.json`; `resume --group NAME|''` changes it) makes runs of the same project share notes and code. Members are the other non-archived runs with that group (`runs.group_members()`), taken once per iteration (`IterationWorkspace.members`): copied into the copy (not linked) and listed in the prompt (`prompt_template.describe_member()`); their refs go into the copy's `.git`. No group commands: budgets, resume, land, archive stay per run. Landing mode is per run, recorded in `run.json` at creation (`--land`; else `manual` with `--group`; else config `land:` (None when unset); else `auto`), kept on resume, changed by `resume --land`. The "also running for this config" warning is quiet for group runs. Harness git calls on the user's repo (`Repo.git`, `runs.git`) retry briefly on lock contention (`workspace.run_git`).
- Time limits: `agent.timeout` (a duration; replaces `agent.timeout_seconds` and `budget.iteration_time`, which are now config errors) is always a hard kill of the main agent, and also gives time-left hints where the backend supports them: Claude Code hooks (`PostToolUse`/`UserPromptSubmit`), the same hook for Codex CLIs that support it (`_check_codex_time_left`: an older one warns and runs without), an OpenCode plugin; all via `$AUTOHELIX_TIME_LEFT_SCRIPT` (`<project>/.autohelix/runtime/bin/time_left.sh`, copied from `src/autohelix/bin/` at run start). `ask` (600s) and `review` (the command's deadline) don't use it. `budget.time` caps total run wall-clock, checked before and after each iteration (doesn't interrupt one in progress). See `harness.py:_install_time_left()`.

## Gotchas

- **Editable install:** the package is installed with `pip install -e .`, so edits under `src/autohelix/` are live immediately — no reinstall needed. Only re-run `pip install -e .` if `pyproject.toml` dependencies or console-script entry points change.
- **Agent edits the main repo instead of its copy:** iterations run in private copies, but if an editable install (`.pth`) or a hardcoded absolute path resolves imports/writes back to the *main* repo, AutoHelix logs a best-effort warning about tracked main-checkout changes. This inspection cannot abort validation and does not revert user files. Keep per-iteration writable paths inside the copy, and put long-lived outputs (models, large artifacts) at a fixed absolute path *outside* the repo entirely.
- **Starting a fresh experiment:** every `autohelix run` creates new state and baseline; `resume` continues one. `archive`/`delete` leave code and external outputs intact. Roll code back with git and remove external outputs separately when that is your intended reset. First baseline capture must be committed-clean.
