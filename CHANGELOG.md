# Changelog

## Unreleased

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
  run on exactly the code that would be accepted; a check, reviewer or
  `autohelix ask` call that changes it rejects the iteration, and the baseline
  runs the constraints once to catch a check that edits files.
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
  (was `.autohelix/`): `notes/`, `history.jsonl`, `hints.md`, `review.md`,
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
  across agents, reviewers and resumed runs.

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
