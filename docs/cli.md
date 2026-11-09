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
Skill installation does not require a git repository or initialize AutoHelix.

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
experiment. It will inspect the project and help configure scope, constraints,
command observations, optional metric gates or advisory review, and budgets.
Review the configuration and verification results before starting
`autohelix run`, unless you already requested a run.
To request ongoing management, ask the agent to prepare, run, and supervise
within explicit effort limits. It should monitor saved outcomes through process
termination and use hints when useful for later iterations. See the
[setup-only and managed walkthroughs](getting-started.md#with-your-coding-agent).

Installing the Python package does not register skills automatically.

## `autohelix init`

Initialize AutoHelix in a project directory.

```bash
autohelix init [path]
```

Creates:

- `autohelix.yaml` — config template (won't overwrite existing)
- `.autohelix/` — runtime directory with notes, prompt template
- `.gitignore` entry for `.autohelix/`

Requires an existing git repository.

## `autohelix run`

Start or resume the optimization loop. Requires `autohelix init` to have been run first.

```bash
autohelix run [--path PATH] [-n ITERATIONS] [--verbose] [--config PATH] [--no-report]
```

### Options

| Option | Description |
|--------|-------------|
| `-n`, `--iterations` | Maximum iterations (overrides `budget.iterations`) |
| `--verbose` | Show agent command details, file paths, heartbeats |
| `--config`, `-c` | Path to config file (default: `autohelix.yaml` in project) |
| `--path`, `-p` | Project directory (default: current dir) |
| `--no-report` | Don't write the run summary when the run finishes |

When the run finishes (not when interrupted), it writes a summary to `.autohelix/output/report.md` — see [Configuration → Report](config.md#report).

On first run, performs preflight checks and captures baseline observables. When the run stops, it saves `.autohelix/output/report.html`, a one-file copy of the [`autohelix view`](#autohelix-view) page — see [Concepts → Run page](concepts.md#run-page).

## `autohelix clear`

Archive state and start fresh.

```bash
autohelix clear [path]
```

Archives everything (history, notes, reviews, logs) to `.autohelix/archive/<timestamp>/`. The agent starts with a clean slate. Archives are excluded from agent context via `.claudeignore`.

Does not touch code — use git directly to roll back.

## `autohelix watch`

Follow a run in a second terminal.

```bash
autohelix watch [path]
```

Shows the live output of whatever the run is doing: the agent during an iteration, the reviewer during a review, and the summary agent at the end (tool calls, text, elapsed time). Below it is a status line with the iteration, phase (agent, checks, review, …) and time in that phase, read from `.autohelix/runtime/run.json`. When the run stops, watch says how and when, and waits for the next run; Ctrl-C stops watching, not the run. Works with Claude Code, Codex and OpenCode logs.

## `autohelix view`

Live web view of a run, in your browser.

```bash
autohelix view [path] [--port 8765] [--open]
```

A browser view of a run's `.autohelix/` state. The header shows run status (which iteration and phase is running, or whether the run finished, was interrupted, or crashed). The left side shows the best metric value with total time and cost, the metric over time, and one row per iteration with the agent's one-line summary. The right side starts with the run summary once one is written (see [Configuration → Report](config.md#report)), then has one card per iteration with its notes (`notes/iter-N.md`), agent activity (`logs/iter-N/`, live while it runs), review, changes against the baseline, and captured output (`observations/iter-N/`); click a row or chart point to open that card. Notes that are not tied to an iteration are listed under Shared notes. Safe to run alongside `autohelix run`; it only reads `.autohelix/`.

The server listens on `127.0.0.1` only and has no login, so on a shared machine other users of that machine can open it. From another machine, forward the port over SSH:

```bash
ssh -L 8765:127.0.0.1:8765 user@host   # then open http://localhost:8765
```

## `autohelix hint`

Send a hint to a running agent, picked up at the start of its next iteration.

```bash
autohelix hint "MESSAGE" [--path PATH]
```

Appends a timestamped line to `.autohelix/hints.md`. The agent reads that file (listed in its "Read before starting" section) each iteration, so a hint lets you steer a run without stopping it. Run it from a second terminal while `autohelix run` is going. Hints accumulate — the file holds the full history for the run.

## `autohelix ask`

Ask an agent a yes/no question about the current directory.

```bash
autohelix ask "QUESTION" [--yes TEXT] [--no TEXT] [--rubric FILE] [--agent TYPE] [--model MODEL] [--effort LEVEL] [--timeout SECONDS]
```

| Option | Description |
|--------|-------------|
| `--yes`, `--no` | What a "yes" or "no" answer means (optional; makes answers more consistent) |
| `--rubric` | A file with criteria for the answer |
| `--agent` | `claude` or `codex` (default: `agent.type` from the config) |
| `--model`, `--effort` | Model and reasoning effort (default: from the config's `agent:`) |
| `--timeout` | Seconds before giving up, including the retry (default 600) |

Exits **0** for yes, **1** for no and **2** if no valid answer was produced, and prints the reason, so it works anywhere a command does: `stop:`, `constraints:`, or a script.

```yaml
stop:
  - autohelix ask "Is every item in SPEC.md implemented and tested?"
```

- **The judge sees only AutoHelix's instructions.** It gets a fixed system prompt with your question and answers in a fixed JSON format; it is told to treat file contents as evidence, not instructions. Settings come from `$AUTOHELIX_CONFIG` (set during a run) or `./autohelix.yaml`: backend, command, model and effort only, never `agent.settings` or `extra_args`.
- **Read-only.** Claude Code gets only its read, grep and glob tools, and loads only user-level settings (no project `CLAUDE.md`, settings or hooks) and no MCP servers. Codex runs in its read-only sandbox; it still reads an `AGENTS.md` in the directory. OpenCode can't be used as a judge yet: it has no read-only mode.
- **Costs and logs.** During a run, each call's cost counts toward the iteration's cost and `budget.cost`, and its transcript is saved as `.autohelix/logs/iter-N/ask-*.log`. Outside a run, the cost is printed to stderr.
- An invalid answer or failed call is retried once. The command stays inside the timeout of the check that runs it.
- **Limits.** The judge reads the same directory the agent worked in, including `.autohelix/notes/` and gitignored files, which scope enforcement doesn't revert. Keep criteria files out of `scope.editable`. Running the judge on a clean copy (without notes, gitignored files or agent instruction files) is planned.

## `autohelix report`

Write (or rewrite) the run summary.

```bash
autohelix report [path] [--config PATH]
```

| Option | Description |
|--------|-------------|
| `--config`, `-c` | Path to config file (default: from last run, or `autohelix.yaml`) |

`autohelix run` writes the summary automatically when a run finishes; use this command after an interrupted run, or to regenerate it. One agent call reads the run's history, notes, reviews, logs and commits and writes `.autohelix/output/report.md`. The previous summary is replaced only if the agent writes a new one. See [Configuration → Report](config.md#report).

## `autohelix parallel` (alpha)

Run multiple workers concurrently, each with its own config file and git worktree. Point them at the same goal to explore it in parallel, or give each a different one.

```bash
autohelix parallel --worker A.yaml --worker B.yaml [--sync-notes-every N]
autohelix parallel                                 # resume this repo's previous run
```

| Option | Description |
|--------|-------------|
| `--worker` | Per-worker config file (repeatable — one worker per flag) |
| `--sync-notes-every` | Sync notes between workers every N iterations (default: 1) |
| `--path`, `-p` | Target git repo (default: current dir) |
| `--verbose` | Verbose per-worker output |

Per-worker settings (model, timeout, agent, budget) come from each worker's own config file, not from command-line flags.

Each worker runs fully async in a worktree on its own branch (`parallel/worker-N`), driven to its config's `budget.iterations`. Workers only ever write their own branch, so they never conflict. Every `--sync-notes-every` iterations, each worker's notes are mirrored to a shared directory that peers symlink into `.autohelix/peer_notes/` — so agents can read what the others have tried, but not overwrite it.

Worktrees and branches **persist** across runs and nothing is deleted automatically: re-running `autohelix parallel` (or a bare invocation with no `--worker`) resumes each worker from where it stopped, reusing the recorded configs and sync interval. When workers share a single metric, results are shown as a leaderboard ranked by that metric (the first metric declared in each config); the branches are the deliverable, left for you to `git merge` the winner and remove the rest.

Iterations come from each worker's `budget.iterations`; model, timeout, agent backend, and all other per-worker settings live in each worker's config file.
