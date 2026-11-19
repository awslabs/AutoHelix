# Changelog

## Unreleased

What changes for a 0.1.1 user. [Upgrading from 0.1.1](#upgrading-from-011)
has the checklist.

### Breaking

- **A new config format.** `constraints:` is now `gates:`, `metrics:` /
  `observables:` are `observe:` entries with a `name:`, `reviewer:` is a
  `review:` entry, `acceptance.metric_gates` is a `metric:` gate. Every old key
  is an error that names its replacement; there is no migration.
- **Budgets are command-line flags**: `budget:` is now the `run`/`resume`
  flags `--until`, `-n`, `--cost` and `--time`. `--until N` stops after
  iteration N of the run, as 0.1.1's `budget.iterations: N` and `-n N` did;
  `--cost` and `--time` count one command (a segment), where 0.1.1's
  `budget.cost` and `budget.time` counted the project's whole history. Without
  a flag a segment runs 1 iteration (0.1.1 ran until iteration
  `budget.iterations`, default 5).
- **`-n N` runs N more iterations**, counted from where the run is; 0.1.1's
  `-n N` ran until iteration N (now `--until N`).
- **Runs.** Every `autohelix run` starts a new run, with its own state in
  `.autohelix/runs/<id>/`; `autohelix resume` continues one. 0.1.1's state
  (`.autohelix/history.jsonl`) is refused, not migrated.
- **Your branch moves only when a run lands.** Accepted iterations are commits
  on `refs/autohelix/<id>`; by default your branch is fast-forwarded at the end
  of each `run`/`resume` (`--land manual`: only by `autohelix land`).
- **Every phase runs in a private copy outside the project**
  (`~/.cache/autohelix/copies/`, or `$AUTOHELIX_COPIES_ROOT`), not a git
  worktree. A gate or observe command that changes the candidate rejects it.
- **The agent's material is in `.autohelix-iteration/`** in its copy (was
  `.autohelix/`): notes, history, hints, `commit_summary.txt`.
