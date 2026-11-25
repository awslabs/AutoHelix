# Changelog

## Unreleased

A redesign. 0.1.1 configs and run state don't carry over: rewrite the config
(every old key is an error naming its replacement; see
[Upgrading from 0.1.1](#upgrading-from-011)) and start a new run.

### Added

- **Runs.** Every `autohelix run` starts a new run, with its own state in
  `.autohelix/runs/<id>/` (per iteration: its record, prompt, agent log and
  every command's output). `resume` continues one, `archive` hides it,
  `delete` removes it.
- **Landing.** Accepted iterations are commits on `refs/autohelix/<id>`. Your
  branch moves only when the run lands: at the end of each `run` or `resume`
  by default, or by `autohelix land` (`--land manual`).
- **Private copies.** Every phase (the agent, the checks, each judge) runs in
  its own copy outside the project (`~/.cache/autohelix/copies/`), made from
  the commit. Rejected work is kept as `refs/autohelix-rejected/<id>/<n>`, and
  the next agent gets it as `rejected.patch`.
- **Experiment folders.** Each experiment is `autohelix/<name>/config.yaml`
  (`autohelix run <name>`), with its scripts beside it. A run stores the
  config and the folder; `resume` refuses when either changed, and
  `run --import <id>` continues from the run with the edit.
- **Bare `autohelix`** in a terminal: the project's runs, then a Claude Code,
  Codex or OpenCode session with the bundled skill, to set up an experiment
  or ask about the runs.
- **`autohelix view`**: the runs on a live local web page; `--terminal`
  follows a run; `--once` and `--json` print a snapshot.
- **`autohelix stop`** ends a run after its current iteration (`--now`: at
  once); **`autohelix wait`** blocks until a run event and prints it as JSON.
- **Judges**: `autohelix ask` (yes/no; `ask:` gates) and `autohelix review`
  (feedback; `review:` observe entries), read-only agents on their own copy.
- **`stop:` commands** end the run once one exits 0 after an accepted
  iteration.
- **Budgets on the command line**: `-n`, `--until`, `--cost` and `--time`,
  per `run` or `resume` (1 iteration without any). Cost tracking covers the
  agent, the judges, the summary, and model calls a command records in
  `$AUTOHELIX_USAGE_FILE`.
- **`run --check`** makes every check before anything is written or spent.
- **Groups** (`run --group NAME`): runs that share notes and code.
  **`run --import ID`**: a new run that reads another run's notes, history
  and code.
- **A summary at the end of each segment**: `report.md` and `report.html`
  (`--no-report` skips it).
- The skill and the user docs ship in the package (`autohelix docs` prints
  the docs' folder); `docs/patterns.md` has a starting config for each kind
  of goal.

### Upgrading from 0.1.1

1. **Rewrite the config** with the new keys (the table below);
   `autohelix run --check` names every old key and its replacement. Metrics
   come only from `##autohelix[name=value]` lines on stdout.
2. **Move it to `autohelix/<name>/config.yaml`** and run it with
   `autohelix run <name>`. If `autohelix.yaml` was committed, commit its
   removal.
3. **Budgets are flags, per command**: `budget.iterations` is `--until`, and
   `budget.cost` and `budget.time` are `--cost` and `--time`. `-n N` now runs
   N more iterations.
4. **Start fresh**: 0.1.1's state can't be resumed. Move or delete
   `.autohelix/` and run `git worktree prune`; after `parallel`, also remove
   its worktrees and `parallel/worker-N` branches.
5. **Removed commands**: `init` (bare `autohelix`), `clear` (`archive`,
   `delete`), `parallel` (`run --group`), `watch` (`view --terminal`).
6. **A custom prompt**: `.autohelix/prompt.md` is no longer read. Move it
   beside the config and set `prompt_template:`. Change its `.autohelix/`
   paths to `.autohelix-iteration/`, and its variables to the current ones
   ([Concepts → Prompt](docs/concepts.md#prompt); `run --check` names any it
   doesn't know).

| 0.1.1 | Now |
|---|---|
| `constraints:` | `gates:` |
| `metrics:`, `observables:` | `observe:`, each entry with a `name:` (its `values:` is `metrics:`) |
| `capture_stdout:` | removed: stdout is always saved |
| `acceptance.metric_gates` | a `- metric: NAME` gate with `regress: N%` or `improve: N%`, compared with the last accepted iteration (was the best), no default |
| `reviewer:` | an `observe:` entry with `name:` and `review:` (`model`, `timeout_seconds`: the entry's `agent:` and `timeout:`); OpenCode can't be a judge |
| `agent.type` | `agent.cli` (`claude` is `claude-code`) |
| `agent.reasoning_effort` | `agent.effort` |
| `agent.command`, `$AUTOHELIX_CLAUDE_CMD` | `$AUTOHELIX_CLAUDE_CODE_CMD`, `$AUTOHELIX_CODEX_CMD`, `$AUTOHELIX_OPENCODE_CMD` |
| `agent.settings` | `agent.extra_args` |
| `agent.require_notes` | ask for notes in the goal or the prompt template |
| `agent.timeout_seconds`, `budget.iteration_time` | `agent.timeout`, a duration (`30m`) |
| `budget:` | the `run`/`resume` flags (step 3) |

## 0.1.1 — 2026-09-02

### Added

- KernelBench GPU optimization scaffolding for all 270 upstream tasks, including
  a CUDA evaluation harness and container configuration.
- A five-iteration KernelBench aggregate: 3.12× median best-so-far speedup with
  Claude Opus 4.8 on an H200.
- The per-task aggregate and plot source behind the 154-task AlgoTune result.
- Advanced loop examples for research, implementation task queues, AI workflow
  optimization, and nested AutoHelix runs.
- CI coverage for Python 3.11 and 3.12, plus tests and wheel verification in the
  gated PyPI publishing workflow.

### Fixed

- Preserve and scope-check changes when an agent creates commits inside its
  iteration worktree.
- Fail closed when an agent reports an infrastructure error, without merging
  changes or consuming the iteration number.
- Handle Codex `turn.failed` events and valid non-object JSON output.
- Reject baselines that omit configured metrics.
- Kill constraint and metric subprocess groups when a run is interrupted.

### Changed

- Removed the older bin-packing and budget-demo examples in favor of examples
  organized around reusable loop patterns.
