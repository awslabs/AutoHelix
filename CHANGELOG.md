# Changelog

## Unreleased

### Changed (breaking)

- **Runs.** Each `autohelix run` belongs to a run with its own id and state in
  `.autohelix/runs/<id>/`. A bare `autohelix run` resumes the most recent open
  run for the same config; `--new` starts another, `--run ID` picks one, and
  `--name` names it. State from earlier versions is refused: move or delete
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
- `autohelix abandon [--purge]` ends a run and keeps its evidence; `clear` is
  now an alias for abandoning the current run (it no longer archives state).
- `.autohelix/` is excluded through `.git/info/exclude` instead of a
  committed `.gitignore` change.

### Added

- `autohelix dashboard`: a live list of the project's runs, served with the
  run pages (`/run/<id>/`) by one local server.
- `--json` and `--print` on `autohelix dashboard` and `autohelix view`: a
  one-time snapshot for scripts (versioned with a `schema` field; fields are
  only ever added) or for the terminal.
- `autohelix land` and `autohelix view --run`.
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