- **`.autohelix/prompt.md` is no longer read**: set `prompt_template:` (a path
  relative to the config). Several template variables are renamed or gone
  ([the table below](#prompt-template-variables)).
- **Metrics come only from `##autohelix[name=value]` lines** on stdout, by
  the metric's name. 0.1.1 also took a `name: value` or `name=value` line, and,
  with one declared metric, a single `##autohelix[...]` line whatever its
  name; those now report nothing.
- **Configs live at `autohelix.yaml` or `autohelix/<name>.yaml`.** `-c/--config`
  is gone; runs are named after the config file (`name:` is an error).
- **A run keeps the config it started with** (`runs/<id>/config.yaml`):
  `resume` refuses when the file's settings changed; apply an edit with
  `run --import <id>`.
- **Agent settings**: `agent.type` is now `agent.cli` (`claude` is
  `claude-code`); `agent.command` is replaced by `AUTOHELIX_CLAUDE_CODE_CMD`,
  `AUTOHELIX_CODEX_CMD` and `AUTOHELIX_OPENCODE_CMD`; `agent.settings` and
  `agent.require_notes` are gone.
- **Time limits are `agent.timeout`**, a duration (`30m`), replacing
  `agent.timeout_seconds` and `budget.iteration_time`.
- **Metric gates** (`- metric: NAME` in `gates:`) compare with the last
  accepted iteration, not the best one (0.1.1); the threshold is written with
  `%` (`regress: 5%` or `improve: 0%`), a bare number is an error, and there is
  no default (0.1.1's `max_regression_pct` defaulted to 10).
- **Removed commands**: `init` (use bare `autohelix`), `clear` (`archive`,
  `delete`), `parallel` (`run --group`), `watch` (`view --terminal`).
- **Exit codes**: `run` and `resume` exit 0 when a budget, a stop condition or
  `autohelix stop` ended them, 1 on an error, 130 when interrupted.
- `.autohelix/` is kept out of git through `.git/info/exclude`, not
  `.gitignore`.

### Added

- **Bare `autohelix`** in a terminal: the project's runs, then a Claude Code,
  Codex or OpenCode session with the bundled skill, which asks what next: a
  question about the runs, or a new experiment.
- **`autohelix view`**: the runs on a live web page (`127.0.0.1:8765`; a page
  per run); `--terminal` follows a run; `--once` and `--json` print the runs
  or one run once (JSON fields are only ever added).
- **`autohelix stop`** ends a run after its current iteration, from another
  terminal or a script; `--now` interrupts it.
- **`autohelix wait`** blocks until a run event (a segment started, an
  iteration recorded, a segment ended or crashed) and prints it as JSON, with
  a cursor to continue from; `--follow` streams them.
- **`autohelix resume`, `land`, `archive` and `delete`** to continue, land,
  hide and remove runs.
- **`autohelix ask`** (a yes/no judge; `ask:` gates) and **`autohelix
  review`** (feedback and `--metric` numbers; `review:` observe entries):
  read-only agents, each on its own copy of the candidate.
- **`stop:` commands**: the run ends once one exits 0 after an accepted
  iteration.
- **Groups** (`run --group NAME`): runs of one project share notes and code.
- **`run --import RUN_ID`**: a new run from your checkout that reads another
  run's notes, history and code.
- **`run --check`** makes every check a run makes before it writes anything,
  then exits.
- **`run --why`** and **`archive --why`**: one line kept with the run.
- **A summary at the end of each segment**: `runs/<id>/report.md` and
  `report.html`, a one-file copy of the run page (`--no-report` skips the
  summary).
- **Cost tracking** across the agent, judges, the summary and model calls a
  command records in `$AUTOHELIX_USAGE_FILE`; `--cost` budgets on it.
- **Gate, observe and stop commands get the run's context** in their environment
  (`$AUTOHELIX_CANDIDATE`, `$AUTOHELIX_BASE`, `$AUTOHELIX_SCRATCH`,
  `$AUTOHELIX_PREVIOUS`, `$AUTOHELIX_OBSERVATIONS` and more): see
  [Phases → Command environment](docs/phases.md#command-environment).
- **Rejected work is kept** as `refs/autohelix-rejected/<id>/<n>`, and the
  next agent gets the change as `rejected.patch`.
- **One folder per iteration**, `runs/<id>/iterations/NNN/`: its record,
  prompt, agent log, and each gate's, observe entry's and stop command's
  output.
- **The prompt has the hints' text** and names discarded out-of-scope changes.
- **Keep-awake**: `run` and `resume` keep the machine from sleeping while a
  segment runs (`caffeinate` on macOS, `systemd-inhibit` on Linux).
- **SIGTERM acts as Ctrl-C** in a running segment (`kill PID`,
  `stop --now`): the iteration is recorded and the run lands.
- **`autohelix skill install` / `export`**: the `autohelix` skill (set up an
  experiment, explain runs, supervise one when asked) for Claude Code, Codex or
  OpenCode; `--agent` takes `agent.cli`'s names (`claude-code`, `codex`,
  `opencode`).
- The user docs ship in the package (`autohelix/docs`); **`autohelix docs`**
  prints their folder.

### Changed

- **A failed or timed-out agent call is a rejected iteration**, and the loop
  goes on; three in a row stop the run.
- **Notes are saved as a version per iteration**: a note the agent deletes
  stays deleted.
- Gate, observe and stop commands run in their own process group, killed
  whole when the command ends or times out.
- Durations are accepted everywhere (`90s`, `30m`, `2h30m`; a bare number is
  seconds).
- `scope.editable` and `scope.frozen` can be combined: editable minus frozen
  (0.1.1 refused both).

### Fixed

- Commands and agents get Ctrl-C (SIGINT) and SIGQUIT at their defaults in a
  run a script started in the background (`nohup autohelix run ... &`).
- `agent.timeout` kills an agent whose child closed its output but kept
  running.
- Agent output in the live panels is sanitised (terminal escape sequences).
- "Not a git repository" is recognised in any locale.
- A nested git repository in the project is refused with "nested git
  repository at X: remove its .git or clone outside the project".
- Rich markup in a goal (`[/bold]`) no longer crashes `--verbose`.

### Upgrading from 0.1.1

1. **Rewrite the config** with the new keys (the table below); move `budget:`
   to the command line: `budget.iterations` (and an old `-n`) is `--until`,
   `budget.cost` / `budget.time` are `--cost` / `--time`, now per command.
   `autohelix run --check` reports every old key with its replacement.
2. **Put the config at `autohelix.yaml`** or `autohelix/<name>.yaml`
   (`autohelix run <name>`); there is no `-c`.
3. **Set `AUTOHELIX_CLAUDE_CODE_CMD`** instead of `AUTOHELIX_CLAUDE_CMD`
   (a set `AUTOHELIX_CLAUDE_CMD` is an error), and use the variables instead
   of `agent.command`.
4. **No `init`**: run bare `autohelix` in a terminal (an agent
   drafts the config), or start from an example or the packaged template.
5. **No `watch`**: `autohelix view` is the web page (the old
   `.autohelix/output/dashboard.html` is no longer written); `view --terminal`
   follows a run.
6. **Old runs can't be resumed**: move or delete `.autohelix/`, run
   `git worktree prune`, and start a new run. If you used `parallel`, remove
   its worktrees and branches too, once you have merged what you want to keep:
   `git worktree remove ../<repo>-parallel/worker-N` and
   `git branch -D parallel/worker-N` for each worker.
7. **A custom prompt**: move it beside the config, set `prompt_template:`,
   change its `.autohelix/` paths to `.autohelix-iteration/`, and rename its
   variables ([the table below](#prompt-template-variables)).
8. **Print metrics as `##autohelix[name=value]`** lines.

Each old key is an error naming its replacement; there are no aliases.

| Old | New | Error message |
|---|---|---|
| `constraints:` | `gates:` (commands, unchanged) | `` `constraints:` is now `gates:` `` |
| `metrics:`, `observables:` | `observe:`, every entry with a `name:` | `` `metrics:` is now `observe:` (each entry needs a `name:`; `values:` is now `metrics:`) `` |
| `values:` in an entry | `metrics:` in an `observe:` entry | `` `values:` is now `metrics:` `` |
| `capture_stdout:` | removed: stdout is always saved | `` `capture_stdout:` is gone: stdout is always saved; remove it `` |
| `acceptance.metric_gates` (`max_regression_pct`) | `gates:` entry `metric:` with `regress: N%` (vs the last accepted iteration, not the best) or `improve: N%` | `` `acceptance:` is now a `gates:` entry: ... `` |
| `reviewer:` | an `observe:` entry with `name:` and `review:`; OpenCode can't be a judge, so with `agent.cli: opencode` the entry needs `agent: {cli: claude-code}` (or `codex`) | `` `reviewer:` is now an `observe:` entry with `name:` and `review: <prompt>` `` |
| `reviewer.model`, `reviewer.timeout_seconds` | `agent:` and `timeout:` on the review entry | |
| `agent.type` | `agent.cli` (`claude` is `claude-code`) | `` `agent.type` is now `agent.cli` (claude-code, codex or opencode) `` |
| `agent.reasoning_effort` | `agent.effort` | `` `agent.reasoning_effort` is now `agent.effort` `` |
| `agent.command` | `$AUTOHELIX_CLAUDE_CODE_CMD`, `$AUTOHELIX_CODEX_CMD`, `$AUTOHELIX_OPENCODE_CMD` | `` `agent.command` is gone: set AUTOHELIX_CLAUDE_CODE_CMD, ... `` |
| `$AUTOHELIX_CLAUDE_CMD` | `$AUTOHELIX_CLAUDE_CODE_CMD` | `AUTOHELIX_CLAUDE_CMD is now AUTOHELIX_CLAUDE_CODE_CMD: ...` |
| `agent.settings` | `agent.extra_args` | `` `agent.settings` is gone: pass the CLI's own options in `agent.extra_args` `` |
| `agent.require_notes` | ask for notes in the goal or the prompt template | `` `agent.require_notes` is gone: ... `` |
| `agent.timeout_seconds` | `agent.timeout` (a duration) | `` `agent.timeout_seconds` is now `agent.timeout` (a duration such as 30m) `` |
| `budget:` (`iterations`, `cost`, `time`) | the `run`/`resume` flags `--until` (a run total, as `iterations` was), `--cost` and `--time` (per segment, where they counted the whole history); see [CLI → Budgets](docs/cli.md#budgets) | `` `budget:` moved to the command line: `autohelix run --until N --cost USD --time DURATION` (also `resume`; ...) `` |
| `budget.iteration_time` | `agent.timeout` (always a hard kill, with time-left hints) | as `budget:` |
| `timeout:` in seconds | durations everywhere (a bare number, or a string of digits like `"90"`, is still seconds) | |
| `.autohelix/prompt.md` | `prompt_template:` (relative to the config) | a warning at run start |
| `-c/--config FILE` on `run`, `report` | the config at `autohelix/<name>.yaml` and `autohelix run <name>` (or `autohelix.yaml` and a bare `run`); `resume <id>` uses the run's own | `No such option '-c'.` |
| the `init [PATH]` command | bare `autohelix` in a terminal (an agent drafts the config; by hand: the examples in docs/config.md and the template it names) | `No such command 'init'.` |

#### Prompt template variables

For a custom `prompt_template:`. `worktree` is a config error naming its new
name; the others are no longer variables: they render
empty, and the run warns at start naming them.

| 0.1.1 | Now |
|---|---|
| `worktree` | `copy` (an error: `` `worktree` is now `copy` ``) |
| `history_summary` | `history` (one line per recent iteration) |
| `constraints` (commands) | `gates` (`{text, kind}`; `text` is the command, or what a metric gate compares) |
| `observables` (`{command, metric_labels}`) | `observe` (`{name, kind, metric_labels}`, plus `command` or `prompt`) |
| `iteration_time` | `time_limit` (from `agent.timeout`) |
| `has_hints` | `hints` (the file, when there are hints) or `hints_text` (their text) |
| `peers` | `group` and `group_runs` (`run --group`) |
| `has_reviewer` | none: reviews are `observe` entries with `kind` `review` |

`goal`, `iteration`, `editable` and `frozen` keep their names (`frozen` now
includes the config and template files). The full list is in
[Concepts → Prompt](docs/concepts.md#prompt).

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
