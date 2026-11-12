# Changelog

## Unreleased

### Config v2

A new config format: `gates:` say what can reject an iteration, `observe:`
what the next agent reads and which numbers the run tracks. See
[docs/config.md](docs/config.md), including the table of old and new keys.

Breaking:

- **Old keys are errors that name their replacement**; there are no aliases
  and no migration:
  - `constraints:` → `gates:` (commands, unchanged);
  - `metrics:` / `observables:` → `observe:`, every entry with a `name:`;
    `values:` → `metrics:`; `capture_stdout:` is gone (stdout is always saved);
  - `acceptance.metric_gates` (`max_regression_pct`) → a `gates:` entry
    `- metric: NAME` with `regress: N%`, compared with the last accepted
    iteration instead of the best value;
  - `reviewer:` → an `observe:` entry with `name:` and `review: <prompt>`;
    the built-in reviewer, `reviews/` and `review.md` are removed;
  - `agent.type` → `agent.backend`; `agent.settings.model` → `agent.model`;
  - `agent.timeout_seconds` and `budget.iteration_time` → `agent.timeout`, a
    hard kill that also gives time-left hints where the backend supports them
    (a Codex without hook support warns and runs without them).
- Metric gate thresholds are percent strings (`regress: 5%`); a bare number
  is an error. Durations everywhere: a bare number (or a string of digits) is
  seconds, strings like `90s`, `30m`, `2h30m`.
- `.autohelix/prompt.md` is not read (a run warns while it exists); use
  `prompt_template:`, a path relative to the config file.
- Observe output is stored by entry name,
  `observations/iter-N/<name>/` (`stdout.txt`, `stderr.txt`, captures at
  their relative paths), so captures with one basename no longer collide.
- Prompt template variables are renamed (see below); custom templates need
  updating.

Added:

- **`gates:`**: commands, `ask:` entries (shorthand for `autohelix ask`, with
  `yes:`/`no:` (quoted or not) and an `agent:` of their own) and `metric:` entries with
  `regress: N%` or `improve: N%` against the last accepted iteration. Gate
  commands run first, in order; the first failure rejects.
- **`observe:`**: named command entries (`metrics:`, `capture:` files or
  folders, `timeout:`) and `review:` entries, run top to bottom after the
  gates; each metric gate is checked as soon as its number exists. A failing
  entry is recorded and shown to the next agent, contributes no metrics, and
  doesn't reject. Every observe command gets `$AUTOHELIX_PREVIOUS`, a copy of
  its own output from the last accepted iteration in which it succeeded.
- **`autohelix review`**: a read-only review of a change, as feedback text. In
  a run it reviews its own fresh copy of exactly the candidate, with the full
  change as `candidate.diff`; `--previous` adds its own last accepted review
  (progress tracking), `--metric NAME:higher|lower` numbers it must report
  (printed as `##autohelix[...]` lines by the command; the prose can't carry
  one). By hand it reviews the working tree against `--base` (default `HEAD`).
  Exits 2 when no valid review was produced or the reviewer changed files.
- `autohelix ask --command`; rendered `ask`/`review` commands put the
  question or prompt after `--`.
- Every harness command gets `$AUTOHELIX_CANDIDATE`, `$AUTOHELIX_BASE`,
  `$AUTOHELIX_PROJECT` and a private `$AUTOHELIX_SCRATCH`; stop commands also
  `$AUTOHELIX_OBSERVATIONS`, a copy of the accepted iteration's observations.
- The baseline runs every gate command and observe entry (rejecting nothing).
  A missing metric from a command entry stops the run; one from a review only
  if a gate uses it, otherwise it is a warning.
