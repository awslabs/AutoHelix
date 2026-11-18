# CLI Reference

Project commands default to the current directory if `--path` is omitted.
Commands that read runs (`resume`, `stop`, `land`, `archive`, `delete`, `hint`,
`view`, `report`, `wait`) also work from a subfolder: when the
folder has no runs (`.autohelix/runs/`), they use the nearest folder above it,
up to the top of its git checkout, that has, and say so ("Using project
../.."). `run` takes the folder as given; bare [`autohelix`](#autohelix) uses
the folder with runs, else the nearest one with a config, else the top of the
git checkout.

### Which run a command acts on

`land`, `hint`, `view --terminal` and `report` take a run id (or its last part); without
one they act on the **running run**, else the **most recently active run that
isn't [archived](#autohelix-archive)**, and say which on stderr ("Run k3f9 (the
running run; name one to choose another)"). With several runs running they
refuse: "Several runs are running: k3f9, m2x7; name one." With no run to act
on, they say so and how to start one ("No runs in /path/to/project; start one
with `autohelix run`."). `resume` has its own rule (a config's latest run,
below). Commands that take several ids (`archive`, `delete`, `stop`, `wait`)
check every id first: an unknown one refuses the whole command, nothing done;
then they act on each, report each, and exit 1 if any failed.

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

The copied `SKILL.md` says which AutoHelix version wrote it (a line after its
front matter), and tells the agent to point out a different `autohelix
--version` and suggest reinstalling with `--force`: an installed copy stays as
it was when AutoHelix is upgraded.

