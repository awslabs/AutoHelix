# CLI Reference

Project commands default to the current directory if `--path` is omitted.

## `autohelix skill`

Install or export the packaged `autohelix-setup` skill and all its references.
It helps your coding agent design and verify performance, writing, and research
experiments using the installed CLI. A source checkout is not required.

```bash
autohelix skill install --agent claude --scope project --path /path/to/project
autohelix skill install --agent codex --scope user
autohelix skill export /path/to/autohelix-setup
```

`--agent` is required and accepts `claude`, `codex`, or `opencode`. `--scope`
defaults to `project`. Project installation uses the current directory or
`--path`; user installation uses your home directory and rejects `--path`.
Skill installation does not require a git repository.

| Agent | Project skill folder | User skill folder |
| --- | --- | --- |
| Claude Code | `.claude/skills/autohelix-setup/` | `~/.claude/skills/autohelix-setup/` |
| Codex | `.agents/skills/autohelix-setup/` | `~/.agents/skills/autohelix-setup/` |
| OpenCode | `.opencode/skills/autohelix-setup/` | `~/.config/opencode/skills/autohelix-setup/` |

Export writes the full skill directly into `DESTINATION`; it does not append
another `autohelix-setup` directory. Use it for custom discovery directories.

Both commands refuse any existing destination by default, including an empty
directory. With `--force`, bundled files are replaced and unrelated local files
are preserved; files removed from a future bundle are not deleted. Symlinks at
the destination or bundled paths and incompatible file/directory types are
rejected before copying. Updates replace individual files; an I/O failure can
leave a partially updated folder. Correct the error and repeat with `--force`.

