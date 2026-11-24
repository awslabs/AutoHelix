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
- **Every experiment is a folder in `autohelix/`, and its config is the
  folder's `config.yaml`: `autohelix/<name>/config.yaml`** (`autohelix run
  <name>`; a bare `autohelix run` runs the only experiment, or lists them
  when there are several). The top-level `autohelix.yaml` is no longer a
  config, nor is `autohelix/<name>.yaml`; development builds'
  `autohelix/<name>/autohelix.yaml` is now `autohelix/<name>/config.yaml`,
  the name the run's stored copy (`runs/<id>/config.yaml`) and
  `$AUTOHELIX_CONFIG` have. A run whose config
  isn't an experiment's is refused, so a development build's run of either
  old file can't be resumed: continue from it with `autohelix run <name>
  --import <id>`. `-c/--config` is gone; runs are named after the
  experiment folder (`name:` is an error).
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

- **A run keeps its experiment folder's files** (`runs/<id>/experiment/`):
  the scripts and material beside `autohelix/<name>/config.yaml`, links
  inside `autohelix/` followed (`metrics.py -> ../lib/metrics.py`; a
  committed project file is named by its path instead). `run` refuses a folder
  with a link out of `autohelix/` (the folder itself too) or into a dot-name
  inside it (`autohelix/.git/`, `.env`), a link to a folder holding it
  (`up -> ..`), a folder reached twice through links, a
  non-regular or unreadable file, or over 200 files, 200 folders or 10 MB.
  The stored files keep their read and exec bits, without write bits. A folder
  holding only its config gives commands, the agent and judges no copy.
  `resume` refuses
  when the folder's files changed since, naming them. Gate, observe and stop
  commands get a copy of it as `$AUTOHELIX_EXPERIMENT`
  (`python "$AUTOHELIX_EXPERIMENT/render.py"`); the agent gets one in
  `.autohelix-iteration/experiment/`, `ask`/`review` in their own copy
  (read-only files). All of `autohelix/` is always frozen, for every config:
  an agent's edit to a committed experiment or `autohelix/lib/` is discarded.
  `run --check` warns about a command that names an `autohelix/` path that
  exists but the commit lacks.
- **The end block's `next` line and the web run page name the run's folder**
  (`.autohelix/runs/<id>/`: `report.md`, `history.jsonl`, `iterations/`).
  Each segment's start record in `segments.jsonl` has `autohelix_version`.
- **Bare `autohelix`** in a terminal: the project's runs, then a Claude Code,
  Codex or OpenCode session with the bundled skill, which asks what next: a
  question about the runs, or a new experiment. The skill keeps a new
  experiment folder private (`/autohelix/` in the local exclude file,
  `git rev-parse --git-path info/exclude`) unless you
  want it shared, and keeps the coordinator's notes in `autohelix/README.md`
  (the session reads it first).
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
  says what `run` would say at its start (other runs' unlanded commits, the
  imported run's, an uncommitted config), and names the command it checked,
  flags included.
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
- **`docs/patterns.md`**: a starting config for each kind of goal (a checked
  task, optimization with a metric adapter, training a model, working through
  a spec, writing and research, comparing approaches); the skill starts from
  it. The training pattern's artifacts recipe (one model folder per run and
  iteration) is now a `paths.sh` in the experiment folder, sourced by the
  agent and the commands alike (`docs/ml-experiments.md`).

### Changed

- **A failed or timed-out agent call is a rejected iteration**, and the loop
  goes on; three in a row stop the run (also a rate limit or an outage, HTTP
  429 or 5xx). An infrastructure failure (a rejected key or credentials, HTTP
  401/403) stops it after the one call, with an error (exit 1) that says to fix
  it and `autohelix resume`.
- **Notes are saved as a version per iteration**: a note the agent deletes
  stays deleted.
- Gate, observe and stop commands run in their own process group, killed
  whole when the command ends or times out.
- Durations are accepted everywhere (`90s`, `30m`, `2h30m`; a bare number is
  seconds).
- `scope.editable` and `scope.frozen` can be combined: editable minus frozen
  (0.1.1 refused both).

### Fixed

- A gate, observe or stop command that leaves a background process holding
  its output (`./server & curl ...`) ends when its shell exits, with the
  shell's exit code; it waited for its whole timeout and was reported as timed
  out.
- A JSON line from the agent that isn't an object (a wrapper script's `0` on
  stderr) is shown as output; for Claude Code and OpenCode it ended the run
  with a Python error. Any error while an agent runs now kills its process
  group: the agent kept running, past its timeout.
- A failed command's output in the record and the prompt is its last 15
  lines, each cut at 500 characters: one long line (a JSON dump) made the
  prompt too large to start the agent, and every resume failed the same way.
- Files the agent writes outside the scope (a dataset, a checkpoint) are no
  longer hashed into your repository's objects at each freeze, and the record
  and the next prompt list at most 20 discarded paths, frozen ones first
  (`discarded_more` counts the rest). An unreadable file outside the scope is
  now discarded instead of rejecting the iteration.
- A setup that can't work stops the run at the baseline, before any agent call
  is paid for, with a setup error naming the gate and its last stderr line: a
  gate command that exits 126 or 127 (not executable, not found; unless its
  first word is a file the agent may write and git doesn't ignore), and an `ask:` gate that exits 2
  (no answer). Each iteration used to be paid for and rejected the same way.
- Every `resume` checks that the agents' executables can be found, as the
  first `run` does; a missing one cost an iteration.
- A custom prompt template that names a variable the prompt doesn't have, or
  the old `.autohelix/` copy paths, or fails to render against a sample
  history, is a config error, so `run --check` finds it. A misspelt variable
  rendered empty (the agent got no goal), and a render error failed iteration
  1 after the baseline, in a run that couldn't take the fix. A run whose
  stored template has one of these problems no longer resumes: continue from
  it with `autohelix run <name> --import <id>` after fixing the template.
- A damaged line in an old run's `history.jsonl` no longer stops new runs of
  its config; that run's own commands name the line and what works (fix or
  remove it, or `autohelix archive`), where `report` and `resume --until`
  ended in a traceback. A blank line resumes, as it views.
- `delete` without `--force` refuses a run whose tip is on no branch, tag or
  remote-tracking branch: a run that read landed after its target was reset
  was deleted with the only ref to its commits. `land` says when it did
  nothing (already there, nothing accepted, landed before) instead of
  "Landed".
- Bare `land` and `report` take the most recently active run that isn't
  running: they took the running run, which refuses them.
- The refusal of a changed config gives the landing first, then the `run
  --import` command, a line each: a new run started first lacked the code.
- A config name given where a run id goes (`land perf`) names the config's
  latest run; an unknown id lists the latest real ids instead of "e.g. k3f9".
- After a failed first baseline, the end block, `view --terminal` and the
  entry screen suggest `autohelix run <name>` (and `autohelix delete <id>`),
  as the error does, not `resume`, which refuses the fixed setup. `view
  --json` runs have `baseline`.
- An interrupted baseline is recorded as "baseline interrupted", not
  "baseline failed: " with nothing after it.
- One set of numbers for a run on every screen: `run`'s baseline row has its
  judges' cost, as `view` shows it; the run page's time counts the baseline, as
  the run list's does (`view --json` gives the baseline's `seconds`); `view`'s
  Budget names the run totals it shows (`to iteration 8 · $5.04 run total`)
  and the time used of `--time` as the web pages do (`12m 05s of 1h (this
  segment)`; the web pages write `12m5s`); an incomplete cost is `$0.07+` everywhere (was `$0.07
  (incomplete)` on `run` and the run page); a resumed segment's `landed` says
  `(this segment)`.
- The run page words the phase as the console does (`gates`, `observe ·
  bench`), not "Running gates…" or "accept…".
- A stopped run's `view --terminal` waits for `autohelix resume <id>`, not
  "the next run" (a new run never shows there).
- The run page and report.html no longer show a stored config or template
  that is a symlink (read as the rest of run state is).
- `resume`'s refusal of a changed config names the field and the entry
  (`agent.timeout`, `gates entry 2`, `observe bench (added)`), not only
  `agent` or `observe`; for files added to the experiment folder it says to
  remove them, not to restore them.
- The config template's `model: opus` is commented out: a user who changed
  only `cli:` kept a Claude model name for Codex or OpenCode.
- `run --help` forks from a run's code on a branch (`git switch -c <branch>
  autohelix/<id>`), not a detached checkout, which can't land.
- A timed-out agent call's error reads `timed out after 30m`, as a timed-out
  command's does (was `timed out after 30 minutes`).
- A second `autohelix view` on a port in use names the page to open in the
  one already serving, or `--port 0`, instead of "Address already in use".
- When no cost is known for a run and all its calls give the same reason, the
  end says it (`cost unavailable: set agent.model` for Codex, whose output names
  no model).
- An OpenCode `error` event (an invalid key) fails the call, the main agent's
  or a judge's: OpenCode exits 0 after one, which read as a successful call
  that changed nothing.
- A Claude Code call whose session ended several turns (it went on after its
  answer, when a background task ended) counts every turn's tokens and time;
  only the last turn's were recorded.
- A flag in `agent.extra_args` that takes several values (Claude Code's
  `--disallowedTools X`) no longer takes the prompt: every agent command ends
  with `--` and the prompt.
- The main Claude Code agent gets `--disallowedTools ScheduleWakeup`: a
  pending wakeup kept `claude -p` running for up to 15 minutes after its
  answer. A `--disallowedTools` of your own in `extra_args` still applies.
- A Claude Code `--settings` in `agent.extra_args` no longer drops AutoHelix's
  time-left hooks (Claude Code reads only the last `--settings`): AutoHelix
  merges your settings and its hooks into one file. A value that can't be read
  is a config error.
- A rejection because a gated metric's observe entry failed names the entry
  and how it failed (`metric gate: score was not measured (observe bench
  exited 2)`), and the next agent gets the tail of its output, as for a failed
  gate command.
- A commit subject cut at 200 characters has the agent's whole first line in
  the commit body.
- The prompt shows at most the last 8,000 characters of the hints, with a line
  saying earlier ones are in `.autohelix-iteration/hints.md`.
- Commands and agents get Ctrl-C (SIGINT) and SIGQUIT at their defaults in a
  run a script started in the background (`nohup autohelix run ... &`).
- `agent.timeout` kills an agent whose child closed its output but kept
  running.
- Agent output in the live panels is sanitised (terminal escape sequences).
- "Not a git repository" is recognised in any locale.
- A nested git repository in the project is refused with "nested git
  repository at X: remove its .git or clone outside the project".
- Rich markup in a goal (`[/bold]`) no longer crashes `--verbose`.
- The examples run as written: Docker's steps name the AutoHelix checkout and
  `run.sh` refuses a missing `--env` script; autoresearch's polite stop names
  the run; AlgoTune's collector reads `base_commit`; kernelbench and
  autoresearch commit without a git identity and autoresearch's setup reruns;
  kernelbench's model is `opus` (its published Bedrock id is a comment), and
  autoresearch's Bedrock id says the example runs on Bedrock.

### Upgrading from 0.1.1

1. **Rewrite the config** with the new keys (the table below); move `budget:`
   to the command line: `budget.iterations` (and an old `-n`) is `--until`,
   `budget.cost` / `budget.time` are `--cost` / `--time`, now per command.
   `autohelix run --check` reports every old key with its replacement.
2. **Move `autohelix.yaml` to `autohelix/<name>/config.yaml`**, a folder
   named for the experiment (`mkdir -p autohelix/perf && mv autohelix.yaml
   autohelix/perf/config.yaml`), and run it with `autohelix run <name>` (or a
   bare `autohelix run` while it is the only one); there is no `-c`.
   - A `prompt_template:` is relative to the config's folder: move the
     template with it.
   - If `autohelix.yaml` was committed, commit its removal: the first baseline
     needs no uncommitted changes to tracked files. Commit the new folder to
     share it, or keep `autohelix/` private
     ([Where configs live](docs/concepts.md#where-configs-live)).
   - With a development build's experiment folders, rename each
     `autohelix/<name>/autohelix.yaml` to `config.yaml`.
   - A `/autohelix.yaml` line in `.git/info/exclude` (a private top-level
     config) can go: `/autohelix/` covers the folder.
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
| `-c/--config FILE` on `run`, `report` | the config at `autohelix/<name>/config.yaml` and `autohelix run <name>` (a bare `run` with one experiment); `resume <id>` uses the run's own | `No such option '-c'.` |
| the `init [PATH]` command | bare `autohelix` in a terminal (an agent drafts the config; by hand: the examples in docs/config.md and the template it names) | `No such command 'init'.` |

#### Prompt template variables

For a custom `prompt_template:`. Each old name is a config error: `worktree`
names its new name, the others are listed as variables the prompt doesn't have.

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