After installation, ask your agent to use `autohelix-setup` to prepare your
experiment (or run bare [`autohelix`](#autohelix) in a terminal: it starts the
agent with the bundled skill, without installing anything). It will inspect the project
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

## `autohelix`

Bare `autohelix` in a terminal is the entry point: the project's runs, then one
question that starts a coding agent session (Claude Code, Codex or OpenCode)
with the skill bundled with AutoHelix.

```bash
autohelix
```

The project is the folder with runs (the current folder or the nearest one
above it with `.autohelix/runs/`, within the git checkout, as for `view`), else the current folder or the nearest one above it with a
config (`autohelix.yaml` or `autohelix/*.yaml`, the folder `autohelix run`
there would use), else the top of the git checkout, whichever subfolder you
are in. The screen has a header (the version and the project), the project's
current runs as [`view --once`](#printed-once) draws them, under them
the command that applies to each run (`view` a live run, `resume` one that
stopped before its end, `land` waiting commits, `git merge autohelix/<id>`
for a run whose branch diverged from its target; an interrupted run or one that
stopped unexpectedly gets `resume` even with commits to land, which an
auto-landing resume lands), how many runs are
archived, and a pointer to `autohelix --help`:

```
  AutoHelix  v0.3.0 · ~/code/sorting
  ────────────────────────────────────────────────────────────────────────────
  run           state        iter   speed ↑  landing     cost  activity      goal
  m2xq perf     running       4/7  35,310.7  landed     $0.95  agent 1m 12s  Cut the memory…
  0mws sorting  interrupted   7/7  57,520.8  landed     $2.65  Nov 13        Make sort_list…

  m2xq  running · iteration 4      next: autohelix view m2xq
  0mws  interrupted · iteration 7  next: autohelix resume 0mws

  1 archived · autohelix --help: all commands

  ❯ Ask Claude Code about these runs
    Set up a new experiment with Claude Code   it drafts a config with you
  (q: quit)
```

(At 80 columns the first option's note, "it summarizes them, then answers
questions", doesn't fit whole, so it is left out.)

Choose with ↑/↓ and Enter, or an option's number; Enter alone takes the first,
q or Esc quits (exit 0), Ctrl-C interrupts (exit 130). Keys typed before the
screen is drawn are dropped. Nothing starts without a key
press, so `autohelix` is also a quick look at a project. The question depends
on what the project has:

| The project has | The question |
|---|---|
| runs that aren't archived | Ask *agent* about these runs / Set up a new experiment with *agent* |
| configs, no runs (a hand-written or half-finished `autohelix.yaml`) | autohelix.yaml, no runs yet: check and finish it with *agent* / Set up a new experiment with *agent* |
| neither (archived runs aside: "no current runs, 2 archived") | Set up an experiment with *agent*? Enter/q |

With several agents installed (looked for in the order `claude`, `codex`,
`opencode`), the labels say "an agent" and "Which agent?" follows, every
time. Without any, the screen says what to install and exits 1. Outside a git
checkout the screen says so (a run refuses the folder) and offers nothing to
start (exit 1); other git problems, such as no commit yet, are in the header's
Git line and in the agent's first prompt.

Every option starts the same session: `autohelix` is replaced by the agent's
usual interactive CLI, started in the project with a first prompt that points
at the bundled skill, names AutoHelix's own CLI and Python and the user docs
shipped in the package, lists the project's runs (id, state, last iteration)
and configs, and says which option you picked; you can switch at any point.

- **Ask about these runs**: the agent opens with a summary of the live runs
  plus the latest one (what was accepted, what was rejected and why, anything
  odd), then answers your questions from the run records. It sends `hint` or
  `stop` only when you ask.
- **Set up a new experiment**: the agent asks what you want to achieve, then
  shows the complete proposed config and walks through each section before
  changing project files: what it controls, why it was chosen, and how to
  adjust it, including effective defaults. It proposes a short name for the
  goal and saves the config as `autohelix/<name>.yaml` (`autohelix.yaml` for
  the project's first config), with `agent.cli` set to that agent unless
  you decide otherwise. When you agree, it writes the config, checks it with
  `autohelix run NAME --check`, commits it, and gives the `autohelix run NAME`
  command (with a budget) for you to run; it starts a run only when asked.
- **Check and finish** an existing config works the same way on that file.

Nothing is installed into the agent's settings, and the skill always matches
the installed AutoHelix (`autohelix skill install` puts a copy where your own
agent sessions find it). Without a terminal (a script, a pipe, an agent's
shell tool), `autohelix` prints the help, adds "Run `autohelix` in a terminal
to start.", and exits 2. All screens are drawn in
[tests/snapshots/](https://github.com/awslabs/AutoHelix/tree/main/tests/snapshots) (`entry-*.txt`).

To write a config by hand instead, start from an
[example](https://github.com/awslabs/AutoHelix/tree/main/examples), the configs in [Getting Started](getting-started.md)
and [Configuration](config.md), or the commented template shipped in the
package (`autohelix/templates/config_template.yaml`); `autohelix run` without a
config names all three.

### Config files

A project's configs live in exactly two places: `autohelix.yaml` at its root,
or `autohelix/<name>.yaml` for several loops. The `autohelix/` folder can
also hold prompt templates and helper scripts (`autohelix/prompts/perf.md`;
`prompt_template:` is relative to the config's folder). Commit the scripts:
iteration copies are made from the commit, so an untracked script a gate runs
is missing there. Whether to commit the configs:
[Configuration → Where configs live](config.md#where-configs-live). Keep `autohelix/` out of
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
  (`.autohelix/runs/<id>/config.yaml`, its prompt template beside it as
  `prompt_template.md`), and `resume`, `view`, `report` and the
  judges use that copy. `resume` refuses when the file's parsed config has
  changed since (comment, formatting and spelling edits don't count):
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
autohelix run [CONFIG] --check
```

`CONFIG` names a config in `autohelix/` (`perf` for `autohelix/perf.yaml`);
without it, the run uses `autohelix.yaml`, else the only config in
`autohelix/` (see [Config files](#config-files)). The project must be a git
checkout with a commit, and the first baseline needs no uncommitted changes to
tracked files. Commit any scripts its commands use: iteration copies are made
from the commit. Commit the config before the first `run`, or keep it private
([Where configs live](config.md#where-configs-live)); with auto landing, `run`
warns about an uncommitted one ("commit autohelix.yaml first: a commit on main
after this run lands makes `resume` refuse").

While the segment runs, `run` (and `resume`) keeps the machine from sleeping:
`caffeinate -i -w <pid>` on macOS, `systemd-inhibit` (idle and sleep) on Linux.
The block watches the AutoHelix process, so it goes when the segment ends or
the process is killed. Where the tool is missing or refuses (a container, no
systemd, polkit over ssh) there is no block and no message. It doesn't keep a
closed laptop lid from sleeping, and it keeps no credentials, SSO session or
VPN alive.

### What it prints

```text
  AutoHelix  v0.3.0 · run krp3 · sorting · 14:02
  Goal     Make sort_list faster in sort.py.
  Agent    Claude Code · sonnet
  Gates    pytest -q test_sort.py · speed regress 5%
  Observe  bench (speed ↑, mem_mb ↓)
  Scope    editable: sort.py
  Budget   7 iterations · $10.00 · 30m each
  ────────────────────────────────────────────────────────────────────────────
             time    cost   speed ↑         mem_mb ↓
  · base                       46.9              120
  ✓ 1         31s   $0.15  17,481.5 373×         118 -2%
  ✗ 2      2m 23s   $0.48  pytest -q test_sort.py failed (exit 1)
  ✓ 3      2m 06s   $0.32  35,310.7 753×          96 -20%
  ✗ 4     30m 00s   $1.10  agent timed out (30m)
  ✗ 5      1m 48s   $0.29    33,002              140
          speed 6.5% worse than iter 3 (5% allowed)
          discarded, outside the scope: autohelix.yaml (frozen), notes.txt
  ✓ 6      1m 51s   $0.31  57,520.8 1226×         95 -21%
  ✗ 7          4s          agent failed: claude exited 1: rate limited

  ────────────────────────────────────────────────────────────────────────────
  Done      iteration budget reached (7)
  speed     46.9 → 57,520.8  1226×  iter 6
  mem_mb    120 → 95  -21%  iter 6
  run       7 iterations · 3 accepted · 38m 43s · $2.65
  landed    3 commits on main
  next      autohelix view krp3 · autohelix resume krp3
  page      .autohelix/runs/20261115-1402-sorting-krp3/report.html
  summary   .autohelix/runs/20261115-1402-sorting-krp3/report.md
```

The header names the run (its id's last part, which every command accepts), what it
runs and the budget (the Agent line: the CLI, the configured model or `(its default)`,
and `via <executable>` when `AUTOHELIX_<CLI>_CMD` names another one); in a terminal the AutoHelix mark stands beside it. Then one row
per iteration: ✓ accepted or ✗ rejected, its time and cost, each metric's value
(an accepted row with its change from the baseline, `373×`, `-21%`, in green or red;
a rejection's values dimmed, without a change), and for a rejection why: the gate
that failed (`pytest -q failed (exit 1)`, `ask: … answered no`), a metric gate's
comparison with the iteration it compares against, or the agent's failure. A reason that doesn't fit goes on the line under the row, with failed
observe entries and discarded out-of-scope paths. While an iteration runs, a live row
shows its time and phase (`agent`, `gates`, `observe · bench` with the entry
running, `stop`), and under it, after a blank line, the commands that steer the run, dim:
`autohelix view krp3 · hint --run krp3 "…" · stop krp3 · Ctrl-C now` (`stop`
ends it after the current iteration; Ctrl-C interrupts it now, progress kept),
then two blank lines. Piped, the header has them as its `Steer` line instead. The end block says how the segment ended (`Done`, `Stopped`,
`Interrupted`, `Error`), each metric from the baseline to the run's tip, the totals,
what landed (`landing  failed: …` in yellow
when auto landing couldn't, the commits then waiting for `autohelix land`), what to
type next and the files written. `resume` shows the baseline and the last 5 rows before `resumed HH:MM`.

Wide terminals (where a reason still fits after the values) space the columns out;
at 80 columns they are as above. Narrower terminals drop the cost column, then the changes. Without a terminal (a
pipe, a log file) or colours there is no mark and no live row. The renders at 80 and
120 columns are in [`tests/snapshots/`](https://github.com/awslabs/AutoHelix/tree/main/tests/snapshots).

### Options

| Option | Description |
|--------|-------------|
| `-n`, `--iterations N` | Run N iterations (for `resume`, N more). Default 1, only when no budget flag is given |
| `--until N` | Run up to iteration N in total (safe to repeat) |
| `--cost USD` | Stop once this segment has spent USD (known costs) |
| `--time DURATION` | Start no iteration after DURATION of this segment's wall clock (`90m`, `2h`) |
| `--path`, `-p` | Project directory (default: current dir) |
| `--group NAME` | Make the run part of a [group](#groups): it sees the other runs of the group (their notes, history and code), and lands manually by default |
| `--land auto\|manual` | Land accepted code at the end of each segment, or retain it only on the run ref until `autohelix land`. Stored with the run. Default: `manual` with `--group`, else `auto` |
| `--import RUN_ID` | [Continue from another run](#continuing-from-another-run): its notes and history, read-only, in every iteration, and its last observe outputs at the baseline |
| `--why TEXT` | Why this run is started: one line (at most 300 characters), stored in `run.json` and shown by `autohelix view` |
| `--verbose` | Show agent command details, file paths, heartbeats |
| `--no-report` | Don't write the run summary when this segment finishes (not stored: pass it to each `resume` too) |
| `--check` | Start nothing: make every check the run makes before writing anything (the config and its prompt template, a clean checkout, the agent command, scope patterns, `--import`'s run), print what it finds, exit 0 when the run would start, else 1. For setup and CI |

`run` always starts a **new** run, with its own notes and history; to
continue one, use [`autohelix resume`](#autohelix-resume). It never refuses
because of other runs, but says what it sees: a run of the same config that is
still running when both land automatically (both will land on the same branch,
and whichever finishes second may not fast-forward), a previous run with commits
that haven't landed (this run starts from your branch without them), or
otherwise just which run came before. Runs started with `--group` don't warn
about each other (running side by side is the point), and neither does a live
run when either of the two lands manually (only one of them lands on its own).
Scripts that want to continue where they left off should call
`resume`, not `run`.

When the run finishes (not when interrupted), it writes a summary to `.autohelix/runs/<id>/report.md` — see [Configuration → Report](config.md#report).

On first start, performs preflight checks and evaluates the baseline (see
[Configuration → Order within an iteration](config.md#order-within-an-iteration)). When the run stops, it saves `.autohelix/runs/<id>/report.html`, a one-file copy of the [`autohelix view`](#the-web-page) run page — see [Concepts → Run page](concepts.md#run-page).

A refused dirty first start creates no run. The first valid baseline requires
a clean checkout at the run's starting commit. Resumes use the run's accepted
code, so unrelated staged/dirty user files can stay in the checkout. Accepted
commits advance `refs/autohelix/<id>` first, then auto landing fast-forwards the
starting branch before report generation. If dirty files would be overwritten,
the target diverged, or the target is checked out elsewhere, landing is refused
and the run ref retains the accepted code. Resolve that condition and land later.
One `run` or `resume` command is a **segment**, with its own
[budget](#budgets). Only one process can own a run at a time: a process-held
file lock enforces it, and whether a run is running comes from that lock (a
status that says running whose lock is free reads as crashed). The segment
starts as soon as the command holds the lock, before its checks (a clean
checkout, preflight): `autohelix stop` and `autohelix wait` see it from there, and
a start its checks refuse is a recorded segment that failed.

A failed or timed-out main-agent call records a rejected attempt (unchecked,
`agent failed: …` in red in the console row, `agent timed out (30m)` for a timeout),
consumes its number, and the loop goes on to the next iteration; budgets apply
as for any rejection. Three failed calls in a row stop the run ("the agent failed
3 times in a row: …"), with landing and the report as at any segment end, and
the command exits 1.
Setup failures before a call (copy creation, the prompt) do not consume an
iteration and stop the run with an error. Failed baseline attempts remain iteration 0 and retain their costs.
A run whose first baseline failed is a disposable attempt: the error ends "Fix it and run `autohelix run` again (`autohelix delete <id>` removes this attempt)", since a fixed config makes a new run (`resume` refuses a changed config).

### Exit codes

`run` and `resume` exit:

- **0** when the segment did what was asked or was stopped politely: a budget
  ran out, a [stop condition](config.md#stop) held, or `autohelix stop` asked
  (also `resume --until N` with nothing left to run, and `run --check` when the
  run would start);
- **1** on an error: the config or a check refused the start (no run to
  resume, a dirty checkout, failed preflight checks, also on `resume`), the
  baseline or an iteration failed with an error, the agent failed three times in
  a row, or auto landing failed (the iterations stay accepted on the run's ref;
  `autohelix land` once the cause is fixed);
- **130** when interrupted: Ctrl-C, SIGTERM, or `autohelix stop --now`
  (the iteration is recorded and the run lands as usual).

Other commands exit 0 on success and 1 on an error; usage errors (a bad flag)
exit 2. `ask`, `review` and `wait` have their own codes (below).

Legacy `.autohelix/history.jsonl` state is not migrated: move or delete the old
`.autohelix/` directory and run `git worktree prune` before starting a new run.

### Budgets

The config says what one iteration does; the flags say how long this
command goes on: one `run` or `resume` command is a **segment** of the run.
Every budget counts **this segment**, whatever earlier ones spent:

| Flag | Stops |
|---|---|
| `-n N` | after N iterations (`resume -n N`: N more) |
| `--until N` | after iteration N of the run: the one flag that counts a total, so a script can repeat it (`resume --until 20` twice runs nothing the second time) |
| `--cost USD` | once this segment has charged USD: agent calls, reviews, asks, every model call a command reports, the baseline's too (the summary, written after the loop, is counted but never stopped by it) |
| `--time DURATION` | once DURATION of this segment's wall clock has passed, the baseline included |

Whichever comes first ends the segment; a stop condition or [`autohelix stop`](#autohelix-stop) can end it sooner.
`-n` and `--until` don't go together. Without any budget flag a segment
runs **1** iteration (one more): a bare `autohelix run` is one checked
attempt, and a loop is typed (`-n 5`). With only `--cost` or `--time` it has no
iteration cap. Cost and time are checked before each iteration and after it,
never during one, so a segment can overshoot by one iteration's worth: for
a hard limit on one iteration use [`agent.timeout`](config.md#agent). Costs that
are unavailable don't count (`--cost` warns and tracks only the known ones): keep
`-n` or `--time` alongside. With `--cost` the only bound, an unknown cost stops
the segment before its next iteration ("cost unknown: --cost can't bound this
segment (add -n or --time)") only while none of the segment's charges was priced
(a backend that can't price); once one was, an unknown one (a timed-out call)
only warns.

`--time` starts no new iteration once the time is up; what is under way then
comes on top, so the worst case is `--time` plus one iteration, landing and the
report: the iteration's agent call (unbounded unless
[`agent.timeout`](config.md#agent) is set), each of its commands up to its
`timeout:` (default 30m), and the report agent's 15 minutes (`--no-report` skips it).

```bash
autohelix run perf -n 10 --cost 20      # at most 10 iterations and $20
autohelix resume -n 10 --cost 5         # 10 more iterations, up to $5 more
autohelix resume --cost 5               # no iteration cap: until $5 more is spent
autohelix resume --until 30 --time 2h   # up to iteration 30, starting none after 2 hours
```

The header names the budget in effect ("3 more iterations (to 8) · $5.00
more · 2h", or "1 iteration (default)"). The run's status records the
iteration and cost caps as run totals and the `--time` cap as this segment's
own; while the segment runs, `view --terminal`'s Budget field shows what is left of that cap
("47m 55s left (--time 1h)", "time up (--time 1h)" once past it); the run's total
time has no cap (`progress.seconds` in [JSON output](#json-output)). Every segment that starts
is also recorded in `.autohelix/runs/<id>/.internal/segments.jsonl` (see
[Concepts → Runs and run state](concepts.md#runs-and-run-state)).

### Groups

Runs started with the same `--group NAME` share what they learn. At the start
of each iteration the agent's copy gets, for every other non-archived run of the
project in that group (running or finished), its `run.json`, `history.jsonl` and
`notes/` in `.autohelix-iteration/runs/<id>/`, and the copy's git has its ref,
so `git diff autohelix/<id>`, `git show autohelix/<id>:path` and
`git checkout autohelix/<id> -- path` work there. The prompt's "Where things
are" list names them, one line each, with their latest accepted metrics. Code
taken from another run is checked like any other change; the prompt asks the
agent to name the run it came from in its summary.

That is all a group does: budgets, stop conditions, `resume`, `land`,
`archive` and `delete` stay per run. Use a group for runs that should build on
each other's notes and code, not for a comparison: runs that share what they
learn don't measure their approaches apart. To compare approaches (models,
prompts, or one setup several times), start separate runs with `--land manual`,
follow them on [`autohelix view`](#autohelix-view) (the web page's metric menu, or `view --once --metric`, picks
the column to compare), `autohelix land <winner>`, and archive the rest with
the reason (`autohelix archive m2x7 --why "slower than k3f9"`). Group runs land
manually by default: with auto landing the first to finish would land and every
other would diverge from it for good. `resume --group NAME` moves a run into a
group (from its next iteration) and `resume --group ''` takes it out; archiving
a run takes it out of its group, and resuming it brings it back. `--group`
doesn't change how a run lands: a run that lands automatically says so ("k3f9
still lands automatically; pass `--land manual` for a group").

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
  ref, `autohelix/<id>`; the prompt's "Where things are" list names it as
  imported ("this run continues from k3f9"). They are copied at the start of each iteration, as for a
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
# Fork from an old run's code: start a branch at it first (or at an iteration's commit).
git switch -c perf-fork autohelix/20261111-1432-perf-k3f9 && autohelix run perf --import k3f9
```

`run --import` says one thing about the imported run: when its accepted commits
aren't in the branch you start from, "k3f9's 2 accepted commits aren't in main:
land it first (`autohelix land k3f9`, or `git switch -c <branch>
autohelix/<id>`) to start from them" (`git merge autohelix/<id>` instead of
`land` when main diverged; `--into <branch>` when you start from another branch
than the run's). Of the config's other runs it only names a live one that lands
on the same branch. The config-change refusal of `resume` says the same for a
run with unlanded commits.

Each continuation is a new run with a new baseline (its code differs), at the
cost of a chain of runs in `autohelix view`, where each shows what it continues
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
autohelix resume [RUN_ID | CONFIG] [-n MORE | --until N] [--cost USD] [--time DURATION] [-p PATH] [--group NAME] [--land auto|manual] [--verbose] [--no-report]
```

`resume` only continues the same run: its stored config, its own tip, more
budget. Use it after a budget ran out, an interruption (Ctrl-C, a killed
process, a reboot, three agent failures), a stop condition or `autohelix stop`, or to change
`--land` or `--group`. Anything else is a new run: [`run --import
<id>`](#continuing-from-another-run).

| Option | Description |
|--------|-------------|
| `-n`, `--iterations` | How many **more** iterations to run (default 1, only when no budget flag is given) |
| `--until N` | Run up to iteration N (a total: running it twice does nothing the second time) |
| `--cost USD`, `--time DURATION` | Up to USD more, and DURATION of this segment's wall clock (see [Budgets](#budgets)) |
| `--land auto\|manual` | Change the run's landing mode (stored; default: keep it) |
| `--group NAME` | Put the run in group NAME from its next iteration (stored); `--group ''` takes it out of its group |

Other options as for `run`, and it keeps the machine awake the same way. A run always uses the config it stored when it
started ([Config files](#config-files)). Before spending anything, `resume`
compares it with the config file: when the file's parsed config differs (a
setting, or the prompt template's text), it refuses, names the settings, and
names both ways out: `autohelix run <config> --import <id>` applies the new
config in a new run, or restore the file to continue this one. What the run
would do counts, not how the file says it: comments, formatting, empty values
(`gates: []`, a key left out), a default written out (`agent: {cli:
claude-code}`) and another spelling (`10m` for `600`, `- pytest` for `- command:
pytest`) are not changes. A deleted file, or a deleted prompt template, isn't a
change: the run continues with its copy (other keys are still compared). The
stored copy is read-only. A
run started before runs stored their config can't be resumed: continue it
with `run --import <id>`. `resume perf` (a config name, as `run perf`
takes) continues the latest run of `autohelix/perf.yaml`; a run id takes
precedence over a config of the same name. Without either, the config is the
one a bare `autohelix run` would use (with several configs and no
`autohelix.yaml`, `resume` stops: name a run or a config), and `resume`
continues that config's most recently active run that isn't [archived](#autohelix-archive), preferring runs
that captured their baseline; it says which ("Resuming k3f9: 12 iterations,
last active 2h ago"). If that run is running in another terminal, `resume`
says so rather than picking an older one. With no run to resume it exits 1
(see [Exit codes](#exit-codes)).

Budgets are this segment's ([Budgets](#budgets)): `resume -n 10 --cost 5`
runs 10 more iterations, up to $5 more, however much the run spent before.
After an interrupted segment (Ctrl-C, a killed process, a reboot), a
`resume` without budget flags says what that one had left: "1 more (default);
the interrupted segment had 8 left: -n 8" (not when that segment had already stopped for a
reason of its own). `--until` with a number already reached says so ("Already at iteration 5;
--until 3 runs nothing.") and exits 0 without starting a segment: nothing is written, landed or
reported, except a `--land` or `--group` given with it, which is stored at once ("Run k3f9: landing
set to auto; already at iteration 5, --until 5 runs nothing."), so `resume ID --until <its last
iteration> --land auto` changes a run's landing mode without running anything. A run stopped by a
[stop condition](config.md#stop) is paused, not ended: `resume` continues it
and says so ("Run k3f9 stopped because score reached 0.9; it stops again after
the next accepted iteration if that still holds"): the condition is checked
again after the next accepted iteration.

When the target branch has moved past the run's tip (you or another run
committed on it after the run's tip), the run doesn't take those commits. With
auto landing, `resume` refuses before writing anything (after saying so if the
run is running elsewhere): "Everything run k3f9 accepted is in main, which
has 2 commits past its tip (committed after it). A run continues its own code: start a new run
that continues from it, `autohelix run perf --import k3f9`, or continue without
landing: `autohelix resume k3f9 --land manual`." With manual landing it says
so and continues from the run's own tip; its accepted commits then diverge from
the branch, so land them with `git merge autohelix/<id>`. Before the baseline
the run hasn't started from its commit yet, so nothing is compared; after it,
a run that never accepted a change says so ("main has 1 commit past run k3f9's
base (committed since it started; it hasn't accepted any changes)").

## `autohelix stop`

End a running run from another terminal or a script.

```bash
autohelix stop RUN_ID... [-p PATH] [--why TEXT] [--now]
```

| Option | Description |
|--------|-------------|
| `--why TEXT` | Why it is stopped: one line (at most 300 characters), shown with the stop reason |
| `--now` | Also interrupt the current iteration, like Ctrl-C in the run's terminal (same host only) |

Without `--now` the run ends **after its current iteration**, which is never
interrupted: the segment then ends as when its budget runs out (auto landing,
the summary unless `--no-report`, the run page), with state `finished`. Asked
during the baseline, it stops after the baseline. A stop condition met by that
iteration takes precedence. Asking again replaces the earlier request (and its
`--why`). `autohelix resume RUN_ID` continues the run later, as after a budget.

`--now` sends the run's process SIGTERM, which the run takes as Ctrl-C: the
iteration in progress is recorded as rejected (`interrupted`), the agent's and
commands' processes are ended, the run lands as usual, and no summary is
written; the segment's state is `interrupted`. It works only on the host the run
is running on, and only when that process is an `autohelix` process. A plain
`kill PID` (the `pid` in the run's `.internal/status.json`) ends a run the same way (without a stop reason); unlike Ctrl-C it also
reaches a run a script started in the background, which ignores SIGINT. Ctrl-C
and SIGTERM wait for an accept that is being recorded to finish.

`stop` refuses runs that aren't running (also one whose process is gone while
its status still says running: its lock is free). A segment counts as running
from the moment its command holds the run's lock, so a run just launched can be
stopped during its checks; it then stops right after them (after the baseline,
for a new run). It takes several ids and exits 1 if any of them was refused or, with
`--now`, could not be signalled (its request stands: it still stops after its
iteration). It prints what happens next ("Run k3f9 stops after its current
iteration (4).") and the `autohelix wait` command that says when it has ended
(repeat it until it prints `ended` or `idle`). A second `--now` while the first
is being handled sends nothing ("already being interrupted"). An interrupt
during the ending waits for the landing to finish.

The request is `.autohelix/runs/<id>/.internal/stop.json`, for the running
segment only: a request the segment didn't answer is removed when it ends, so it
never stops a later `resume`. The run checks it before each iteration (first
right after the baseline). While it is pending, `view` shows the run as
`running (stopping)` (the web page's tooltip says whether `--now` was used and
why) and `view --json` has it as `stop_requested`. The stop is recorded as
the segment's stop reason, in `status.json` and `segments.jsonl`: `kind:
request`, `detail` ("requested by autohelix stop: enough data"), `why`,
`requested_at`, and `now: true` for `--now` (see
[Concepts → Runs and run state](concepts.md#runs-and-run-state)).

## `autohelix land`

```bash
autohelix land [RUN_ID] [-p PATH] [--into BRANCH]
```

Fast-forward the run's starting branch to its accepted tip, preserving unrelated
checkout edits. Without `RUN_ID`, the [running or latest run](#which-run-a-command-acts-on). It only fast-forwards: when the
target diverged (a commit on it after the run's base, during a segment or
since), it refuses, says so, and gives what works: `git merge autohelix/<id>`
after reviewing the changes, or, for a run that lands automatically,
`autohelix resume <id> --land manual` to stop trying to land it. Auto landing
at a segment's end refuses the same way; AutoHelix never rewinds user commits.
A run created from a detached checkout requires `--into BRANCH`. Landing also
refuses branches that are being rebased.

`land` (and auto landing) records what it landed in the run's `run.json` as
`landed: {tip, target, at}`. So a run stays `landed` when its target branch gets
its commits as other commits, the usual pull request flow:

```bash
autohelix land k3f9                     # fast-forward your local main to the run's tip
git push origin main:perf-pr            # push it as a branch and open a PR from it
                                        # the PR is squash-merged (or rebased) on the remote
git fetch origin && git reset --hard origin/main   # your main as the remote has it
```

The runs table, `view`, the entry screen and `delete` then read k3f9 as
landed (no `--force` needed), while its tip isn't on main: the target lacks the
tip, but run.json says `land` landed that tip on this same branch. Nothing
compares the code, so if the PR is abandoned and main reset, the run still
reads landed (its commits stay on `autohelix/<id>`). Only a landing on the
run's own target is recorded: a landing on another branch (`land --into
other`) leaves the record alone, and a squash merge of the run's branch done
without `land` (`git merge --squash autohelix/<id>`) isn't recorded, so both
read diverged. Commits the run accepts after the landing (a `resume --land
manual`) count as unlanded and diverged: apply them with `git cherry-pick
<landed tip>..autohelix/<id>` (the command the runs table, `view` and the entry
screen give; `git merge` would re-apply the squashed commits and conflict), or
land them again through a PR.
An auto-landing `resume` refuses such a run like one whose target moved on: to
continue from the merged code, start a new run, `autohelix run --import k3f9`.

## `autohelix archive`

```bash
autohelix archive RUN_ID... [-p PATH] [--undo] [--why TEXT]
```

Sets runs aside: they are hidden from [`autohelix view`](#autohelix-view)'s run list
(behind a count) and skipped by a bare `resume`. Their history, notes, logs and
commits stay; `autohelix resume RUN_ID` resumes one and brings it back, and
`--undo` brings it back without running it. A running run can't be archived.
Use it for runs that are done or didn't work out, and for runs you merged some
other way than through `land` (a squash of the run's branch, cherry-pick):
those keep looking unlanded to git.
`--why TEXT` records why (one line, at most 300 characters) in each run's
`run.json` as `archive_why`, shown on the web page and in `--json`;
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

The project's runs on a live page in your browser (with `RUN_ID`, that run's page); `--terminal` follows one run live in the terminal; `--once` and `--json` print the runs (or one run) once.

```bash
autohelix view [RUN_ID] [-p PATH] [--port 8765] [--open]
autohelix view [RUN_ID] [-p PATH] --terminal
autohelix view [-p PATH] --once [--metric NAME] | --json
autohelix view RUN_ID [-p PATH] --once | --json
```

`RUN_ID` (or its last part) picks a run. Without it, the web page and `--once`/`--json` show the project's runs, and `--terminal` follows the [running or latest run](#which-run-a-command-acts-on), chosen when the view starts. Use one of `--terminal`, `--once` and `--json` at a time; `--port` and `--open` go with the web page, `--metric` with the runs table. Everything `view` does only reads `.autohelix/`, so it is safe alongside `autohelix run`.

### The web page

`autohelix view` serves the page on `127.0.0.1` (port 8765, or `--port`; `--open` opens it in a browser) until Ctrl-C: the run list at `/`, each run at `/run/<id>/`, live as the runs go. The server has no login, so on a shared machine other users of that machine can open it. From another machine, forward the port over SSH (or use `--terminal`):

```bash
ssh -L 8765:127.0.0.1:8765 user@host   # then open http://localhost:8765
```

The run list (`/`) has one row per run: its short id and name (with its group, and the config file under it when the run isn't named after it), state (with time and cost so far, and what is left of `--time`, under it), iterations, one metric from the latest accepted iteration, landing, last activity and the goal's title (with the run's notes under it: why it was started, what it continues from, why it was archived). The metric's change against the run's own baseline is in the tooltip, and the metric is picked from a menu in the column header. Hover a cell for details such as the stop reason or budgets. The header names the machine and checkout (every page is `localhost` over an SSH tunnel). Archived runs are hidden until you click their count in the header. Narrower than about 900 pixels, the Goal and Last active columns are hidden and the goal goes under the run's name. Click a row for that run's page.

A run's page (`/run/<id>/`) shows its `.autohelix/` state. The header shows the goal's title (its first line; click it for the rest) and run status (which iteration and phase is running, or whether the run finished, was interrupted, or crashed, and when; hover for why it ended). The left side shows the best metric value with total time and cost, the metric over time, and one row per iteration with the agent's one-line summary (and, for a rejection, the gate that failed), then a row for the run summary once one is written (see [Configuration → Report](config.md#report)). The right side is one panel: the selected iteration (the running one while a run is live; click a row or chart point, press `j`/`k` or use the panel's arrows to pick another; `#iter-N` in the address picks one), or, after the last iteration, the run summary (shown when the run has stopped; `#summary`). An iteration's panel has: why it was rejected; changes outside the editable scope that were discarded; the gates in list order (passed, failed with its output, or pending), each with its stored output (`iterations/NNN/gates/<k>/`: stdout, stderr and an `ask:` gate's judge transcript); its notes (`notes/iter-N.md`, as saved with the iteration); the iteration's own changes (from the commit it started from to its frozen tree, rejected attempts included while their ref is kept) and the changes against the baseline, each file folding on its own when there are several; one section per observe entry (`iterations/NNN/observe/<name>/`: status, metrics, stdout, stderr and captured files; a review's text as Markdown); the stop commands that ran after it (`iterations/NNN/stop/<k>/`, the one that stopped the run marked); and agent and judge activity (`iterations/NNN/agent.log` and the transcripts, live while they run). A section you open or close stays so as you move between iterations. Paths are below `.autohelix/runs/<id>/`. Notes that are not tied to an iteration are listed under Shared notes.

### In the terminal

`--terminal` follows one run (run it in a second terminal while a run goes). The terminal shows, on top, the run's own screen as `autohelix run` prints it: the header (goal, agent, gates, observe entries, scope, budget, the group and the run's notes, and the run's state in the title line), one row per iteration under the column header (outcome, time, cost, each metric with its change from the baseline, why a rejection was rejected), then the live row of the iteration under way (its time so far, ticking and with a spinner, and phase: agent, gates, `observe · <entry>`, stop) or, once the run stopped, its end block (how it ended, each metric's baseline → latest accepted value, totals, what landed, what to type next, and the run's report.html and report.md once written), refreshed as the run goes (the run is reread when its files change). The rows come from the run's history, so they read as `run` printed them; if the history can't be read, a yellow `!` line says so and the end block has only how the run ended. Commits waiting on the run's branch read `unlanded  3 commits on autohelix/<id> (manual landing)` with `autohelix land` among the next commands, as in `run`'s end block; on a branch that diverged from its target, `(diverged from main)` with `git merge autohelix/<id>` instead (`autohelix land` only fast-forwards). Below that is the live output of the agent during an iteration, of the latest `autohelix ask` or `review` call while gates, observe entries or stop commands make one (followed live, then from its stored transcript), and of the summary agent at the end (tool calls, text, elapsed time), and a status line with the iteration, phase (in the live row's words: agent, gates, observe, stop) and time in that phase, read from `.autohelix/runs/<id>/.internal/status.json`. While the run goes, the last line says what Ctrl-C does there: `Ctrl-C quits this view · autohelix stop krp3 stops the run · hint --run krp3 "…"`. On a short terminal the live output gets fewer lines first, then the oldest iteration rows are left out (the baseline stays). When the run stops, the view says how and when, and waits for the next segment; Ctrl-C stops watching, not the run. Works with Claude Code, Codex and OpenCode logs. Safe to run alongside `autohelix run`; it only reads `.autohelix/` (and, for a judge or review call still running, its transcript in the run's copies folder).

### Printed once

Without `RUN_ID`, `--once` prints the runs table and exits: a header that names the checkout and shows the number of runs and the project's total cost, including archived runs (deleted runs no longer count), then one line per run in `.autohelix/runs/`, cut to the terminal's width. A line has the run's short id and name (`krp3 sorting`, what every command accepts); its state (`running` in the accent colour, `running (stopping)` once [`autohelix stop`](#autohelix-stop) asked it to end); iterations, of the latest segment's cap; one metric from the latest accepted iteration (`--metric`, default the first one of the most recently active run); landing: `landed`, or how many accepted commits the run's target branch lacks (`2 to land`, or `2 diverged` when it can no longer fast-forward); the run's cost so far; activity: what a running run is doing (`agent 1m 12s`, `gates 30s`), else when it was last active (`2h ago`); and the goal's title (its first line), left out when the line has no room for it; on a terminal too narrow for the rest, the cost column goes first, so the activity stays whole. A run whose files can't be read gets its id and why, in red. Live runs come first; archived runs are only counted. The same table is on bare [`autohelix`](#autohelix)'s screen (`tests/snapshots/dashboard-*.txt` and `entry-*.txt` show both). `--json` prints every run once, archived ones too, and exits (see [JSON output](#json-output)).

With `RUN_ID`, `--once` prints the top part of `--terminal`'s screen once and exits (`tests/snapshots/view-*.txt` shows it), and `--json` prints the run once and exits, for scripts and agents managing runs: the run's row from [`view --json`](#json-output) under `run`, plus `iterations`, one entry per iteration with `iteration`, `accepted`, `metrics`, `summary` (the agent's one-line summary; for records without one, the commit subject, or why it was rejected), `reason` (why it was rejected, `null` otherwise), `cost`, `cost_incomplete`, `seconds`, `gates` (in config order: `text`, `kind` (`command` or `metric`), `passed` (`true`, `false`, or `null` when not reached), and for commands that ran `return_code`, `duration_s` (`timed_out` when one did) and, failed, the `output` tail; for checked metric gates `value`, `reference` and, failed, `reason`), `observe` (the entries that ran, in order: `name`, `ok`, `return_code`, `error`, `metrics`, `duration_s`), `stops` (the stop commands that ran, in order: `text`, `outcome` (`stop`, `continue` or `error`), `return_code`, `duration_s`, and `timed_out` when one did) and `discarded` (paths outside the editable scope whose changes were dropped). In the text table a rejected iteration shows the gate that failed (a metric gate as `speed 6.5% worse than iter 3 (5% allowed)`), with failed observe entries, failed stop commands and discarded paths on the lines under it.

### JSON output

`view --json` (without `RUN_ID`) prints the same data the run list reads. Fields are only ever added, never renamed or removed; `schema` changes only if that promise has to be broken. Times are Unix seconds, costs are US dollars, and absent values are `null`.

- `schema` (1), `project` (folder name), `host`, `path` (absolute), `path_display` (with `~` for your home folder), and the checkout's `branch` (`null` when detached) and short `commit`.
- `runs`, most recently active first. Each run has:
  - `id`, `name`, `archived` (true or false), `created_at`, `last_activity`;
  - `group` (`null` outside a group) and `land` (`auto` or `manual`: the run's landing mode);
  - `config_file`, `goal` and `title` (the goal's first line, cut to about 80 characters), read from the config file as it is now in the checkout;
  - `status`: the last segment's `state` (`running`, `finished`, `interrupted`, `failed`, or `crashed` when its process is gone), `phase`, `iteration`, `iteration_started_at`, `phase_started_at`, `stopped_at`, `stop_reason` (`kind`, `detail`, …; see [Concepts → Runs and run state](concepts.md#runs-and-run-state) for the kinds), that segment's caps `max_iterations`, `max_cost_usd` and `max_time_seconds` (as in `progress`), `segment_started_at`, when that segment started (its `--time` counts from there), and `segment`, its number (1, 2, … in the run); `null` before the first segment;
  - `stop_requested`: while the running segment has been asked to end by [`autohelix stop`](#autohelix-stop), `requested_at`, `why` and `now`; else `null`;
  - `accepted`: the number of accepted iterations;
  - `metrics`: the latest accepted measurement, `iteration` and `values` (`name`, `value`, `baseline`, `direction`), or `null`;
  - `progress`: `iterations` attempted, `cost_usd` and `cost_incomplete` (the whole run's), `seconds` (the recorded iterations' wall clock, the baseline's included), and the latest segment's [budget](#budgets), `null` where it had none: `max_iterations` (the last iteration it may run: the previous last iteration + `-n`, or `--until`) and `max_cost_usd` (the run's cost before it + `--cost`) as run totals, "spent before it + its own cap"; `max_seconds`, that segment's own `--time` (not a run total: compare it with the time since `status.segment_started_at`, not with `seconds`). They come from the run's status, never from the config;
  - `agent`: the latest main-agent call's `model` (the one the CLI reported, `null` if none) and `executable`, `{}` before any call;
  - `target_branch` and `landing`: how the run's tip stands against its target, `relation` (`same`, `ahead`: the target can fast-forward onto it, `behind`: the target has commits past it, `diverged`, or `landed`: the target lacks the tip, but it is the one `autohelix land` last landed on this branch, as after a squash-merged PR), `commits` (the run's accepted commits the target lacks), `behind` (the target's commits the run lacks), whether the target can `fast_forward` (`same` or `ahead`), or `null` when either side is unavailable; `landing_words`: the runs table's `cell` (`landed`, `2 to land`, `2 diverged`, `—`), `text`, what it means and what works (the run list's tooltip), the end block's `note` (`manual landing`, `diverged from main`) and the `commands` that land it (`autohelix land <id>`, `git merge autohelix/<id>`, `git cherry-pick <landed tip>..autohelix/<id>` after a squash-merged landing);
  - `why` (`run --why`), `archive_why` (`archive --why`) and `import` (`{"run": <id>}` for a run started with `run --import`), each `null` when absent.
  - A run that can't be read has only `id` and `error`.

## `autohelix wait`

Block until something happens in a run, print it as one JSON line, and exit.
For scripts and supervising agents: no polling, no reading of half-written
files.

```bash
autohelix wait [RUN_ID...] [-p PATH] [--after CURSOR] [--timeout DURATION]
autohelix wait [RUN_ID...] [-p PATH] [--after CURSOR] --follow
```

| Option | Description |
|--------|-------------|
| `RUN_ID...` | Watch these runs (an id or its last part); default: every run of the project, runs started later included |
| `--after CURSOR` | Continue right after the event that printed this cursor |
| `--timeout DURATION` | Give up after this long without an event (`30s`, `10m`, `1h30m`): print a `timeout` event and exit 2 |
| `--follow` | Never exit: print one line per event, in order (not with `--timeout`; no `idle` events) |
| `--path`, `-p` | Project directory (default: current dir) |

It only reads each run's event log, `.autohelix/runs/<id>/.internal/segments.jsonl`
(a line when a segment starts, when an iteration's record is final, when the
segment ends; see [Concepts](concepts.md#runs-and-run-state)): nothing written
or repaired, so it is safe beside running runs, and any number can wait at once.
Whether an open segment's process is gone comes from the run's lock, probed only
for a segment open on this host (once for a crashed one).

### The cursor

Every event has a `cursor`. `wait --after CURSOR` returns the next event after
that one, at once if one is already pending, so a loop of wait, inspect,
`wait --after` misses nothing and repeats nothing, however long the inspection
takes. Without `--after`, only what happens from now on counts. To watch a run
you are about to start, take a cursor first (a short `--timeout` prints one):

```bash
cursor=$(autohelix wait --timeout 1s | jq -r .cursor)   # now (a timeout event)
autohelix run perf -n 10 > run.log 2>&1 &
while :; do
  event=$(autohelix wait --after "$cursor" --timeout 30m)
  [ -n "$event" ] || break                     # an error: nothing on stdout
  cursor=$(jq -r .cursor <<<"$event")
  jq -c '{event, short, iteration, accepted, reason}' <<<"$event"
  case $(jq -r .event <<<"$event") in ended|crashed) break ;; esac   # of any run
done
```

A cursor records, per run, how many lines of its log were reported (and the
last crash); it doesn't compare times. It is opaque text (`2.` and base64):
pass it back as printed. A cursor works with
other run ids or none: it covers every run of the project, so `--after` reports
only what happened since, whichever runs are watched. A run missing from a
cursor (started after it) is new: everything it has is pending.

`--follow` turns the same events into a stream, e.g. for a notifier:
`autohelix wait --follow | ./notify.sh`. It exits quietly when the reader goes
away.

### Events

The event JSON is a public contract, like [`view --json`](#json-output):
`schema` is 1, and fields are only ever added. Every event has:

- `schema` (1), `event` (`started`, `iteration`, `ended`, `crashed`, `idle` or
  `timeout`), `run` and `short` (the run's id and its last part; `null` for
  `idle` and `timeout`), `time` (when it happened, Unix seconds; for `idle` and
  `timeout`, when reported), and `cursor`.

Per event:

- **`started`**: a segment (one `run` or `resume` command) started. `segment`
  (1, 2, … in the run), `start_iteration` (`0` when it begins with the
  baseline), `resumed` (`segment` > 1). `time` is the segment's start.
- **`iteration`**: an iteration was recorded (the baseline is iteration 0; a
  retried baseline is another event for 0).
  `iteration`, `accepted`, `reason` (as recorded: `baseline`, `gate failed:
  pytest -q`, `interrupted`, …; `null` for most accepted ones), `summary` (the
  agent's one line), `metrics`, `commit` (`null` when rejected or an accepted
  no-op), `cost_usd` (the known cost, `null` when none is known),
  `cost_incomplete` (some of its calls' costs are unknown), `agent_error` (why
  the agent call failed, else `null`). `time` is when it ended. An accepted
  iteration is reported once its record is final, after its stop commands.
- **`ended`**: a segment ended. `segment`, `state` (`finished`, `interrupted`
  or `failed`), `stop_reason` (an object with `kind` and `detail`, as in
  `status.json`, or `null`), `error` (a failed segment's error, else `null`),
  `last_iteration` (the run's last iteration), `iterations` (the records this
  segment added). `time` is when it ended.
- **`crashed`**: a segment never ended and its process is gone (nothing holds
  the run's lock, or a later segment started). `segment`, `last_iteration` (the
  last iteration reported before it crashed; `null` if none was, e.g. during its
  first baseline). Reported once per segment. `time` is its last event (its
  start or its last iteration). An iteration whose process was killed after its
  record but before its log line (an accepted one in its stop commands, say) is
  reported when the run is resumed: the resume logs it for the crashed segment
  before its own start (after the crash, if that was already reported).
- **`idle`**: only with run ids, without `--follow`: none of them is running
  and nothing is pending. `runs`: one `{run, short, state}` per run named,
  `state` being how its last segment ended (`finished`, `interrupted`,
  `failed`), `crashed`, `new` (nothing started yet) or `gone` (deleted while
  waiting). A segment starts as soon as its `run` or `resume` holds the run's
  lock; until then (the command's first second or so) the run is idle. So to
  watch a run you launch, `run` or `resume` alike, wait for its `started` event:
  without ids, from a cursor taken before.
- **`timeout`**: `--timeout` passed without an event. `timeout_seconds`. Its
  cursor continues where this wait started (the one given, or now): a timeout
  is a heartbeat, not a gap.

A run's events come in the order of its log: a segment's `started`, its
iterations, then its `ended` or `crashed`. Across runs the oldest pending event
comes first, but a line can be written after its `time`, so an event of one
run may follow a newer one of another run. Only complete lines of the log
count.

### Exit codes and limits

`wait` exits **0** after an event (`idle` included) and **2** after a timeout.
Errors (an unknown run id) exit **1**; usage errors (a bad option, a cursor
`wait` didn't print) exit 2, as for every command, but print nothing on stdout,
while a timeout prints its JSON line there. Ctrl-C exits 130.

A crash is detected only for a process on this host (from the run's lock). A
run that crashed on another host looks running until it is resumed: only
`--timeout` ends a wait on it.

## `autohelix hint`

Send a hint to a run's agent, picked up at the start of its next iteration.

```bash
autohelix hint "MESSAGE" [-p PATH] [--run RUN_ID]
```

The run is `--run RUN_ID`, else the [running or latest run](#which-run-a-command-acts-on); `hint` names it ("Hint added to run k3f9."), adding "(not running; read by its next `autohelix resume k3f9`)" when it isn't running. Appends a timestamped line to `.autohelix/runs/<id>/hints.md`. Each iteration's prompt holds the hints' text, under "Hints from the person running this run (newest last)", so a hint lets you steer a run without stopping it. Run it from a second terminal while `autohelix run` is going. Hints accumulate — the file holds the full history for the run.

## `autohelix ask`

Ask an agent a yes/no question about the candidate (in a run: a fresh copy of it, with `candidate.diff`), or by hand about the current directory (the change: its working tree against HEAD).

```bash
autohelix ask [--yes TEXT] [--no TEXT] [--rubric FILE] [--cli CLI] [--model MODEL] [--effort LEVEL] [--timeout DURATION] -- "QUESTION"
```

| Option | Description |
|--------|-------------|
| `--yes`, `--no` | What a "yes" or "no" answer means (optional; makes answers more consistent) |
| `--rubric` | A file with criteria for the answer |
| `--cli` | `claude-code` or `codex` (default: `agent.cli` from the config, else `claude-code`); the executable is `$AUTOHELIX_CLAUDE_CODE_CMD` or `$AUTOHELIX_CODEX_CMD`, else the name on `PATH` |
| `--model`, `--effort` | Model and reasoning effort, passed through (default: from the config's `agent:`, unless `--cli` names another CLI) |
| `--timeout` | How long before giving up, including the retry: a duration (`90s`, `10m`; a bare number is seconds; default 10m) |

Put the question after `--`, so a question starting with `-` isn't read as an
option. Exits **0** for yes, **1** for no and **2** if no valid answer was
produced, and prints the reason, so it works anywhere a command does: `gates:`
(where an `ask:` entry is shorthand for it), `stop:`, or a script.

```yaml
stop:
  - autohelix ask -- "Is every item in SPEC.md implemented and tested?"
```

- **The judge sees only AutoHelix's instructions.** It gets a fixed system prompt with your question and answers in a fixed JSON format; it is told to treat file contents as evidence, not instructions. Settings come from `$AUTOHELIX_CONFIG` (set during a run: the run's stored config), else the config a bare `autohelix run` in the current directory would use (none, or several: the defaults): `cli`, `model` and `effort` only, never `extra_args`. `--agent` and `--command` are gone: errors naming `--cli` and the variables.
- **What it judges.** In a run (`$AUTOHELIX_CANDIDATE` is set, as for every command the harness runs), its own fresh copy of exactly the candidate tree, made in `$AUTOHELIX_SCRATCH` and removed afterwards, as `autohelix review` does: the complete change (new files included) is `.autohelix-iteration/candidate.diff` there, and the judge's prompt names it. Files earlier commands wrote aren't in that copy: a gate that wants one judged writes it to `$AUTOHELIX_SCRATCH` and names it by absolute path in the question (`python bench.py > "$AUTOHELIX_SCRATCH/report.txt" && autohelix ask -- "Does $AUTOHELIX_SCRATCH/report.txt show ..."`). By hand, the current directory in place; the change, named in the prompt, is its working tree against `HEAD` (computed as for `review`). Outside a git repository there is no diff: the judge judges the files as they are.
- **Read-only.** Claude Code gets only its read, grep and glob tools, and loads only user-level settings (no project `CLAUDE.md`, settings or hooks) and no MCP servers. Codex runs in its read-only sandbox, with `project_doc_max_bytes=0` so it loads no `AGENTS.md` from the judged files (unconfirmed against a real Codex call). OpenCode can't be used as a judge yet: it has no read-only mode.
- **Costs and logs.** During a run, each call's cost counts toward the iteration's cost and `--cost`, and its transcript is saved with the output of the command that ran it: `.autohelix/runs/<id>/iterations/NNN/gates/<k>/ask.log` for an `ask:` gate (`stop/<k>/` for a stop command, `observe/<name>/` for an observe entry; `ask-2.log`, … for further calls). While the command runs it is written to the command's private `$AUTOHELIX_LOG_DIR`. Outside a run, a line on stderr gives the answer, the agent and where its settings came from, and the cost: `autohelix ask: yes · codex gpt-5 (autohelix/a.yaml) · $0.0120`, or `· claude-code (defaults: several configs here) · …`, so a by-hand answer from other settings than a run's is visible.
- An invalid answer or failed call is retried once. The command stays inside the timeout of the command that runs it.
- **Scripts own the exit code.** `ask` exits non-zero when it can't answer; a script that swallows that (`autohelix ask ... || true`) and exits 0 passes.
- **One write guard.** Each call fingerprints the judged files first (git's tracked and non-ignored untracked files, modes, symlinks and submodule contents; outside a repository, every file but cache folders); only the call's own transcript and the usage file may change. If the call changes or adds one, it ends without retry and `ask` exits 2. In a run that write was in the judge's own copy, so it never reaches the candidate.
- **Limits.** Keep criteria files out of `scope.editable`: the judged agent could edit them.

## `autohelix review`

Review a change with a read-only agent; prints the review as feedback.

```bash
autohelix review [--previous] [--metric NAME]... [--cli CLI] [--model MODEL] [--effort LEVEL] [--timeout DURATION] [--base REV] -- "PROMPT"
```

| Option | Description |
|--------|-------------|
| `--previous` | In a run: include this entry's own review from the last accepted iteration |
| `--metric NAME` | A number the review must report (repeatable). Which way is better is the config's business (a `review:` entry's `metrics:`); `NAME:higher` is an error naming `--metric NAME` |
| `--cli` | `claude-code` or `codex` (default: `agent.cli` from the config, else `claude-code`); the executable is `$AUTOHELIX_CLAUDE_CODE_CMD` or `$AUTOHELIX_CODEX_CMD`, else the name on `PATH` |
| `--model`, `--effort` | Model and reasoning effort, passed through (default: from the config's `agent:`, unless `--cli` names another CLI) |
| `--timeout` | How long before giving up, including the retry: a duration (default: the command's deadline in a run, else 30m) |
| `--base REV` | By hand: the commit the working tree is compared with (default `HEAD`) |

PROMPT is the review request; put it after `--`. A `review:` entry in
`observe:` is shorthand for this command (see
[Configuration → Review entries](config.md#review-entries)), and scripts can
call it directly, as they call `autohelix ask`.

**Two modes** (the same as `ask`'s):

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
Outside a run, a line on stderr names the agent, where its settings came from and the cost, as `ask`'s does.

**Exit codes:** 0 when a review was produced; 2 when no valid review was
produced (after one retry), a requested number is missing, or the reviewer
changed files; 130 when interrupted.

**Read-only**, as for `ask`: the backend's read-only mode (Claude Code: read,
grep and glob tools, user-level settings only, no MCP; Codex: its read-only
sandbox, no `AGENTS.md`), the one write guard around each call (a change fails
the call), and in a run its own fresh copy of the candidate tree, so nothing
earlier commands wrote is in front of the reviewer.

Costs and logs are recorded as for `ask`
(`.autohelix/runs/<id>/iterations/NNN/observe/<name>/review.log` for a `review:` entry).

## `autohelix report`

Write (or rewrite) the run summary.

```bash
autohelix report [RUN_ID] [-p PATH]
```

Without `RUN_ID`, the [running or latest run](#which-run-a-command-acts-on). `autohelix run` and `resume` write the summary automatically when a run finishes; use this command after an interrupted run, or to regenerate it. It uses the config the run stored when it started. One agent call reads the run's history, notes, logs and commits and writes `.autohelix/runs/<id>/report.md`. The summary inspects a detached checkout of the accepted run tip, even when the user's checkout is dirty. Standalone report refuses a live ownership lock. The previous summary is replaced only if the agent writes a new one. See [Configuration → Report](config.md#report).
