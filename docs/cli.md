# CLI Reference

Project commands default to the current directory if `--path` / positional path
is omitted.

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
the bundled skill, without installing anything). It will inspect the project and help configure scope, gates,
observe entries (commands and reviews), optional metric gates, and budgets.
Review the configuration and verification results before starting
`autohelix run`, unless you already requested a run.
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
| `NAME` | Write `autohelix/NAME.yaml` (lowercase letters, digits, hyphens, up to 32 characters); runs of it are named after it |
| `--path`, `-p` | Project directory (default: current dir) |
| `--agent` | Draft the config with this agent, without asking |

In a terminal, `setup` first asks whether to set up the loop with an agent or
write an empty config. When several agents are installed (looked for in the
order `claude`, `codex`, `opencode`), it then asks which one (Enter takes the
first). With an agent, `setup` is replaced by that agent's usual interactive
session, started in the project with a first prompt to follow the setup skill
bundled with AutoHelix: it asks what you want to achieve, and writes the
config with you, with `agent.backend` set to that agent unless you decide
otherwise. Nothing is installed into the agent's settings, and the skill
always matches the installed AutoHelix.

An empty config is the commented template; then edit it, commit it, and run
`autohelix run [NAME]`. Without a terminal (a script), `setup` writes the
template unless `--agent` is given. Without an agent on `PATH` it writes the
template too.

`setup` never overwrites a file. With `--agent`, an existing config (such as a
template written earlier) is completed with the agent; without it, an existing
config is an error. A bare `setup` is refused when `autohelix/` already has
configs: `autohelix.yaml` would take over a bare `autohelix run`, so name the
new one. `setup` doesn't need git and creates no `.autohelix/`; it warns when
`run` would refuse the folder (no git repository, or no commit yet).

`autohelix init` still works, as another name for `setup`.

### Config files

A project's configs live in exactly two places: `autohelix.yaml` at its root,
or `autohelix/<name>.yaml` for several loops. The `autohelix/` folder is
versioned, and can also hold prompt templates and helper scripts
(`autohelix/prompts/perf.md`; `prompt_template:` is relative to the config's
folder). `.autohelix/` is the runs' state, kept out of git.

- The configs in `autohelix/` are the `.yaml` files directly in it; keep
  other files there in subfolders.
- `autohelix run perf` uses `autohelix/perf.yaml`.
- A bare `autohelix run` uses `autohelix.yaml`, else the only config in
  `autohelix/`, else stops with the list of configs.
- The config's `name:` defaults to the file's stem, so runs of
  `autohelix/perf.yaml` are named like `20261112-1400-perf-0mws`;
  `autohelix.yaml` gives runs no name.
- A run keeps the config file it started with: `resume <id>` uses it, and
  `view`, `dashboard` and `report` read it. To change a run's config, edit
  that file.

There is no `--config` option, and nothing to initialize: the first
`autohelix run` creates `.autohelix/` and adds `.autohelix/` and
`__pycache__/` to the repository's local exclusions (`info/exclude`;
`.gitignore` is unchanged). `run` refuses if a conflicting `.gitignore` rule makes `.autohelix/` visible
to git despite the local exclusion. Add `.autohelix/` after any conflicting
negation rules in your `.gitignore`, then retry. AutoHelix leaves that file
unchanged.

## `autohelix run`

Start a new run of the optimization loop.

```bash
autohelix run [CONFIG] [-p PATH] [-n ITERATIONS] [--name NAME] [--group NAME] [--land auto|manual] [--verbose] [--no-report]
```

