# Configuration

All configuration lives in `autohelix.yaml` at the project root (or another file
passed with `-c`).

**Paths are relative to the project root.** Every command (in `gates:`,
`observe:` and `stop:`) runs inside a per-iteration copy of the code, not the main
repo, and every path (`capture:` files, the patterns in `scope.editable` and
`scope.frozen`) is resolved there. Use relative paths (`src/`, `tests/`,
`results/log.json`); absolute paths point outside the copy and will either be
silently ignored, discard all the agent's work as out of scope, or crash the run.
(The one exception is long-lived outputs you deliberately keep outside the repo,
such as a model checkpoint directory, which are meant to live at a fixed absolute
path.) Untracked files such as a virtualenv or downloaded data aren't in the copy;
see [Concepts → Candidate Copies](concepts.md#candidate-copies).

## Full Reference

```yaml
# What the agent should achieve. The first line is the run's title (keep it short,
# like a commit subject); the whole text goes into the agent's prompt.
goal: |
  Make sort_list faster in sort.py.
  Keep the public API; don't add dependencies.

# name: perf              # readable part of new run ids
# land: auto              # new runs: auto (land at segment end) or manual
# prompt_template: prompts/agent.md   # rare: replaces the built-in agent prompt
# report: false           # don't write a summary when a run finishes

agent:
  backend: claude         # claude (default), codex, opencode, mock
  # model: claude-opus-4-8
  # reasoning_effort: high
  # timeout: 30m          # per iteration: a hard kill, with time-left hints
  # command: /path/to/claude
  # settings: {}          # codex: -c key=value pass-through
  # extra_args: []        # extra CLI arguments for the main agent
  # auto_memory: false    # Claude Code's built-in memory (off by default)
  # require_notes: true   # claude: block the agent from ending before it writes notes

scope:
  editable: [sort.py]     # only these may change
  # frozen: [tests/]      # never these (also inside editable)

gates:                    # anything that can reject an iteration
  - pytest -q             # must exit 0
  - command: pytest tests/slow
    timeout: 30m
  - ask: Does the change keep the public API?   # = autohelix ask -- "..."
  - metric: speed
    regress: 5%           # at most 5% worse than the last accepted iteration
  # - metric: speed
  #   improve: 0%         # must beat the last accepted iteration

observe:                  # feedback for the next agent, numbers for the harness
  - name: bench
    command: python benchmark.py     # prints ##autohelix[speed=1234]
    metrics: {speed: higher}
    capture: [results/log.json]
    # timeout: 30m
  - name: review          # = autohelix review -- "..."
    review: Review the diff for correctness. What is the most important next fix?
    # agent: {backend: codex, model: gpt-5.5}

stop:                     # end the run early once one exits 0
  - jq -e '.speed >= 5000' "$AUTOHELIX_METRICS"

budget:
  iterations: 10
  # cost: 20              # dollars
  # time: 2h              # total wall-clock
```

Top-level keys: `goal`, `name`, `land`, `prompt_template`, `report`, `agent`,
`scope`, `gates`, `observe`, `stop` and `budget`. An unknown key is a warning
(a possible typo); a key of the old format is an error that names its
replacement (see [Old → new](#old--new)). `autohelix run` lists every error and
starts nothing until they are fixed.

`gates:`, `observe:` and `stop:` are lists; a single string or mapping counts
as a list of one.

### Durations

Every time value (`agent.timeout`, an entry's `timeout:`, `budget.time`) is a
duration: a bare number is seconds (`90`), a string uses `h`, `m` and `s`
(`90s`, `30m`, `2h30m`). It must be positive and at most 30 days.

## Goal

A free-text description of what the agent should achieve. Be specific: this is
the core of the prompt the agent receives each iteration.

Its first line is the run's **title**, shown in the viewer, the run list and the
report, like a commit subject. A first line longer than about 80 characters is
shown cut (at its first sentence end, or with "…"), and validation warns. Put
details, rules and context on the following lines.

## Gates

`gates:` answers "what can reject my iteration?". There are three kinds of entry:

**Commands** must exit 0. An entry is a string, or a mapping with `command:`
and an optional `timeout:` (default 30m). A failing command rejects the
iteration; its output tail goes to the next agent.

```yaml
gates:
  - pytest -q
  - command: python check_correctness.py
    timeout: 10m
```

**`ask:` entries** are shorthand for [`autohelix ask`](cli.md#autohelix-ask), a
yes/no question to a read-only agent (exit 0 for yes):

```yaml
gates:
  - ask: Does the change keep the public API of sort.py?
    yes: Every public function keeps its name and signature.   # optional
    no: A public function was removed, renamed or changed.     # optional
    # timeout: 15m
    # agent: {backend: codex, model: gpt-5.5, reasoning_effort: high, timeout: 10m}
```

- `ask: <question>` becomes `autohelix ask [flags] -- "<question>"`: flags first,
  then `--`, so a question starting with `-` stays a question.
- `yes:` and `no:` say what each answer means (`--yes`, `--no`).
- `agent:` takes only `backend` (`claude` or `codex`), `model`,
  `reasoning_effort`, `timeout` and `command`; they become flags. Unset ones come
  from the top-level `agent:`, except that naming another backend drops the
  top-level `model`, `reasoning_effort` and `command` (they belong to the other
  backend). OpenCode has no read-only mode: with `agent.backend: opencode` at the
  top level, every `ask:` entry needs its own `agent.backend`.
- The judge's own time limit is 600 seconds unless set. `timeout:` is the whole
  command's deadline and also bounds the judge; `agent.timeout` alone sets the
  judge's limit and makes the command's deadline a minute longer. `agent.timeout`
  must be shorter than `timeout:`.

**`metric:` entries** run nothing. They compare a number from an `observe:`
entry with the **last accepted iteration**, the code being built on:

```yaml
gates:
  - metric: speed
    regress: 5%      # no more than 5% worse than the last accepted iteration
  - metric: errors
    improve: 0%      # strictly better than the last accepted iteration

observe:
  - name: bench
    command: python benchmark.py
    metrics: {speed: higher, errors: lower}
```

- `regress: N%`: the value may be worse than the reference by at most N%.
  `regress: 0%` means "no worse".
- `improve: N%`: the value must be strictly better than the reference, by at
  least N%. `improve: 0%` means "strictly better".
- Thresholds are percent strings (`5%`, `0.5%`). A bare number is an error:
  `0.05` would otherwise read as 0.05%.
- "Better" follows the metric's direction (`higher` or `lower`) in `observe:`.
  The metric must be declared there.
- **The reference** is the value measured on the run's tip commit: by the
  accepted iteration that created it, or by the baseline. An accepted no-op (no
  new commit) doesn't re-measure it, so the bar can't drift without a code
  change. If the tip's record lacks the metric (a gate added on resume), the
  most recent accepted commit, or the baseline, that has it is used; if no
  record has it at all, the gate passes.
- **Near zero:** percentages are of the reference's absolute value. With a
  reference of 0, `regress:` means "no worse than 0" and `improve:` means "any
  strict improvement". For counts (`missing` going 1 → 0) use `improve: 0%`,
  not a percentage.
- **A missing number fails the gate**: the entry that should produce it failed,
  or didn't print it, so nothing was verified. A non-finite value (`inf`,
  `nan`, `1e999`) counts as missing.

Comparing with the last accepted iteration rather than the best doesn't ratchet
on one lucky measurement. The cost is that many small regressions, each under
the threshold, can add up. Most runs need no metric gate: the agent sees the
numbers and decides.

> **Noise.** Under `improve:`, a noisy metric keeps lucky measurements: the
> tip's recorded value is biased upward and the loop stalls. Make the benchmark
> stable (repeats, medians: that is the benchmark's job, not the harness's) and
> set thresholds above its noise. The same holds, more mildly, for `regress:`.

## Observe

`observe:` lists commands whose output the next agent reads, and whose
`metrics:` become the run's numbers (the terminal, the viewer's chart, metric
gates, `$AUTOHELIX_METRICS`). Every entry needs a `name:` and exactly one of
`command:` or `review:`.

```yaml
observe:
  - name: bench
    command: python benchmark.py
    metrics:
      throughput: higher
      latency: lower
    capture: [results/profile.json, results/plots/]
    timeout: 30m
  - name: review
    review: |
      Review the diff for correctness and clarity.
      What single change would most improve the result next iteration?
```

**Names** are lowercase letters, digits, `_` and `-` (at most 64 characters),
unique in the list. They key the stored output, `$AUTOHELIX_PREVIOUS` and the
viewer's sections, so renaming an entry starts its history afresh. Metric names
(letters, digits, `_`) are unique across all entries.

### Command entries

- `command:` the shell command.
- `metrics:` optional: metric name → `higher` or `lower`.
- `capture:` optional: files or folders (relative to the copy) to save with the
  output. Not globs. Symlinks are never followed; anything that isn't a regular
  file or folder is skipped; a capture that doesn't exist is skipped silently.
  Paths inside `.git`, `.autohelix` or `.autohelix-iteration`, paths leaving
  the copy, and a top-level `stdout.txt` or `stderr.txt` are config errors.
- `timeout:` default 30m.

**Metric extraction.** The recommended form is a structured line on stdout, one
per metric:

```
##autohelix[throughput=1234.5]
##autohelix[latency=0.003]
```

Command entries also accept `name: value` or `name=value` lines matching a
declared name, and, when only one metric is declared, a lone structured line
with another name. A command without `metrics:` still runs and its output is
kept for the agent.

### Review entries

`review: <prompt>` is shorthand for
[`autohelix review`](cli.md#autohelix-review): a read-only agent reviews the
candidate and its text is feedback for the next agent. Each review runs in its
own fresh copy of exactly the candidate, with the complete change (new files
included) as `.autohelix-iteration/candidate.diff`. The prompt is the review
request; the reviewer isn't handed the goal or the metrics, so put whatever
context it needs in the prompt.

```yaml
observe:
  - name: progress
    review: |
      For each PRD.md item, give done / partial / missing with file:line evidence.
      Report `missing`: the number of items that are not fully done.
    previous: true            # = --previous
    metrics: {missing: lower} # = --metric missing:lower
    # timeout: 30m
    # agent: {backend: codex, model: gpt-5.5}
```

- `previous: true` gives the reviewer its own output from the last accepted
  iteration (as `.autohelix-iteration/previous.md`), as a starting point it must
  re-verify. This is how [progress tracking](concepts.md#progress-tracking)
  works.
- `metrics:` names numbers the review must report (added to its answer
  schema). They are printed as `##autohelix[...]` lines by the command itself;
  only those lines count (the review's prose is escaped, so it can't carry a
  number). A review that leaves a declared number out fails. What a number means
  is up to the prompt.
- `agent:` as for [`ask:` entries](#gates): `backend`, `model`,
  `reasoning_effort`, `timeout`, `command`.
- `timeout:` the command's deadline (default 30m), which also bounds the review;
  `agent.timeout` alone makes it a minute longer.

Review numbers are noisy and gameable: fine for display and `stop:`, not
recommended for gates.

### Failures continue

An observe entry that exits non-zero, times out, can't start, or (a review)
produces no valid answer doesn't reject the iteration. Its output and the
failure are recorded and shown to the next agent, and the remaining entries
run. **A failed entry contributes no metrics**, so a metric gate on one of its
numbers fails. Only gates reject. One exception: a command or review that
changes the candidate's files rejects the iteration (see
[Read-only evaluation](concepts.md#candidate-copies)).

### Stored output

Each entry's output is stored in `.autohelix/runs/<id>/observations/iter-N/<name>/`:
`stdout.txt`, `stderr.txt` (when not empty), and each capture at its relative
path. It is staged in a private folder after the command (and every process it
started) ended, then replaces that folder, so nothing a command wrote there
itself survives. Rejected iterations keep their output. The agent sees it in
its copy under `.autohelix-iteration/observations/`.

### Previous output

Every observe command gets `$AUTOHELIX_PREVIOUS`: a copy, made for that call,
of the entry's own folder from the most recent accepted iteration in which it
**succeeded** (the baseline counts). It is unset when there is none (the
baseline, or a new or renamed entry). Rejected iterations and failed runs of
the entry never count, so anything built on it moves only with accepted code.
Because it is a copy, neither the agent nor candidate code can rewrite what the
next call reads.

## Order within an iteration

1. Gate commands (including `ask:`), in list order. The first failure rejects
   the iteration and skips everything after it.
2. `observe:` entries, top to bottom. After each one, every metric gate whose
   number that entry declares is checked, in gates list order; a failure rejects
   the iteration and skips the remaining entries.
3. Accept (commit, record), then the stop commands.

List reviews last (the usual place), so their cost isn't spent on an iteration
a benchmark gate already rejected.

**The baseline** (iteration 0, on the run's starting code) runs every gate
command and every observe entry, `ask:` gates and reviews included; failures
are logged but reject nothing, and metric gates aren't checked. A gate, entry
or judge that modifies the code it checks stops the run with a setup error. A
metric from a `command:` entry that is missing at the baseline stops the run
too (there would be no reference). A metric from a `review:` entry stops it
only if a gate uses it; otherwise it is a warning (an LLM answer missing once
may be flakiness, not setup).

## Command environment

Gate, observe and stop commands run with the copy as their working directory,
in their own process group: when a command ends or times out, everything it
started is killed. Each gets:

| Variable | Content |
|---|---|
| `AUTOHELIX_CANDIDATE` | The candidate's tree (a git tree hash); at the baseline, the starting commit's tree |
| `AUTOHELIX_BASE` | The commit the candidate is built on (the last accepted tip) |
| `AUTOHELIX_PROJECT` | The project directory (in your checkout) |
| `AUTOHELIX_SCRATCH` | A private, empty folder for this command, removed after it |
| `AUTOHELIX_CONFIG` | The config file |
| `AUTOHELIX_ITERATION` | The iteration number (0 for the baseline) |
| `AUTOHELIX_RUN_ID`, `AUTOHELIX_RUN_DIR` | The run's id and its `.autohelix/runs/<id>/` folder |
| `AUTOHELIX_LOG_DIR` | Where judge and review calls save their logs |
| `AUTOHELIX_USAGE_FILE` | Where model calls record their cost (see [Cost Tracking](#cost-tracking)) |
| `AUTOHELIX_COMMAND_DEADLINE` | The command's deadline, in Unix seconds |
| `AUTOHELIX_PREVIOUS` | Observe commands only: see [Previous output](#previous-output) |
| `AUTOHELIX_METRICS`, `AUTOHELIX_OBSERVATIONS` | Stop commands only: see [Stop](#stop) |

`PYTHONPATH` is remapped so the project's own sources import from the copy.
That would let candidate code replace modules inside `autohelix ask` or
`autohelix review`, so `PATH` starts with a private folder holding an
`autohelix` launcher: it runs the harness's own installation in isolated mode,
whatever the command's `PYTHONPATH` and working directory. Call `autohelix` by
name in commands, not by a path to your virtualenv.

## Stop

`stop:` lists commands that end the run early. Each entry is a string, or a
mapping with `command:` and an optional `timeout:`. They run after the baseline
and after each **accepted** iteration, in a fresh copy of the accepted code,
in order until one exits:

- **0**: stop. The run ends after this iteration, and the last line the command
  printed to stdout is recorded as the reason (`speedup 10.3 ≥ 10`), so print one.
- **1**: keep going.
- **anything else, or a timeout**: an error. On the baseline it stops the run
  before the first iteration (fix the command and run again); later it prints a
  warning and the run continues.

On top of the [command environment](#command-environment), stop commands get:

- `AUTOHELIX_METRICS`: a JSON file with the metrics just measured, e.g.
  `{"speedup": 10.3}`. A target is a one-liner:
  `jq -e '.speedup >= 10' "$AUTOHELIX_METRICS"` (`jq -e` exits 0 when the
  expression is true, 1 when false).
- `AUTOHELIX_OBSERVATIONS`: a copy of the accepted iteration's
  `observations/iter-N/` folder (one subfolder per entry), so `grep` or
  `autohelix ask` can read a tracker's report.

For a qualitative condition use [`autohelix ask`](cli.md#autohelix-ask), e.g.
`autohelix ask -- "Is every item in SPEC.md implemented and tested?"`. Its cost
counts toward the run.

Keep stop commands out of `scope.editable`, and don't let them modify files: a
change is reported as an error (the acceptance stays recorded), and on the
baseline it stops the run. The agent's prompt lists them.

Stop conditions only end a run early: budgets still apply. If the baseline
already meets one, no iterations run. If you interrupt a run while its stop
commands are running, the iteration is kept but not checked; the next accepted
iteration is. A stop pauses the run: `autohelix resume` continues it (and it
stops again after the next accepted iteration if the condition still holds);
raise the target or remove the condition first.

Why a run ended is shown at the end of the run, in `autohelix view` (terminal
and `--web`), the run summary, and `.autohelix/runs/<id>/runtime/status.json`
(`stop_reason`).

## Scope

Controls which files the agent's changes are kept for:

- **`editable`**: only matching paths are kept.
- **`frozen`**: matching paths are never kept.
- Both together: editable minus frozen (`editable: [src/, tests/]`,
  `frozen: [tests/acceptance/]`).

Changes outside the scope are discarded when the agent's copy is frozen, before
any gate runs, so gates check exactly the tree that would be accepted. Nothing
is written back into the agent's copy. Discarded paths are recorded with the
iteration (`discarded` in history, the frozen ones also in `discarded_frozen`),
shown in the console, `view` and the viewer, and the next agent is told ("Your
changes to tests/x.py were discarded: outside the editable scope", or "...:
frozen" for frozen paths). A rejected attempt whose changes were all discarded
keeps no tree and gives the next agent no `rejected.patch`.

With no scope, the agent may edit the tests and benchmarks that gate it; a run
whose config has gate or observe commands but no scope warns at start. Patterns
that match no file are warnings.

## Agent

`agent:` configures the main agent, and is the default for every `ask:` and
`review:` call.

| Key | Meaning |
|---|---|
| `backend` | `claude` (default), `codex`, `opencode` or `mock` |
| `model` | The model (default: the backend's) |
| `reasoning_effort` | e.g. `low`, `medium`, `high`, `xhigh` |
| `timeout` | A duration per iteration; unset means no limit (see below) |
| `command` | The backend's executable (default `claude`, `codex`, `opencode` on `PATH`) |
| `settings` | Codex only: `-c key=value` pass-through |
| `extra_args` | Extra CLI arguments for the main agent |
| `auto_memory` | Claude Code's built-in memory; off by default for self-contained iterations |
| `require_notes` | Claude only: `true`, or `{min_chars: 50, max_retries: 3}`; a Stop hook keeps the agent going until it has written notes |

`ask:` and `review:` calls keep their own independent, read-only defaults and
use only `backend`, `model`, `reasoning_effort` and `command` from here, never
`settings`, `extra_args` or `auto_memory`. They don't use `agent.timeout` either:
`ask` defaults to 600 seconds, a review to its command's deadline.

**`agent.timeout`** is a hard kill: at the deadline the agent is stopped, even
mid-step, and the iteration is rejected unchecked (its tree is still kept, see
[Concepts → Rejected work](concepts.md#rejected-work)); the loop goes on with
the next iteration, as after any failed agent call, until three fail in a row. Where the backend
supports it, the agent also sees the time left, so it can wrap up and write
notes first: Claude Code through hooks after each tool call, Codex through the
same hook on CLI versions that support it (an older Codex warns and runs without
hints), OpenCode through a plugin. Every backend can run
`bash "$AUTOHELIX_TIME_LEFT_SCRIPT"`, and the prompt says so. See
[ML Experiments](ml-experiments.md) for when this matters.

## Budget

Caps how far a run goes. Combine any of:

- `iterations`: maximum number of iterations, a positive integer (default 5).
- `cost`: cumulative cost threshold in USD: the main agent, plus model calls
  the harness's commands report (`ask` gates, reviews), including the
  baseline's. Uses backend-reported amounts for Claude Code and OpenCode, and
  calculated API-equivalent estimates for Codex.
- `time`: maximum total wall-clock, a duration (`90m`, `2h`).

`cost` and `time` are **checked between iterations, never mid-iteration**: the
loop stops once the cumulative total crosses the limit, so a run can overshoot
by up to one iteration's worth of cost or time. For a hard cutoff within an
iteration use `agent.timeout`.

Both are also checked before starting an iteration, including after the
baseline and when resuming a run: a run past its budget stops at once
(`resume -n` extends only the iteration budget; raise `cost` or `time` here).

## Run identity and landing

`name` is an optional readable part of newly generated run ids
(`20261111-1432-<name>-k3f9`); it does not select an existing run. It accepts
lowercase letters, digits and hyphens, with at most 32 characters. Without a
name, nondefault config filename stems supply the name.

`land` is optional: `auto` fast-forwards accepted code to the starting branch
when a run segment ends, while `manual` keeps it on the run ref until
`autohelix land`. It is the default for new runs only: each run records its
mode when it starts (`--land`; else `manual` for a run started with `--group`,
whatever `land:` says; else `land:`; else `auto`) and keeps it;
`resume --land` changes it. CLI `--name` and `--land` override these values.

A bare `autohelix resume` picks the config path's most recently active run, or
name one; neither depends on `name` or `land`.

## Prompt template

`prompt_template:` is optional: a [Jinja2](https://jinja.palletsprojects.com/)
file that replaces the built-in agent prompt, as a path relative to the config
file (so it is versioned with it). Most configs need only `goal:`. The
variables a template gets are a documented contract; see
[Concepts → Prompt](concepts.md#prompt). `.autohelix/prompt.md` is no longer
read; a run warns while it exists.

## Report

When a run finishes after at least one iteration (a stop condition is met, or
the iteration, cost or time budget is reached), one more agent call reads the
run's history, notes, logs and commits and writes a short summary
to `.autohelix/runs/<id>/output/report.md`: what was achieved, which changes
mattered, what didn't work, what to try next, and anything worth checking
before relying on the results. An interrupted run gets no summary; run
[`autohelix report`](cli.md#autohelix-report) to write one. Set `report: false`
to skip it. The call uses the same agent backend and model as the run, with a
timeout of `agent.timeout` capped at 15 minutes. Its time and cost are shown
next to the summary in `autohelix view` and `report.html`; the cost is not
counted against `budget.cost`.

## Old → new

The first config format is not read. Each old key is an error naming its
replacement; there are no aliases and no migration.

| Old | New | Error message |
|---|---|---|
| `constraints:` | `gates:` (commands, unchanged) | `` `constraints:` is now `gates:` `` |
| `metrics:`, `observables:` | `observe:`, every entry with a `name:` | `` `metrics:` is now `observe:` (each entry needs a `name:`; `values:` is now `metrics:`) `` |
| `values:` in an entry | `metrics:` in an `observe:` entry | `` `values:` is now `metrics:` `` |
| `capture_stdout:` | removed: stdout is always saved | `` `capture_stdout:` is gone: stdout is always saved; remove it `` |
| `acceptance.metric_gates` (`max_regression_pct`) | `gates:` entry `metric:` with `regress: N%` (vs the last accepted iteration, not the best) or `improve: N%` | `` `acceptance:` is now a `gates:` entry: ... `` |
| `autohelix ask "..."` in `constraints:` | a `gates:` entry `ask:` (or the command, unchanged) | |
| `reviewer:` | an `observe:` entry with `name:` and `review:` | `` `reviewer:` is now an `observe:` entry with `name:` and `review: <prompt>` `` |
| `reviewer.model`, `reviewer.timeout_seconds` | `agent:` and `timeout:` on the review entry | |
| `agent.type` | `agent.backend` | `` `agent.type` is now `agent.backend` `` |
| `agent.timeout_seconds` | `agent.timeout` (a duration) | `` `agent.timeout_seconds` is now `agent.timeout` (a duration such as 30m) `` |
| `budget.iteration_time` | `agent.timeout` (always a hard kill, with time-left hints) | `` `budget.iteration_time` is now `agent.timeout` `` |
| `agent.settings.model` | `agent.model` | `` `agent.settings.model` is now `agent.model` `` |
| a bare number as the threshold (`max_regression_pct: 5` was one; `regress: 5` now) | `regress: 5%` | `write the percentage with %, e.g. 5%, got 5` |
| `timeout:` in seconds | durations everywhere (a bare number, or a string of digits like `"90"`, is still seconds) | |
| `.autohelix/prompt.md` | `prompt_template:` (relative to the config) | a warning at run start |

## Example Configs

### Performance Optimization

The most common use case: make code faster while keeping tests passing.

```yaml
goal: |
  Make sort_list faster. The current implementation is a slow bubble sort.

gates:
  - pytest test_sort.py

observe:
  - name: bench
    command: python benchmark.py
    metrics: {speed: higher}

scope:
  editable: [sort.py]

budget:
  iterations: 10
```

### ML Training Recipe

Capture full eval output (loss curves, per-task breakdowns) for the agent to analyze:

```yaml
goal: |
  Improve the training recipe to maximize average accuracy.

gates:
  - python test_recipe.py

observe:
  - name: eval
    command: python evaluate.py
    metrics: {avg_accuracy: higher}
    capture: [results/eval_log.json, results/per_task.jsonl]

scope:
  editable: [recipe.py]
```

### Code Quality Refactor

Improve readability without breaking anything, with an editor's review:

```yaml
goal: |
  Refactor utils.py for clarity: better naming, Pythonic idioms, less duplication.
  Don't change external behavior.

gates:
  - pytest tests/

observe:
  - name: review
    review: |
      Review src/utils.py for clarity and duplication. Name the single change
      that would most improve it next.

scope:
  editable: [src/utils.py]

budget:
  iterations: 5
```

### Metric Gates and Reviews

Regression protection on one number, a second measurement, and a review by
another model family:

```yaml
goal: |
  Optimize the attention kernel for maximum TFLOPS on A100.

agent:
  backend: claude
  model: claude-opus-4-8
  timeout: 30m

gates:
  - python check_correctness.py
  - metric: tflops
    regress: 5%

observe:
  - name: bench
    command: python benchmark.py
    metrics: {tflops: higher}
  - name: memory
    command: python measure_memory.py
    metrics: {memory_mb: lower}
  - name: review
    review: |
      Review the Triton kernel for correctness, numerical stability and
      performance. Suggest the single most impactful optimization.
    agent: {backend: codex}

scope:
  editable: [kernel.py]

budget:
  iterations: 20
```

### Working Through a List

Progress tracking with a review that keeps its own status, and a stop on the
number it reports (see [Concepts → Progress tracking](concepts.md#progress-tracking)):

```yaml
goal: |
  Implement every item in PRD.md
  Each iteration, take the highest-priority unfinished item, implement only
  that one, and add tests for it.

scope:
  editable: [src/, tests/]
  frozen: [PRD.md]

gates:
  - pytest -q

observe:
  - name: progress
    review: |
      For each PRD.md item, give done / partial / missing with file:line evidence.
      Report `missing`: the number of items that are not fully done.
    previous: true
    metrics: {missing: lower}

stop:
  - jq -e '.missing == 0' "$AUTOHELIX_METRICS"

budget: {iterations: 30, cost: 20}
```

### Minimal Config

Just a goal and one gate:

```yaml
goal: |
  Add type hints to all public functions in src/.

gates:
  - python -m mypy src/ --strict
```

## Auto Memory

Controls Claude Code's built-in memory system for the main agent:

- Defaults to off for reproducible, self-contained iterations; set
  `agent.auto_memory: true` to opt in.
- `ask` and `review` calls always run without it.
- Disabled via `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`; ignored for non-Claude backends.

## Cost Tracking

Available token usage and cost are captured from agent backends and shown:

- Per iteration (after accepted/rejected). A `+` after the amount (`$0.10+`) means part of that iteration's cost (agent or a command's model call) is unavailable, so the amount is what is known.
- Cumulative in the final summary
- Persisted in `.autohelix/runs/<id>/history.jsonl`

Iteration costs include the optimization agent and model calls made by the
commands the harness runs (`ask:` gates and `review:` entries, or
`autohelix ask` and `autohelix review` anywhere in `gates:`, `observe:` or
`stop:`). Run totals and `budget.cost` also include the baseline's commands;
report generation remains separate. History's `calls` list records each
attempted agent call and command model call with its role, usage and log path.
Records from earlier versions may list `reviewer` calls; they count the same way.

A command of your own can report its model calls the same way: append one JSON
line per call, e.g. `{"id": "...", "source": "my-eval", "cost_usd": 0.12}`, to the
file named by `$AUTOHELIX_USAGE_FILE` (set for every command the harness runs).
Lines with the same `id` are one call (the last line wins), so a call can be
recorded with `"cost_usd": null` when it starts and its cost when it ends.
Interrupted or failed actual main-agent attempts are recorded as rejected
iterations, including command calls and any available usage. Missing
charges remain unknown, so totals are incomplete rather than zero. Failures before
an agent call consume no iteration. Failed baseline attempts retain their costs in
iteration 0 for a clean retry. A process killed before a durable history write can
still lose unreturned usage; recovered accepted commits record that charge as unknown.

The numeric field is `usage.cost_usd` for every backend. `usage.cost_source` is
`backend` when the CLI supplies the amount, or `calculated` when AutoHelix
calculates it. Backend-supplied amounts can themselves be estimates; they are
not necessarily invoice totals. Older history without this metadata still loads.

**Codex costs are estimated** from reported input, cached-input, cache-write,
and output token counts and a bundled snapshot of the model's published
[OpenAI API rates](https://developers.openai.com/api/docs/pricing).
For subscription and Bedrock users, these are API-equivalent amounts, not
subscription charges or provider bills. The normal display uses the same
**Cost** label for every backend; provenance is saved in history:

- `cost_model` and, when available, `cost_provider` identify the model and provider.
- `cost_basis: openai_standard_api` identifies Standard pricing at ordinary
  context lengths, with 30-minute cache writes.
- `cost_rates` saves the rates per million tokens, cache-write lifetime, and
  verification date used for that iteration.
- `output_includes_reasoning` records whether separately reported reasoning
  tokens were already included in output. They are not counted twice.

The snapshot was verified on November 6, 2026. It supports listed GPT-6 models
and common GPT-5 Codex models; unknown names and expiring promotional prices are
not guessed. The model normally comes from the matching Codex session metadata,
so omitted `agent.model` works when that metadata is available. For ephemeral
sessions, an unambiguous explicit `agent.model` can provide the model instead.

Estimates exclude speed-tier and regional premiums, per-request long-context
surcharges, separately billed tools, and usage outside the captured thread.
Cache-write lifetimes other than 30 minutes are not inferred. Report and
supervisor calls remain separate; external evaluator calls count when they
record usage through `$AUTOHELIX_USAGE_FILE`. Codex agent duration is measured
locally even when usage is unavailable.

Missing, invalid, or interrupted usage remains **unavailable**, not zero.
Mixed totals are labeled **incomplete**. If an iteration or resumed history has
unavailable cost, `budget.cost` warns and tracks only the known amounts; retain
an iteration or time limit as well. The budget uses the saved estimate and is
checked between iterations, so it is an approximate stopping threshold, not a
guaranteed spending cap.