After installation, ask your agent to use `autohelix-setup` to prepare your
experiment (or let [`autohelix setup`](#autohelix-setup) start the agent with
the bundled skill, without installing anything). It will inspect the project
and propose scope, gates, observe entries (commands and reviews) and
optional metric gates. Review the complete YAML and its
section-by-section explanation before project files change; agreement to save
and commit leads to writing, validating, and committing only the setup files.
It gives verification results and the complete launch command with a suggested
budget (`autohelix run perf -n 10 --cost 20`), and starts `autohelix run` only
when requested.
To request ongoing management, ask the agent to prepare, run, and supervise
within explicit effort limits. It should monitor saved outcomes through process
termination and use hints when useful for later iterations. See the
[setup-only and managed walkthroughs](getting-started.md#with-your-coding-agent).

Installing the Python package does not register skills automatically.

## `autohelix setup`

Write a loop's config: `autohelix.yaml`, or `autohelix/NAME.yaml` with a name.

```bash
autohelix setup [NAME] [-p PATH] [--agent claude|codex|opencode]
```

| Option | Description |
|--------|-------------|
| `NAME` | Write `autohelix/NAME.yaml` (lowercase letters, digits, hyphens, up to 32 characters, starting with a letter or digit; not `autohelix`); runs of it are named after it |
| `--path`, `-p` | Project directory (default: current dir) |
| `--agent` | Draft the config with this agent, skipping the selection menu |

In a terminal, `setup` first asks whether to set up the loop with an agent or
write an empty config. When several agents are installed (looked for in the
order `claude`, `codex`, `opencode`), it then asks which one. Choose with ↑/↓
and Enter, or an option's number; Enter alone takes the first, Esc or Ctrl-C
cancels. With an agent, `setup` is replaced by that agent's usual interactive
session, started in the project with a first prompt to follow the setup skill
bundled with AutoHelix: it asks what you want to achieve, then shows the complete
proposed config and walks through each section before changing project files:
what it controls, why it was chosen, and how to adjust it, including effective
defaults. Discuss revisions; when you agree to save and commit, it writes,
validates, and commits the setup.
`agent.backend` is set to that agent unless you decide otherwise. Nothing is
installed into the agent's settings, and the skill always matches the installed
AutoHelix. Setup gives the launch command; it starts a run only when requested.

An empty config is the commented template; then edit it, commit it, and run
`autohelix run [NAME]`. Without a terminal (a script), `setup` writes the
template unless `--agent` is given. Without an agent on `PATH` it writes the
template too.

`setup` never overwrites a file. With `--agent`, an existing config (such as a
template written earlier) can be revised after reviewing and agreeing on the
agent's proposal; without it, an existing config is an error. A bare `setup`
is refused when `autohelix/` already has configs: `autohelix.yaml` would take
over a bare `autohelix run`, so name the
new one. `setup` doesn't need git and creates no `.autohelix/`; it warns when
`run` would refuse the folder (no git repository, or no commit yet).

`autohelix init` is another name for `setup`; its old PATH argument is now `-p PATH`.

### Config files

A project's configs live in exactly two places: `autohelix.yaml` at its root,
or `autohelix/<name>.yaml` for several loops. The `autohelix/` folder is
versioned, and can also hold prompt templates and helper scripts
(`autohelix/prompts/perf.md`; `prompt_template:` is relative to the config's
folder). Commit them: iteration copies are made from the commit, so an
untracked script a gate runs is missing there. Keep `autohelix/` out of
`scope.editable` (or list it under `frozen:`), so the agent can't edit its own
gates. `.autohelix/` is the runs' state, kept out of git.

- The configs in `autohelix/` are the `.yaml` files directly in it; keep
  other YAML files in subfolders.
- `autohelix run perf` uses `autohelix/perf.yaml`.
- A bare `autohelix run` uses `autohelix.yaml`, else the only config in
  `autohelix/`, else stops with the list of configs.
- Runs are named after the config file, so runs of `autohelix/perf.yaml` are
  named like `20261112-1400-perf-0mws`; `autohelix.yaml` gives runs no name.
- A run has exactly one config: it stores the file's text when it starts
  (`.autohelix/runs/<id>/config.yaml`), and `resume`, `view`, `dashboard`,
  `report` and the judges use that copy. `resume` refuses when the file's
  parsed config has changed since (comment and formatting edits don't count):
  apply an edited config with a new run, `autohelix run perf --import <id>`,
  or restore the file to continue. A deleted file (or template) is fine: the
  run continues with its stored copy.
- The config file and its `prompt_template:` file are always frozen, whatever
  `scope:` says. Scripts the config calls (`bench.py`, gate scripts) are code:
  they move with the run's commits; list them under `scope.frozen` if they
  must not change.

There is no `--config` option, and nothing to initialize: the first
`autohelix run` creates `.autohelix/` and adds `.autohelix/` and
`__pycache__/` to the repository's local exclusions (`info/exclude`;
`.gitignore` is unchanged). `run` refuses if a conflicting `.gitignore` rule
makes `.autohelix/` visible to git despite the local exclusion. Add
`.autohelix/` after any conflicting negation rules in your `.gitignore`, then
retry. AutoHelix leaves that file unchanged.

## `autohelix run`

Start a new run of the optimization loop.

```bash
autohelix run [CONFIG] [-n N | --until N] [--cost USD] [--time DURATION] [-p PATH] [--group NAME] [--import RUN_ID] [--why TEXT] [--land auto|manual] [--verbose] [--no-report]
```

`CONFIG` names a config in `autohelix/` (`perf` for `autohelix/perf.yaml`);
without it, the run uses `autohelix.yaml`, else the only config in
`autohelix/` (see [Config files](#config-files)). The project must be a git
checkout with a commit, and the first baseline needs no uncommitted changes to
tracked files. Commit the config so it's versioned, and any scripts its
commands use: iteration copies are made from the commit. Do it before the first
`run`: with auto landing, a later commit on the branch makes `resume` refuse
(see [`resume`](#autohelix-resume)), leaving `run --import` or `--land manual`.

### Options

| Option | Description |
|--------|-------------|
| `-n`, `--iterations N` | Run N iterations (for `resume`, N more). Default 3, only when no budget flag is given |
| `--until N` | Run up to iteration N in total (safe to repeat) |
| `--cost USD` | Stop once this invocation has spent USD (known costs) |
| `--time DURATION` | Start no iteration after DURATION of this invocation's wall clock (`90m`, `2h`) |
| `--path`, `-p` | Project directory (default: current dir) |
| `--group NAME` | Make the run part of a [group](#groups): it sees the other runs of the group (their notes, history and code), and lands manually by default |
| `--land auto\|manual` | Land accepted code at the end of each segment, or retain it only on the run ref until `autohelix land`. Stored with the run. Default: `manual` with `--group`, else `land:` in the config, else `auto` |
| `--import RUN_ID` | [Continue from another run](#continuing-from-another-run): its notes and history, read-only, in every iteration, and its last observe outputs at the baseline |
| `--why TEXT` | Why this run is started: one line (at most 300 characters), stored in `run.json` and shown on the dashboard |
| `--verbose` | Show agent command details, file paths, heartbeats |
| `--no-report` | Don't write the run summary when the run finishes |

`run` always starts a **new** run, with its own notes and history; to
continue one, use [`autohelix resume`](#autohelix-resume). It never refuses
because of other runs, but says what it sees: a run of the same config that is
still running (both will land on the same branch, and whichever finishes second
may not fast-forward), a previous run with commits that haven't landed (this
run starts from your branch without them), or otherwise just which run came
before. Runs started with `--group` don't warn about each other: running side
by side is the point. Scripts that want to continue where they left off should call
`resume`, not `run`.

When the run finishes (not when interrupted), it writes a summary to `.autohelix/runs/<id>/report.md` — see [Configuration → Report](config.md#report).

On first start, performs preflight checks and evaluates the baseline (see
[Configuration → Order within an iteration](config.md#order-within-an-iteration)). When the run stops, it saves `.autohelix/runs/<id>/report.html`, a one-file copy of the [`autohelix view --web`](#autohelix-view) page — see [Concepts → Run page](concepts.md#run-page).

A refused dirty first start creates no run. The first valid baseline requires
a clean checkout at the run's starting commit. Resumes use the run's accepted
code, so unrelated staged/dirty user files can stay in the checkout. Accepted
commits advance `refs/autohelix/<id>` first, then auto landing fast-forwards the
starting branch before report generation. If dirty files would be overwritten,
the target diverged, or the target is checked out elsewhere, landing is refused
and the run ref retains the accepted code. Resolve that condition and land later.
One invocation of `run` or `resume` is a segment, with its own
[budget](#budgets). Only one process can own a run at a time; a PID in
the viewer is advisory, while a process-held file lock enforces ownership.

A failed or timed-out main-agent call records a rejected attempt (unchecked,
`agent failed: …` in the console row, `agent timed out (30m)` for a timeout),
consumes its number, and the loop goes on to the next iteration; budgets apply
as for any rejection. Three failed calls in a row stop the run ("the agent failed
3 times in a row: …"), with landing and the report as at any segment end.
Setup failures before a call (copy creation, the prompt) do not consume an
iteration and stop the run with an error. Failed baseline attempts remain iteration 0 and retain their costs.

Legacy `.autohelix/history.jsonl` state is not migrated: move or delete the old
`.autohelix/` directory and run `git worktree prune` before starting a new run.

### Budgets

The config says what one iteration does; the flags say how long this
invocation goes on. Every budget counts **this invocation**, whatever earlier
ones spent:

| Flag | Stops |
|---|---|
| `-n N` | after N iterations (`resume -n N`: N more) |
| `--until N` | after iteration N of the run: the one flag that counts a total, so a script can repeat it (`resume --until 20` twice runs nothing the second time) |
| `--cost USD` | once this invocation has charged USD: agent calls, reviews, asks, every model call a command reports, the baseline's too (the summary report excepted) |
| `--time DURATION` | once DURATION of this invocation's wall clock has passed, the baseline included |

Whichever comes first ends the invocation; a stop condition can end it sooner.
`-n` and `--until` don't go together. Without any budget flag an invocation
runs **3** iterations (more); with only `--cost` or `--time` it has no
iteration cap. Cost and time are checked before each iteration and after it,
never during one, so an invocation can overshoot by one iteration's worth: for
a hard limit on one iteration use [`agent.timeout`](config.md#agent). Costs that
are unavailable don't count (`--cost` warns and tracks only the known ones): keep
`-n` or `--time` alongside. With `--cost` the only bound, an unknown cost stops
the invocation before its next iteration ("cost unknown: --cost can't bound this
invocation (add -n or --time)").

```bash
autohelix run perf -n 10 --cost 20      # at most 10 iterations and $20
autohelix resume -n 10 --cost 5         # 10 more iterations, up to $5 more
autohelix resume --cost 5               # no iteration cap: until $5 more is spent
autohelix resume --until 30 --time 2h   # up to iteration 30, starting none after 2 hours
```

The header names the budget in effect ("3 more iterations (to 8) · $5.00
more · 2h"), and the run's status records it as run totals (see
[JSON output](#json-output)).

### Groups

Runs started with the same `--group NAME` share what they learn. At the start
of each iteration the agent's copy gets, for every other non-archived run of the
project in that group (running or finished), its `run.json`, `history.jsonl` and
`notes/` in `.autohelix-iteration/runs/<id>/`, and the copy's git has its ref,
so `git diff autohelix/<id>`, `git show autohelix/<id>:path` and
`git checkout autohelix/<id> -- path` work there. The prompt lists them, one
line each, with their latest accepted metrics. Code taken from another run is
checked like any other change; the prompt asks the agent to name the run it
came from in its summary.

That is all a group does: budgets, stop conditions, `resume`, `land`,
`archive` and `delete` stay per run. A typical flow: start K runs with
`--group perf` (one per terminal, or in the background), follow them on
[`autohelix dashboard`](#autohelix-dashboard) (`--metric` picks the column to
compare), `autohelix land <winner>`, and `autohelix archive` the rest. Group
runs land manually by default: with auto landing the first to finish would land
and every other would diverge from it for good. `resume --group NAME` moves a
run into a group (from its next iteration) and `resume --group ''` takes it
out; archiving a run takes it out of its group, and resuming it brings it back.

### Continuing from another run

`autohelix run [CONFIG] --import RUN_ID` (an id or its last part) starts a new
run that continues from another one of the project: **code from your checkout,
state from that run.** The config is chosen as for any `run` (a warning names
both files when it isn't the one the imported run used: a different config is
legitimate), the code is your checkout's HEAD (the usual clean-tree rule), and
the run has its own notes (empty, numbered from its iteration 1), budgets,
baseline and metric reference. What it adds:

- every iteration's copy shows the imported run's `run.json`, `history.jsonl`
  and `notes/` read-only in `.autohelix-iteration/runs/<id>/`, and its code's
  ref, `autohelix/<id>`; the prompt says so ("This run continues from k3f9:
  …"). They are copied at the start of each iteration, as for a
  [group](#groups); a deleted imported run is just left out;
- at the baseline, each observe entry gets as `$AUTOHELIX_PREVIOUS` that run's
  last successful output of the entry with the same name
  ([Previous output](config.md#previous-output)), so a tracker continues from it.

`run.json` records `"import": {"run": "<id>"}`. The imported run may be
archived or still running. It isn't a group membership: `--group` still
decides which runs share notes and code, and an imported run that is also in
the group is shown once, as imported. Typical uses:

```bash
# The run landed, you committed more on main: keep going with what it learned.
autohelix run perf --import k3f9
# Apply an edited config (resume refuses a changed one).
autohelix run perf --import k3f9
# Fork from an old run's code: check it out first (or an iteration's commit).
git checkout autohelix/20261111-1432-perf-k3f9 && autohelix run perf --import k3f9
```

Each continuation is a new run with a new baseline (its code differs), at the
cost of a chain of runs on the dashboard, where each shows what it continues
from.

### Run ids

A run's id is the local date and time it started, its name if it has one, and
four random characters: `20261111-1432-k3f9`, or `20261111-1432-perf-k3f9`. So
`.autohelix/runs/` lists runs in the order they started. Every command that
takes a run id also takes just the last part (`k3f9`), which is unique in the
repository; git commands need the full id (`git merge autohelix/20261111-1432-k3f9`).

## `autohelix resume`

Continue a run.

```bash
autohelix resume [RUN_ID] [-n MORE | --until N] [--cost USD] [--time DURATION] [-p PATH] [--group NAME] [--land auto|manual] [--verbose] [--no-report]
```

`resume` only continues the same run: its stored config, its own tip, more
budget. Use it after a budget ran out, an interruption (Ctrl-C, a killed
process, a reboot, three agent failures), a stop condition, or to change
`--land` or `--group`. Anything else is a new run: [`run --import
<id>`](#continuing-from-another-run).

| Option | Description |
|--------|-------------|
| `-n`, `--iterations` | How many **more** iterations to run (default 3, only when no budget flag is given) |
| `--until N` | Run up to iteration N (a total: running it twice does nothing the second time) |
| `--cost USD`, `--time DURATION` | Up to USD more, and DURATION of this invocation's wall clock (see [Budgets](#budgets)) |
| `--land auto\|manual` | Change the run's landing mode (stored; default: keep it) |
| `--group NAME` | Put the run in group NAME from its next iteration (stored); `--group ''` takes it out of its group |

Other options as for `run`. A run always uses the config it stored when it
started ([Config files](#config-files)). Before spending anything, `resume`
compares it with the config file: when the file's parsed config differs (a
value, a key, or the prompt template's text; comments and formatting don't
count), it refuses and names both ways out: `autohelix run <config> --import
<id>` applies the new config in a new run, or restore the file to continue
this one. Empty values (`gates: []`, a key left out) count the same; a default
written out (`report: true`) or another spelling (`10m` for `600`) is a change,
named in the message. A deleted file, or a deleted prompt template, isn't a
change: the run continues with its copy (other keys are still compared). The
stored copy is read-only. A
run started before runs stored their config can't be resumed: continue it
with `run --import <id>`. Without `RUN_ID`, the config is the one a bare
`autohelix run` would use (with several configs and no `autohelix.yaml`,
`resume` stops: name a run), and `resume` continues that config's most
recently active run that isn't [archived](#autohelix-archive), preferring runs
that captured their baseline; it says which ("Resuming k3f9: 12 iterations,
last active 2h ago"). If that run is running in another terminal, `resume`
says so rather than picking an older one. With no run to resume it exits
with status 3.

Budgets are this invocation's ([Budgets](#budgets)): `resume -n 10 --cost 5`
runs 10 more iterations, up to $5 more, however much the run spent before.
After an interrupted invocation (Ctrl-C, a killed process, a reboot), a
`resume` without budget flags says what that one had left: "3 more (default);
the interrupted invocation had 8 left: -n 8" (not when that invocation had already stopped for a
reason of its own). `--until` with a number already reached says so ("Already at iteration 5;
--until 3 runs nothing.") and exits 0 without starting a segment: nothing is written, landed or
reported. A run stopped by a
[stop condition](config.md#stop) is paused, not ended: `resume` continues it
and says so ("Run k3f9 stopped because score reached 0.9; it stops again after
the next accepted iteration if that still holds"): the condition is checked
again after the next accepted iteration.

When the target branch has moved past the run's tip (you or another run
committed on it after the run's tip), the run doesn't take those commits. With
auto landing, `resume` refuses before writing anything (after saying so if the
run is running elsewhere): "main has 2 commits past run k3f9's tip (committed
after its tip). A run continues its own code: start a new run
that continues from it, `autohelix run perf --import k3f9`, or continue without
landing: `autohelix resume k3f9 --land manual`." With manual landing it says
so and continues from the run's own tip; its accepted commits then diverge from
the branch, so land them with `git merge autohelix/<id>`. Before the baseline
the run hasn't started from its commit yet, so nothing is compared; after it,
a run that never accepted a change says so ("committed since run k3f9 started;
it hasn't accepted any changes").

## `autohelix land`

```bash
autohelix land [RUN_ID] [-p PATH] [--into BRANCH]
```

Fast-forward the run's starting branch to its accepted tip, preserving unrelated
checkout edits. Defaults to the current run. For a divergent target, use the
printed git merge command after reviewing the changes. A run created from a
detached checkout requires `--into BRANCH`. Landing also refuses branches that
are being rebased. If the target moved during a segment, review it and land
explicitly; AutoHelix never rewinds user commits.

## `autohelix archive`

```bash
autohelix archive RUN_ID... [-p PATH] [--undo] [--why TEXT]
```

Sets runs aside: they are hidden from the [dashboard](#autohelix-dashboard)
(behind a count) and skipped by a bare `resume`. Their history, notes, logs and
commits stay; `autohelix resume RUN_ID` resumes one and brings it back, and
`--undo` brings it back without running it. A running run can't be archived.
Use it for runs that are done or didn't work out, and for runs you merged some
other way (squash, cherry-pick): those keep looking unlanded to git.
`--why TEXT` records why (one line, at most 300 characters) in each run's
`run.json` as `archive_why`, shown on the web dashboard and in `--json`;
`--undo` keeps the note, and a later `archive --why` replaces it.

## `autohelix delete`

```bash
autohelix delete RUN_ID... [-p PATH] [--force]
```

Deletes runs: their `.autohelix/runs/<id>/` folder (history, every iteration's
folder, notes and their versions, superseded attempts, logs, reports), their ref, the refs keeping rejected attempts' trees
(`refs/autohelix-rejected/<id>/<n>`) and any leftover copies. Refuses a running
run, and a run with accepted commits that its target branch lacks (they would be
lost) unless `--force`. Code on your branch and external artifacts stay intact.
To keep a run but hide it, use `archive`.

## `autohelix view`

One run, live in the terminal (run it in a second terminal while a run goes), as text once, as JSON, or as a live page in your browser.

```bash
autohelix view [RUN_ID] [-p PATH]
autohelix view [RUN_ID] [-p PATH] --once | --json
autohelix view [RUN_ID] [-p PATH] --web [--port 8765] [--open]
```

`RUN_ID` (or its last part) picks the run; the default is the current run. By default the terminal shows, on top, the run's status, time and cost, landing, the latest accepted metrics against the baseline, and one row per iteration, refreshed as the run goes. Below that is the live output of the agent during an iteration, of the latest `autohelix ask` or `review` call while gates, observe entries or stop commands make one (followed live, then from its stored transcript), and of the summary agent at the end (tool calls, text, elapsed time), and a status line with the iteration, phase (agent, checks, benchmark, …) and time in that phase, read from `.autohelix/runs/<id>/.internal/status.json`. On a short terminal the live output gets fewer lines first, then the oldest iteration rows are left out. When the run stops, the view says how and when, and waits for the next segment; Ctrl-C stops watching, not the run. Works with Claude Code, Codex and OpenCode logs. Safe to run alongside `autohelix run`; it only reads `.autohelix/` (and, for a judge or review call still running, its transcript in the run's copies folder).

`--once` prints the top part (status and iteration table) once as text and exits. `--json` prints the run once and exits, for scripts and agents managing runs: the run's row from [`dashboard --json`](#json-output) under `run`, plus `iterations`, one entry per iteration with `iteration`, `accepted`, `metrics`, `summary` (the agent's one-line summary; for records without one, the commit subject, or why it was rejected), `reason` (why it was rejected, `null` otherwise), `cost`, `cost_incomplete`, `seconds`, `gates` (in config order: `text`, `kind` (`command` or `metric`), `passed` (`true`, `false`, or `null` when not reached), and for commands that ran `return_code`, `duration_s` and, failed, the `output` tail; for checked metric gates `value`, `reference` and, failed, `reason`), `observe` (the entries that ran, in order: `name`, `ok`, `return_code`, `error`, `metrics`, `duration_s`) and `discarded` (paths outside the editable scope whose changes were dropped). In the text table a rejected iteration shows the gate that failed, with the agent's summary, failed observe entries and discarded paths on the lines under it. Use one of `--once`, `--json` and `--web` at a time.

`--web` serves a browser view of a run's `.autohelix/` state instead (`--port`, default 8765, and `--open` go with it). The header shows the goal's title (its first line) and run status (which iteration and phase is running, or whether the run finished, was interrupted, or crashed). The left side shows the best metric value with total time and cost, the metric over time, and one row per iteration with the agent's one-line summary (and, for a rejection, the gate that failed). The right side starts with the run summary once one is written (see [Configuration → Report](config.md#report)), then has one card per iteration with: why it was rejected; changes outside the editable scope that were discarded; the gates in list order (passed, failed with its output, or pending), each with its stored output (`iterations/NNN/gates/<k>/`: command, stdout, stderr and an `ask:` gate's judge transcript); its notes (`notes/iter-N.md`, as saved with the iteration); the iteration's own changes (from the commit it started from to its frozen tree, rejected attempts included while their ref is kept) and the changes against the baseline; one section per observe entry (`iterations/NNN/observe/<name>/`: status, metrics, stdout, stderr and captured files; a review's text as Markdown); the stop commands that ran after it (`iterations/NNN/stop/<k>/`, the one that stopped the run marked); and agent and judge activity (`iterations/NNN/agent.log` and the transcripts, live while they run). Paths are below `.autohelix/runs/<id>/`. Click a row or chart point to open that card. Notes that are not tied to an iteration are listed under Shared notes.

The server listens on `127.0.0.1` only and has no login, so on a shared machine other users of that machine can open it. From another machine, forward the port over SSH:

```bash
ssh -L 8765:127.0.0.1:8765 user@host   # then open http://localhost:8765
```

The page lives at `/run/<id>/`; the server's root (`/`) is the run list described under [`autohelix dashboard`](#autohelix-dashboard).

## `autohelix dashboard`

The project's runs, live in the terminal, as text once, as JSON, or as a live list in your browser.

```bash
autohelix dashboard [-p PATH] [--metric NAME]
autohelix dashboard [-p PATH] --once [--metric NAME]
autohelix dashboard [-p PATH] --json
autohelix dashboard [-p PATH] --web [--port 8765] [--open]
```

By default the terminal shows one row per run in `.autohelix/runs/`, redrawn every 5 seconds until Ctrl-C: the id, with its group after it (`20261111-1432-k3f9 (perf)`); the status (with the phase while running), with time and cost so far underneath; iterations, of the latest invocation's cap; one metric from the latest accepted iteration (`--metric`, default the first one of the most recently active run); landing: how many accepted commits the run's target branch lacks, and whether it can still fast-forward or has diverged; when the run was last active; and the goal's title (its first line), with the run's notes under it: why it was started (`run --why`) and the run it continues from (`run --import`). The header names the machine and checkout and shows the number of runs and the project's total cost, including archived runs (deleted runs no longer count). Live runs come first; archived runs are only counted. `--once` prints the table once and exits. `--json` prints every run once, archived ones too, and exits (see [JSON output](#json-output)). Use one of `--once`, `--json` and `--web` at a time.

`--web` serves the same rows as a live page instead (`--port`, default 8765, and `--open` go with it). There the metric's change against the run's own baseline is in the tooltip, and the metric is picked from a menu in the column header. Hover a cell for details such as the stop reason or budgets. The header names the machine and checkout (every dashboard is `localhost` over an SSH tunnel). Archived runs are hidden until you click their count in the header. Click a row for that run's [`autohelix view --web`](#autohelix-view) page. It is the same server as `autohelix view --web`, with the same `127.0.0.1`-only binding.

### JSON output

`dashboard --json` prints the same data the web page reads. Fields are only ever added, never renamed or removed; `schema` changes only if that promise has to be broken. Times are Unix seconds, costs are US dollars, and absent values are `null`.

- `schema` (1), `project` (folder name), `host`, `path` (absolute), `path_display` (with `~` for your home folder), and the checkout's `branch` (`null` when detached) and short `commit`.
- `runs`, most recently active first. Each run has:
  - `id`, `name`, `archived` (true or false), `created_at`, `last_activity`;
  - `group` (`null` outside a group) and `land` (`auto` or `manual`: the run's landing mode);
  - `config_file`, `goal` and `title` (the goal's first line, cut to about 80 characters), read from the config file as it is now in the checkout;
  - `status`: the last segment's `state` (`running`, `finished`, `interrupted`, `failed`, or `crashed` when its process is gone), `phase`, `iteration`, `iteration_started_at`, `phase_started_at`, `stopped_at`, `stop_reason` (`kind`, `detail`, …), and that segment's caps `max_iterations`, `max_cost_usd` and `max_time_seconds` (as in `progress`); `null` before the first segment;
  - `accepted`: the number of accepted iterations;
  - `metrics`: the latest accepted measurement, `iteration` and `values` (`name`, `value`, `baseline`, `direction`), or `null`;
  - `progress`: `iterations` attempted, `cost_usd` and `cost_incomplete` (the whole run's), `seconds` (the recorded iterations' wall clock, the baseline's included), and the latest invocation's [budget](#budgets) as run totals, "spent before it + its own cap", `null` where it had none: `max_iterations` (the last iteration it may run: the previous last iteration + `-n`, or `--until`), `max_cost_usd` (the run's cost before it + `--cost`) and `max_seconds` (`seconds` before it + `--time`). They come from the run's status, never from the config;
  - `target_branch` and `landing`: `commits` the target lacks and whether it can `fast_forward`, or `null` when either side is unavailable;
  - `why` (`run --why`), `archive_why` (`archive --why`) and `import` (`{"run": <id>}` for a run started with `run --import`), each `null` when absent.
  - A run that can't be read has only `id` and `error`.

## `autohelix hint`

Send a hint to a running agent, picked up at the start of its next iteration.

```bash
autohelix hint "MESSAGE" [-p PATH] [--run RUN_ID]
```

Requires a selected run (start one first). Appends a timestamped line to `.autohelix/runs/<id>/hints.md`. The agent reads that file (listed in its "Read before starting" section) each iteration, so a hint lets you steer a run without stopping it. Run it from a second terminal while `autohelix run` is going. Hints accumulate — the file holds the full history for the run.

## `autohelix ask`

Ask an agent a yes/no question about the current directory.

```bash
autohelix ask [--yes TEXT] [--no TEXT] [--rubric FILE] [--agent BACKEND] [--model MODEL] [--effort LEVEL] [--timeout SECONDS] [--command PATH] -- "QUESTION"
```

| Option | Description |
|--------|-------------|
| `--yes`, `--no` | What a "yes" or "no" answer means (optional; makes answers more consistent) |
| `--rubric` | A file with criteria for the answer |
| `--agent` | `claude` or `codex` (default: `agent.backend` from the config, else `claude`) |
| `--model`, `--effort` | Model and reasoning effort (default: from the config's `agent:`, unless `--agent` names another backend) |
| `--timeout` | Seconds before giving up, including the retry (default 600) |
| `--command` | The backend's executable (default: `agent.command` from the config, unless `--agent` names another backend) |

Put the question after `--`, so a question starting with `-` isn't read as an
option. Exits **0** for yes, **1** for no and **2** if no valid answer was
produced, and prints the reason, so it works anywhere a command does: `gates:`
(where an `ask:` entry is shorthand for it), `stop:`, or a script.

```yaml
stop:
  - autohelix ask -- "Is every item in SPEC.md implemented and tested?"
```

- **The judge sees only AutoHelix's instructions.** It gets a fixed system prompt with your question and answers in a fixed JSON format; it is told to treat file contents as evidence, not instructions. Settings come from `$AUTOHELIX_CONFIG` (set during a run: the run's stored config), else the config a bare `autohelix run` in the current directory would use (none, or several: the defaults): backend, command, model and effort only, never `agent.settings` or `extra_args`.
- **Candidate contents are checked before and after every judge call.** Tracked and nonignored files, modes, symlinks and submodule contents must remain unchanged; runtime feedback/log outputs are allowed. A stop judge after acceptance cannot change accepted code: its edits are discarded and its verdict ignored, with error and usage recorded. Backend restrictions remain best effort.
- **Read-only.** Claude Code gets only its read, grep and glob tools, and loads only user-level settings (no project `CLAUDE.md`, settings or hooks) and no MCP servers. Codex runs in its read-only sandbox; it still reads an `AGENTS.md` in the directory. OpenCode can't be used as a judge yet: it has no read-only mode.
- **Costs and logs.** During a run, each call's cost counts toward the iteration's cost and `--cost`, and its transcript is saved with the output of the command that ran it: `.autohelix/runs/<id>/iterations/NNN/gates/<k>/ask.log` for an `ask:` gate (`stop/<k>/` for a stop command, `observe/<name>/` for an observe entry; `ask-2.log`, … for further calls). While the command runs it is written to the command's private `$AUTOHELIX_LOG_DIR`. Outside a run, the cost is printed to stderr.
- An invalid answer or failed call is retried once. The command stays inside the timeout of the command that runs it.
- **Scripts own the exit code.** `ask` exits non-zero when it can't answer; a script that swallows that (`autohelix ask ... || true`) and exits 0 passes.
- **Judges can't change what they judge.** Each call fingerprints the files in its directory first. If the call changes one or adds a non-ignored file, it ends without retry: standalone ask exits 2, and in a run the call's usage record keeps an `integrity_violation` flag that rejects the iteration even if a wrapper script restores the files and exits 0.
- **Limits.** The judge reads the directory it runs in. In a run that is the checks or stop copy: the candidate's files and (in the checks copy) `.autohelix-iteration/candidate.diff`, with no notes or other harness material, but with any new or gitignored files earlier commands left there. Keep criteria files out of `scope.editable`.

## `autohelix review`

Review a change with a read-only agent; prints the review as feedback.

```bash
autohelix review [--previous] [--metric NAME:higher|lower]... [--agent BACKEND] [--model MODEL] [--effort LEVEL] [--timeout SECONDS] [--command PATH] [--base REV] -- "PROMPT"
```

| Option | Description |
|--------|-------------|
| `--previous` | In a run: include this entry's own review from the last accepted iteration |
| `--metric NAME:higher\|lower` | A number the review must report (repeatable) |
| `--agent` | `claude` or `codex` (default: `agent.backend` from the config, else `claude`) |
| `--model`, `--effort` | Model and reasoning effort (default: from the config's `agent:`, unless `--agent` names another backend) |
| `--timeout` | Seconds before giving up, including the retry (default: the command's deadline in a run, else 1800) |
| `--command` | The backend's executable (default: `agent.command` from the config, unless `--agent` names another backend) |
| `--base REV` | By hand: the commit the working tree is compared with (default `HEAD`) |

PROMPT is the review request; put it after `--`. A `review:` entry in
`observe:` is shorthand for this command (see
[Configuration → Review entries](config.md#review-entries)), and scripts can
call it directly, as they call `autohelix ask`.

**Two modes:**

- **In a run** (`$AUTOHELIX_CANDIDATE` is set, as for every command the harness
  runs): it makes its own fresh copy of exactly the candidate tree, with HEAD at
  `$AUTOHELIX_BASE`, inside `$AUTOHELIX_SCRATCH`, and removes it afterwards. The
  complete change (new files included) is `.autohelix-iteration/candidate.diff`
  there. With `--previous`, its own output from the last accepted iteration in
  which it succeeded (`$AUTOHELIX_PREVIOUS/stdout.txt`, without the metric
  lines) is `.autohelix-iteration/previous.md`, and the reviewer is told to treat
  it as a starting point and re-verify every item. Symlinks pointing outside
  the copy are removed, not followed, and listed in the reviewer's prompt.
  `--base` is ignored there, with a warning. It never reads the checks copy:
  gate and observe commands leave files there and run candidate code.
- **By hand**: it reviews the current directory in place. The change is the
  working tree (new, non-ignored files included; AutoHelix state left out)
  against `--base`, limited to the current directory. It is computed in a
  temporary index, so your index is untouched, but untracked files are hashed
  into `.git/objects` (as `git add` would; `git gc` removes them later).
  `--previous` does nothing there, with a warning. Outside a git repository,
  there is no diff: the reviewer reviews the files as they are.

**Output.** The review's prose on stdout, escaped so it can't carry a metric
line, then one `##autohelix[NAME=value]` line per `--metric`, written by the
command itself. The numbers come from the reviewer's structured answer (its
JSON output schema requires them); a missing or non-finite one is an error.
Outside a run, the cost is printed to stderr.

**Exit codes:** 0 when a review was produced; 2 when no valid review was
produced (after one retry), a requested number is missing, or the reviewer
changed files; 130 when interrupted.

**Read-only, three layers:**

1. The backend's read-only mode, as for `ask` (Claude Code: read, grep and glob
   tools, user-level settings only, no MCP; Codex: its read-only sandbox).
2. The reviewed directory is fingerprinted before and after the call, and in a
   run `candidate.diff` and `previous.md` must be unchanged too. A change fails
   the call; in a run its usage record keeps a sticky `integrity_violation`
   flag that rejects the iteration, even though observe failures otherwise
   don't.
3. In a run, its own fresh copy of the candidate tree, so nothing earlier
   commands or the agent wrote is in front of the reviewer.

Costs and logs are recorded as for `ask`
(`.autohelix/runs/<id>/iterations/NNN/observe/<name>/review.log` for a `review:` entry).

## `autohelix report`

Write (or rewrite) the run summary.

```bash
autohelix report [RUN_ID] [-p PATH]
```

`autohelix run` and `resume` write the summary automatically when a run finishes; use this command after an interrupted run, or to regenerate it. It uses the config the run stored when it started. One agent call reads the run's history, notes, logs and commits and writes `.autohelix/runs/<id>/report.md`. The summary inspects a detached checkout of the accepted run tip, even when the user's checkout is dirty. Standalone report refuses a live ownership lock. The previous summary is replaced only if the agent writes a new one. See [Configuration → Report](config.md#report).