- **A failed or timed-out agent call no longer ends the run**: it is a
  rejected iteration (unchecked; `agent_error` in its record; `✗ agent failed:
  …` / `✗ agent timed out (30m)` in the console) and the loop goes on, with
  budgets as for any rejection. Three failed calls in a row stop the run ("the
  agent failed 3 times in a row: …"); an accepted or gate-rejected iteration
  resets the count.
- **Rejected work is kept**: each rejected attempt's tree as
  `refs/autohelix-rejected/<id>/<n>` (also failed or timed-out agent calls;
  removed by `delete`; none when all its changes were discarded), and the next
  agent gets the latest rejection's change as
  `.autohelix-iteration/rejected.patch` (over 2 MiB, the `git diff` command
  that shows it instead, `rejected_diff` in the prompt).
- **Discarded paths are reported**: changes outside the scope are recorded
  with the iteration (`discarded`, and `discarded_frozen` for the frozen
  ones), named in the next prompt (frozen ones said apart), and shown in the
  console, `view` and the viewer. `scope.editable` and `scope.frozen` can be combined (editable minus
  frozen). A run with gate or observe commands but no scope warns.
- **The prompt is a documented contract**: `goal`, `title`, `iteration`,
  `worktree`, `history`, `gates`, `observe`, `stop`, `editable`, `frozen`,
  `notes_path`, `hints`, `rejected_patch`, `rejected_diff`, `discarded`,
  `discarded_frozen`, `last` (the latest iteration's outcome, gate results and
  observations), `time_limit`, `group`, `group_runs`. A custom template using
  other names gets a warning at run start. The history lines include each iteration's one-line summary.
  The goal's first line is the run's title (validation warns when it is long).
- History records `gates` (in list order, not reached ones as `null`),
  `observe` (per entry: `ok`, `error`, `metrics`, and `metric_errors` with
  how to report a declared metric an entry ran without), `summary`, `base`,
  `tree`, `discarded`, `discarded_frozen` and `rejected_ref`; the old
  `constraints` field is gone. `view --json` adds `gates`, `observe`,
  `summary`, `discarded` and `discarded_frozen`. Gate failures at the baseline
  are shown (console, `view`, viewer).
- `##autohelix[name= 5 ]`: whitespace around the value is accepted (`wc -l`
  pads). A stop command whose last line is just `true`/`false`/`null` (`jq -e`)
  gives the command as the stop reason.
- `review` and `ask` don't offer `--agent opencode` (it has no read-only mode).
- Viewer: one section per observe entry (status, metrics, stdout, stderr,
  captured files; a review as Markdown), gates in list order with pending
  ones, the agent's summary per iteration, each iteration's own diff
  (rejected attempts included), and the goal's title.
- Console: the header lists the goal's title, agent, gates and observe
  entries by name; one row per iteration with the rejection reason (the
  failing gate's text) in the status column and the agent's summary
  below; the ending is plain lines (stop reason, totals, what landed, next
  commands), with an error printed first.

Hardening:

- Harness commands call `autohelix` through a launcher first on their `PATH`
  that runs the harness's own installation in isolated mode, so candidate code
  on the remapped `PYTHONPATH` can't replace modules inside `ask` or `review`.
- Gate, observe and stop commands run in their own process group, killed
  whole when the command ends or times out, so background children can't keep
  writing after the harness checked the files.
- Observe output is staged after the command ended and then replaces the
  entry's folder; `capture:` paths into `.git`, `.autohelix` or
  `.autohelix-iteration`, or leaving the copy, are config errors; symlinks are
  never followed and only regular files and folders are copied.
- Reviews drop symlinks pointing outside their copy, and check that
  `candidate.diff` and `previous.md` are unchanged after the call.
- The viewer serves files only from known run folders, opened one path
  component at a time without following symlinks, and passes git only object
  names that are plain hex.

### Config files and `autohelix setup`

Breaking:

- **`autohelix setup [NAME]` replaces `init`**. `autohelix init` is another
  name for `setup`; its old PATH argument is now `-p PATH`. It writes `autohelix.yaml`, or `autohelix/NAME.yaml`, from the
  commented template; it never overwrites a file, doesn't need git and
  creates no `.autohelix/`. A bare `setup` is refused when `autohelix/`
  already has configs.
- **`-c/--config` is removed** from `run`, `resume`, `view` and `report`,
  with no replacement option: configs live only at `autohelix.yaml` or
  `autohelix/<name>.yaml`. Move a config kept elsewhere into one of them.
- **No init step.** `run` creates `.autohelix/` and the local git excludes on
  first use; it needs a git checkout with a commit, and the first baseline no
  uncommitted changes to tracked files. Commit the config so it's versioned,
  and any scripts its commands use: iteration copies are made from the commit.
  "AutoHelix not initialized" is gone.

Added:

- **`setup` as the front door.** In a terminal, `setup` offers to start
  Claude Code, Codex or OpenCode with the bundled skill, or writes the
  template. `autohelix skill install` remains for ordinary agent sessions.
- **Several configs in an `autohelix/` folder.** `autohelix run perf` uses
  `autohelix/perf.yaml`; a bare `run` uses `autohelix.yaml`, else the only
  config in `autohelix/`, else lists them. The folder is versioned and can hold
  prompt templates and helper scripts in subfolders, committed with it
  (`prompt_template:` is relative to the config's folder).
- `name:` defaults to the config file's stem, so runs of
  `autohelix/perf.yaml` are named like `20261112-1400-perf-0mws`.
- A bare `resume` uses the config a bare `run` would and errors when that is
  ambiguous (name a run instead); `resume <id>` uses the config the run
  started with. To change a run's config, edit its file.

### Changed (breaking)

- **Runs.** Each `autohelix run` starts a new run, with its own id and state
  in `.autohelix/runs/<id>/` (ids sort by start: `20261111-1432-k3f9`, with
  `--name` or the config's name in the middle; commands accept the last part,
  `k3f9`). `autohelix resume [<id>]` continues one (`-n` more iterations,
  `--until` up to an iteration; exit 3 when there is none); `run` never
  refuses, but warns about a run of the same config that is live or has
  unlanded commits. State from earlier versions is refused: move or delete
  `.autohelix/` first. There is no migration.
- **Your branch moves only when a run lands.** Accepted iterations are commits
  on a hidden ref, `refs/autohelix/<id>` (`git log autohelix/<id>`). With
  `land: auto` (the default) your branch is fast-forwarded at the end of each
  `autohelix run`; with `land: manual` (or `run --land manual`) use
  `autohelix land`. You can keep working in your checkout while a run goes.
- **Every phase runs in a fresh private copy outside the project**, under
  `~/.cache/autohelix/copies/` (override with `AUTOHELIX_COPIES_ROOT`). Checks
  run on exactly the code that would be accepted; a check, review or
  `autohelix ask` call that changes it rejects the iteration, and the baseline
  runs the gates once to catch a check that edits files.
- `autohelix archive` hides runs from the dashboard and a bare `resume`,
  keeping everything; `autohelix delete` removes a run's state and ref
  (`--force` with unlanded commits). `clear` is gone.
- Run ids are positional and the project folder is `-p` for every command:
  `autohelix view k3f9 -p PATH` (was `view PATH --run ID`); likewise `report`
  and `dashboard -p`. `hint` takes `--run`.
- **`autohelix view` and `autohelix dashboard` are live in the terminal by
  default**; `--web` serves the web pages (`--port` and `--open` now need
  it). `view` shows the run's iteration table above the agent's live output,
  so `autohelix watch` is removed: use `autohelix view`. `--once` prints one
  snapshot as text (it replaces `--print`); `--json` is unchanged.
- `budget.time` is also checked before an iteration starts, so a resumed run
  past its time budget starts no new iteration.
- `dashboard --json`: `lifecycle` is replaced by `archived` (schema 1 was not
  in a release yet; it is on `main`).
- `.autohelix/` is excluded through `.git/info/exclude` instead of a
  committed `.gitignore` change.
- **`autohelix parallel` is removed**, replaced by groups (below). Its
  leftover worktrees and branches are left alone; remove them by hand when
  done: `git worktree remove ../<repo>-parallel/worker-N` and
  `git branch -D parallel/worker-N`. The `AUTOHELIX_PEER_RUNS` variable,
  `.autohelix/peer_notes/` and the `peers`/`peer_runs` prompt variables are
  gone (use `group` and `group_runs`).
- **The agent's copy keeps the harness's material in `.autohelix-iteration/`**
  (was `.autohelix/`): `notes/`, `history.jsonl`, `hints.md`,
  `commit_summary.txt`, `observations/`, `logs/`, and `prompt.md`, now this
  iteration's rendered prompt. Checks copies get only
  `.autohelix-iteration/candidate.diff`. A copy's git lists only this run's
  ref (and its group's), not every `refs/autohelix/*`.
- **The agent prompt is built in; `.autohelix/prompt.md` is no longer read**
  and `init` no longer writes it (every project had a copy of the old default,
  which would have sent notes to the old place). To keep a custom prompt, move
  it next to your config, set `prompt_template: <file>` (relative to the
  config file), and change its `.autohelix/` paths to `.autohelix-iteration/`;
  a run warns while `.autohelix/prompt.md` exists.
- The landing mode is recorded per run when it starts (`--land`; else
  `manual` with `--group`; else the config's `land:`; else `auto`) and kept on resume;
  `resume --land` changes it. `land:` in the config is the default for new
  runs only.
- `examples/nested-autohelix/` is removed (AutoHelix running AutoHelix is
  deferred; an agent driving runs through the CLI is the supported way).

### Added

- `autohelix dashboard`: a live list of the project's runs in the terminal;
  with `--web`, served with the run pages (`/run/<id>/`) by one local server.
- `--json` and `--once` on `autohelix dashboard` and `autohelix view`: a
  one-time snapshot for scripts (versioned with a `schema` field; fields are
  only ever added) or for the terminal.
- `autohelix land`.
- **Groups:** `autohelix run --group NAME` makes runs in one project share
  notes and code. Each iteration's copy gets the other non-archived runs of the
  group (`run.json`, `history.jsonl`, `notes/` under
  `.autohelix-iteration/runs/<id>/`) and their refs (`git diff autohelix/<id>`),
  and the prompt lists them with their latest accepted metrics. `resume
  --group NAME` sets or changes a run's group, `--group ''` clears it, and
  `archive` takes a run out. The dashboard shows the group after the id;
  `dashboard --json` adds `group` and `land`.
- Notes are saved as versions (`runs/<id>/notes.<n>/`, with `runs/<id>/notes`
  a link to the latest): a note the agent deletes stays deleted (before, it
  came back next iteration), and every version is kept.
- Harness git calls retry briefly when another git process holds a lock
  (concurrent runs in one project).
- From #16: the live run viewer (`autohelix view`), run summaries and
  `report.html`, stop commands and `autohelix ask`, and cost accounting
  across agents, judges and resumed runs.

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