`CONFIG` names a config in `autohelix/` (`perf` for `autohelix/perf.yaml`);
without it, the run uses `autohelix.yaml`, else the only config in
`autohelix/` (see [Config files](#config-files)). The project must be a git
checkout with a commit, and the config must be committed: the first baseline
needs a clean working tree.

### Options

| Option | Description |
|--------|-------------|
| `-n`, `--iterations` | Iterations to run (overrides `budget.iterations`) |
| `--path`, `-p` | Project directory (default: current dir) |
| `--name NAME` | Readable part of the new run's id, lowercase letters/digits/hyphens, up to 32 characters (default: the config's `name:`, else its file name, none for `autohelix.yaml`) |
| `--group NAME` | Make the run part of a [group](#groups): it sees the other runs of the group (their notes, history and code), and lands manually by default |
| `--land auto\|manual` | Land accepted code at the end of each segment, or retain it only on the run ref until `autohelix land`. Stored with the run. Default: `manual` with `--group`, else `land:` in the config, else `auto` |
| `--verbose` | Show agent command details, file paths, heartbeats |
| `--no-report` | Don't write the run summary when the run finishes |

`run` always starts a **new** run, with its own budget, notes and history; to
continue one, use [`autohelix resume`](#autohelix-resume). It never refuses
because of other runs, but says what it sees: a run of the same config that is
still running (both will land on the same branch, and whichever finishes second
may not fast-forward), a previous run with commits that haven't landed (this
run starts from your branch without them), or otherwise just which run came
before. Runs started with `--group` don't warn about each other: running side
by side is the point. Scripts that want to continue where they left off should call
`resume`, not `run`.

When the run finishes (not when interrupted), it writes a summary to `.autohelix/runs/<id>/output/report.md` — see [Configuration → Report](config.md#report).

On first start, performs preflight checks and evaluates the baseline (see
[Configuration → Order within an iteration](config.md#order-within-an-iteration)). When the run stops, it saves `.autohelix/runs/<id>/output/report.html`, a one-file copy of the [`autohelix view --web`](#autohelix-view) page — see [Concepts → Run page](concepts.md#run-page).

A refused dirty first start creates no run. The first valid baseline requires
a clean checkout at the run's starting commit. Resumes use the run's accepted
code, so unrelated staged/dirty user files can stay in the checkout. Accepted
commits advance `refs/autohelix/<id>` first, then auto landing fast-forwards the
starting branch before report generation. If dirty files would be overwritten,
the target diverged, or the target is checked out elsewhere, landing is refused
and the run ref retains the accepted code. Resolve that condition and land later.
One invocation of `run` or `resume` is a segment; its iteration/time/cost limits
still apply to the whole run. Only one process can own a run at a time; a PID in
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

### Run ids

A run's id is the local date and time it started, its name if it has one, and
four random characters: `20261111-1432-k3f9`, or `20261111-1432-perf-k3f9`. So
`.autohelix/runs/` lists runs in the order they started. Every command that
takes a run id also takes just the last part (`k3f9`), which is unique in the
repository; git commands need the full id (`git merge autohelix/20261111-1432-k3f9`).

## `autohelix resume`

Continue a run.

```bash
autohelix resume [RUN_ID] [-n MORE | --until N] [-p PATH] [--group NAME] [--land auto|manual] [--verbose] [--no-report]
```

| Option | Description |
|--------|-------------|
| `-n`, `--iterations` | How many **more** iterations to run |
| `--until N` | Run up to iteration N (a total: running it twice does nothing the second time) |
| `--land auto\|manual` | Change the run's landing mode (stored; default: keep it) |
| `--group NAME` | Put the run in group NAME from its next iteration (stored); `--group ''` takes it out of its group |

Other options as for `run`. A run always uses the config file it started with;
to change it, edit that file. Without `RUN_ID`, the config is the one a bare
`autohelix run` would use (with several configs and no `autohelix.yaml`,
`resume` stops: name a run), and `resume` continues that config's most
recently active run that isn't [archived](#autohelix-archive), preferring runs
that captured their baseline; it says which ("Resuming k3f9: 12 iterations,
last active 2h ago"). If that run is running in another terminal, `resume`
says so rather than picking an older one. With no run to resume it exits
with status 3.

`-n` and `--until` override `budget.iterations` only. Cost and time budgets
stay run totals from the config: a run that reached `budget.cost` stops at once
until you raise it there. `--until` with a number already reached runs an empty
segment: it lands what is unlanded and saves `report.html`, but writes no new summary. A run stopped by a
[stop condition](config.md#stop) is paused, not ended: `resume` continues it,
and the condition is checked again after the next accepted iteration.

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
autohelix archive RUN_ID... [-p PATH] [--undo]
```

Sets runs aside: they are hidden from the [dashboard](#autohelix-dashboard)
(behind a count) and skipped by a bare `resume`. Their history, notes, logs and
commits stay; `autohelix resume RUN_ID` resumes one and brings it back, and
`--undo` brings it back without running it. A running run can't be archived.
Use it for runs that are done or didn't work out, and for runs you merged some
other way (squash, cherry-pick): those keep looking unlanded to git.

## `autohelix delete`

```bash
autohelix delete RUN_ID... [-p PATH] [--force]
```

Deletes runs: their `.autohelix/runs/<id>/` folder (history, notes, logs,
reports), their ref, the refs keeping rejected attempts' trees
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

`RUN_ID` (or its last part) picks the run; the default is the current run. By default the terminal shows, on top, the run's status, time and cost, landing, the latest accepted metrics against the baseline, and one row per iteration, refreshed as the run goes. Below that is the live output of the agent during an iteration and of the summary agent at the end (tool calls, text, elapsed time), and a status line with the iteration, phase (agent, checks, benchmark, …) and time in that phase, read from `.autohelix/runs/<id>/runtime/status.json`. On a short terminal the live output gets fewer lines first, then the oldest iteration rows are left out. When the run stops, the view says how and when, and waits for the next segment; Ctrl-C stops watching, not the run. Works with Claude Code, Codex and OpenCode logs. Safe to run alongside `autohelix run`; it only reads `.autohelix/`.

`--once` prints the top part (status and iteration table) once as text and exits. `--json` prints the run once and exits, for scripts and agents managing runs: the run's row from [`dashboard --json`](#json-output) under `run`, plus `iterations`, one entry per iteration with `iteration`, `accepted`, `metrics`, `summary` (the agent's one-line summary; for records without one, the commit subject, or why it was rejected), `reason` (why it was rejected, `null` otherwise), `cost`, `cost_incomplete`, `seconds`, `gates` (in config order: `text`, `kind` (`command` or `metric`), `passed` (`true`, `false`, or `null` when not reached), and for commands that ran `return_code`, `duration_s` and, failed, the `output` tail; for checked metric gates `value`, `reference` and, failed, `reason`), `observe` (the entries that ran, in order: `name`, `ok`, `return_code`, `error`, `metrics`, `duration_s`) and `discarded` (paths outside the editable scope whose changes were dropped). In the text table a rejected iteration shows the gate that failed, with the agent's summary, failed observe entries and discarded paths on the lines under it. Use one of `--once`, `--json` and `--web` at a time.

`--web` serves a browser view of a run's `.autohelix/` state instead (`--port`, default 8765, and `--open` go with it). The header shows the goal's title (its first line) and run status (which iteration and phase is running, or whether the run finished, was interrupted, or crashed). The left side shows the best metric value with total time and cost, the metric over time, and one row per iteration with the agent's one-line summary (and, for a rejection, the gate that failed). The right side starts with the run summary once one is written (see [Configuration → Report](config.md#report)), then has one card per iteration with: why it was rejected; changes outside the editable scope that were discarded; the gates in list order (passed, failed with its output, or pending); its notes (`notes/iter-N.md`); the iteration's own changes (from the commit it started from to its frozen tree, rejected attempts included while their ref is kept) and the changes against the baseline; one section per observe entry (`observations/iter-N/<name>/`: status, metrics, stdout, stderr and captured files; a review's text as Markdown); and agent activity (`logs/iter-N/`, live while it runs). Click a row or chart point to open that card. Notes that are not tied to an iteration are listed under Shared notes.

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

By default the terminal shows one row per run in `.autohelix/runs/`, redrawn every 5 seconds until Ctrl-C: the id, with its group after it (`20261111-1432-k3f9 (perf)`); the status (with the phase while running), with time and cost so far underneath; iterations of the run's budget; one metric from the latest accepted iteration (`--metric`, default the first one of the most recently active run); landing: how many accepted commits the run's target branch lacks, and whether it can still fast-forward or has diverged; when the run was last active; and the goal's title (its first line). The header names the machine and checkout and shows the number of runs and the project's total cost, including archived runs (deleted runs no longer count). Live runs come first; archived runs are only counted. `--once` prints the table once and exits. `--json` prints every run once, archived ones too, and exits (see [JSON output](#json-output)). Use one of `--once`, `--json` and `--web` at a time.

`--web` serves the same rows as a live page instead (`--port`, default 8765, and `--open` go with it). There the metric's change against the run's own baseline is in the tooltip, and the metric is picked from a menu in the column header. Hover a cell for details such as the stop reason or budgets. The header names the machine and checkout (every dashboard is `localhost` over an SSH tunnel). Archived runs are hidden until you click their count in the header. Click a row for that run's [`autohelix view --web`](#autohelix-view) page. It is the same server as `autohelix view --web`, with the same `127.0.0.1`-only binding.

### JSON output

`dashboard --json` prints the same data the web page reads. Fields are only ever added, never renamed or removed; `schema` changes only if that promise has to be broken. Times are Unix seconds, costs are US dollars, and absent values are `null`.

- `schema` (1), `project` (folder name), `host`, `path` (absolute), `path_display` (with `~` for your home folder), and the checkout's `branch` (`null` when detached) and short `commit`.
- `runs`, most recently active first. Each run has:
  - `id`, `name`, `archived` (true or false), `created_at`, `last_activity`;
  - `group` (`null` outside a group) and `land` (`auto` or `manual`: the run's landing mode);
  - `config_file`, `goal` and `title` (the goal's first line, cut to about 80 characters), read from the config file as it is now in the checkout;
  - `status`: the last segment's `state` (`running`, `finished`, `interrupted`, `failed`, or `crashed` when its process is gone), `phase`, `iteration`, `iteration_started_at`, `phase_started_at`, `stopped_at`, `stop_reason` (`kind`, `detail`, …) and `max_iterations`; `null` before the first segment;
  - `accepted`: the number of accepted iterations;
  - `metrics`: the latest accepted measurement, `iteration` and `values` (`name`, `value`, `baseline`, `direction`), or `null`;
  - `progress`: `iterations` attempted, `max_iterations`, `cost_usd`, `cost_incomplete`, `max_cost_usd`, `seconds` (iteration wall-clock, as the time budget counts it) and `max_seconds`;
  - `target_branch` and `landing`: `commits` the target lacks and whether it can `fast_forward`, or `null` when either side is unavailable.
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

- **The judge sees only AutoHelix's instructions.** It gets a fixed system prompt with your question and answers in a fixed JSON format; it is told to treat file contents as evidence, not instructions. Settings come from `$AUTOHELIX_CONFIG` (set during a run), else the config a bare `autohelix run` in the current directory would use (none, or several: the defaults): backend, command, model and effort only, never `agent.settings` or `extra_args`.
- **Candidate contents are checked before and after every judge call.** Tracked and nonignored files, modes, symlinks and submodule contents must remain unchanged; runtime feedback/log outputs are allowed. A stop judge after acceptance cannot change accepted code: its edits are discarded and its verdict ignored, with error and usage recorded. Backend restrictions remain best effort.
- **Read-only.** Claude Code gets only its read, grep and glob tools, and loads only user-level settings (no project `CLAUDE.md`, settings or hooks) and no MCP servers. Codex runs in its read-only sandbox; it still reads an `AGENTS.md` in the directory. OpenCode can't be used as a judge yet: it has no read-only mode.
- **Costs and logs.** During a run, each call's cost counts toward the iteration's cost and `budget.cost`, and its transcript is saved as `.autohelix/runs/<id>/logs/iter-N/call-*-command/ask.log`. Outside a run, the cost is printed to stderr.
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
(`.autohelix/runs/<id>/logs/iter-N/call-*-command/review.log`).

## `autohelix report`

Write (or rewrite) the run summary.

```bash
autohelix report [RUN_ID] [-p PATH]
```

`autohelix run` and `resume` write the summary automatically when a run finishes; use this command after an interrupted run, or to regenerate it. It uses the config file the run started with. One agent call reads the run's history, notes, logs and commits and writes `.autohelix/runs/<id>/output/report.md`. The summary inspects a detached checkout of the accepted run tip, even when the user's checkout is dirty. Standalone report refuses a live ownership lock. The previous summary is replaced only if the agent writes a new one. See [Configuration → Report](config.md#report).
