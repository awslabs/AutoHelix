# CLI Reference

All commands default to the current directory if `--path` / positional path is omitted.

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
autohelix run [--path PATH] [-n ITERATIONS] [--verbose] [--config PATH]
```

### Options

| Option | Description |
|--------|-------------|
| `-n`, `--iterations` | Maximum iterations (overrides `budget.iterations`) |
| `--verbose` | Show agent command details, file paths, heartbeats |
| `--config`, `-c` | Path to config file (default: `autohelix.yaml` in project) |
| `--path`, `-p` | Project directory (default: current dir) |

On first run, performs preflight checks and captures baseline observables. After every iteration it refreshes a self-contained dashboard at `.autohelix/output/dashboard.html` (open it in a browser to watch progress) — see [Concepts → Dashboard](concepts.md#dashboard).

## `autohelix clear`

Archive state and start fresh.

```bash
autohelix clear [path]
```

Archives everything (history, notes, reviews, logs) to `.autohelix/archive/<timestamp>/`. The agent starts with a clean slate. Archives are excluded from agent context via `.claudeignore`.

Does not touch code — use git directly to roll back.

## `autohelix watch`

Live-tail agent output in a second terminal.

```bash
autohelix watch [path]
```

Shows tool calls, text output, and elapsed time. Automatically switches to new iterations as they start.

## `autohelix hint`

Send a hint to a running agent, picked up at the start of its next iteration.

```bash
autohelix hint "MESSAGE" [--path PATH]
```

Appends a timestamped line to `.autohelix/hints.md`. The agent reads that file (listed in its "Read before starting" section) each iteration, so a hint lets you steer a run without stopping it. Run it from a second terminal while `autohelix run` is going. Hints accumulate — the file holds the full history for the run.

## `autohelix report`

Generate an agent-written analysis of the run.

```bash
autohelix report [path] [--config PATH]
```

| Option | Description |
|--------|-------------|
| `--config`, `-c` | Path to config file (default: from last run, or `autohelix.yaml`) |

Uses a one-shot agent call to analyze iteration history, notes, and git log, then writes `.autohelix/output/report.md`.

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
