# Configuration

A loop's configuration lives in `autohelix.yaml` at the project root, or in
an experiment folder's `autohelix/<name>/autohelix.yaml` when a project has
several (`autohelix run <name>`; see
[CLI → Config files](cli.md#config-files)). Bare `autohelix` in a terminal
starts a coding agent that drafts one with you; to write one by hand, start from
the examples below or the commented template shipped in the package
(`autohelix/templates/config_template.yaml`).

**Paths are relative to the project root.** Every command (in `gates:`,
`observe:` and `stop:`) runs inside a per-iteration copy of the code, not the main
repo, and every path (`capture:` files, the patterns in `scope.editable` and
`scope.frozen`) is resolved there. Use relative paths (`src/`, `tests/`,
`results/log.json`); absolute paths point outside the copy and will either be
silently ignored or crash the run (in `scope:`, an absolute path or a `..`
component is a config error).
(The one exception is long-lived outputs you deliberately keep outside the repo,
such as model checkpoints: see
[ML Experiments → One folder per iteration](ml-experiments.md#one-folder-per-iteration).) Untracked files such as a virtualenv or downloaded data aren't in the copy;
see [Phases → Candidate copies](phases.md#candidate-copies).

## Where configs live

A run stores its config when it starts
([CLI → Config files](cli.md#config-files)), so git is only for sharing
configs. Either choice is fine, per experiment:

- **Shared** (a team's benchmark): commit `autohelix.yaml` or `autohelix/` with
  the project. Commit it before the first `run`: with auto landing, a commit on
  the branch after the run lands puts the branch past the run's tip, and
  `resume` refuses (`run` warns when the config, or a file of its experiment
  folder, isn't committed).
- **Private**: put `/autohelix/` (or `/autohelix.yaml`) in `.git/info/exclude`
  (your clone only) or `.gitignore`. An ignored config is never committed, so it
  can't move the branch. A config that is already committed must be untracked
  first (`git rm -r --cached autohelix`, then commit), or the exclude has no
  effect. An ignored `autohelix/` isn't in the copies
  ([Phases → Candidate copies](phases.md#candidate-copies)): commands run the
  experiment folder's scripts from `$AUTOHELIX_EXPERIMENT`
  ([Experiment folder](#experiment-folder)), and every other script they run
  is committed in the project.

For a private history of your configs, either make `autohelix/` its own git
repository:

```bash
echo /autohelix/ >> .git/info/exclude
cd autohelix && git init && git add perf/autohelix.yaml && git commit -m "perf config"
```

or make it a symlink into a personal repository of configs:

```bash
ln -s ~/configs/myproject autohelix
echo /autohelix >> .git/info/exclude    # no trailing slash: git sees a symlink as a file
```

AutoHelix follows the link. Two differences: `run.json` names the config by
its resolved, absolute path, and with auto landing `run` warns "commit
… first"; the warning doesn't apply to a file outside the project.

## Full Reference

```yaml
# What the agent should achieve. The first line is the run's title (keep it short,
# like a commit subject); the whole text goes into the agent's prompt.
goal: |
  Make sort_list faster in sort.py.
  Keep the public API; don't add dependencies.

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
    # agent: {cli: codex, model: gpt-5.5}

stop:                     # end the run early once one exits 0
  - jq -e '.speed >= 5000' "$AUTOHELIX_METRICS"

agent:
  cli: claude-code        # claude-code (default), codex or opencode
  model: opus             # passed through unchanged; unset = the CLI's default
  # effort: high
  # timeout: 30m          # per iteration: a hard kill, with time-left hints
  # extra_args: []        # extra CLI arguments for the main agent
  # auto_memory: false    # Claude Code's built-in memory (off by default)

# prompt_template: prompts/agent.md   # rare: replaces the built-in agent prompt
```

How a launch goes is not in the config either: how long (`autohelix run -n 10
--cost 20 --time 2h`, see [CLI → Budgets](cli.md#budgets)), how it lands
(`--land manual`, see [CLI → `autohelix run` options](cli.md#options)) and
whether it writes a summary (`--no-report`, see
[CLI → `autohelix report`](cli.md#autohelix-report)).

Top-level keys, in the order generated configs use: `goal`, `scope`,
`gates`, `observe`, `stop`, `agent` and `prompt_template` (any order loads). An unknown key is an error (a
misspelt `stop:` or `gates:` would otherwise do nothing); a key of the old
format is an error that names its replacement.
`goal:` must be a string (`|` for several lines), `agent.cli`, `model` and
`effort` strings (arguments go in `extra_args`), and
`auto_memory` `true` or `false`. `autohelix run` lists every error and starts
nothing until they are fixed; `autohelix run --check` makes the same checks
without starting (see [`run --check`](cli.md#autohelix-run)).

`gates:`, `observe:` and `stop:` are lists; a single string or mapping counts
as a list of one.

### Durations

Every time value (`agent.timeout`, an entry's `timeout:`, and `--time` on the
command line) is a
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
iteration; its output tail goes into the next agent's prompt, and its whole
stored output (see [Stored gate output](#stored-gate-output)) into its copy.

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
    # agent: {cli: codex, model: gpt-5.5, effort: high}
```

- `ask: <question>` becomes `autohelix ask [flags] -- "<question>"`: flags first,
  then `--`, so a question starting with `-` stays a question.
- `yes:` and `no:` say what each answer means (`--yes`, `--no`).
- `agent:` takes only `cli` (`claude-code` or `codex`), `model` and `effort`;
  they become flags (`--cli`, `--model`, `--effort`). Unset ones come from the
  top-level `agent:`, except that naming another CLI drops the top-level
  `model` and `effort` (they belong to the other CLI). The executable comes
  from the environment, as the main agent's does (see [Agent](#agent)).
  OpenCode has no read-only mode: with `agent.cli: opencode` at the top level,
  every `ask:` entry needs its own `agent.cli`, and an `autohelix ask` a script
  runs needs `--cli claude-code` or `--cli codex`.
- `timeout:` is the command's deadline (default 10m, at least 1m: the judge
  keeps 30s of it for its usage and its copy), which also bounds the judge. An
  entry's `agent.timeout` is an error naming `timeout:`.
- The judge works in its **own fresh copy of exactly the candidate**, as a
  review does, with the complete change as `.autohelix-iteration/candidate.diff`
  (named in its prompt): not the checks copy, so files earlier gates wrote
  aren't in front of it. A gate whose output should be judged writes it to
  `$AUTOHELIX_SCRATCH` and names it by absolute path in an `autohelix ask` of
  its own.

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
  change. A run keeps its config, so every gate is there from the baseline;
  if the tip's record has no value for the metric, the gate passes.
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

### Stored gate output

Every command and `ask:` gate that ran keeps its output in
`.autohelix/runs/<id>/iterations/NNN/gates/<k>/`, `<k>` being its 1-based
position in `gates:` (`gates/1/` is the first entry): `command.txt` (the
command as run; for an `ask:` gate the `autohelix ask` it became),
`stdout.txt`, `stderr.txt` (when not empty) and the judge's transcript
(`ask.log`; `ask-2.log`, … for further calls). `stdout.txt` and `stderr.txt`
keep the first 64 KB and the last 1 MB (compile errors are at the top, test
summaries at the bottom). Passing gates keep theirs too (for flaky tests).
Metric gates run nothing, so their positions have no folder (their value,
reference and outcome are in the record), and neither does a gate that never
ran after an earlier failure. The next agent's copy holds the folders of the
**failing** gates only; the viewer shows each gate's.

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

**Metric extraction.** A metric is read only from a structured line on stdout,
one per metric, matched by name (the last one wins):

```
##autohelix[throughput=1234.5]
##autohelix[latency=0.003]
```

Nothing else counts: a `speed: 5` line, anything on stderr, or a structured
line under another name (`##autohelix[sped=5]` for `speed`) leaves the metric
unreported, and the entry's record says how to print it. A non-finite value
(`inf`, `nan`) reports nothing. A command without `metrics:` still runs and its
output is kept for the agent.

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
    metrics: {missing: lower} # = --metric missing (the direction is for the metric gate)
    # timeout: 30m
    # agent: {cli: codex, model: gpt-5.5}
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
- `agent:` as for [`ask:` entries](#gates): `cli`, `model`, `effort`.
- `timeout:` the command's deadline (default 30m, at least 1m), which also bounds the review.

Review numbers are noisy and gameable: fine for display and `stop:`, not
recommended for gates.

### Failures continue

An observe entry that exits non-zero, times out, can't start, or (a review)
produces no valid answer doesn't reject the iteration. Its output and the
failure are recorded and shown to the next agent, and the remaining entries
run. **A failed entry contributes no metrics**, so a metric gate on one of its
numbers fails. Only gates reject. One exception: a command or review that
changes the candidate's files rejects the iteration (see
[Read-only evaluation](phases.md#candidate-copies)).

### Stored output

Each entry's output is stored in `.autohelix/runs/<id>/iterations/NNN/observe/<name>/`:
`stdout.txt` (not capped), `stderr.txt` (when not empty), each capture at its
relative path, and a review's transcript (`review.log`). It is staged in a
private folder after the command (and every process it started) ended, then
replaces that folder, so nothing a command wrote there itself survives.
Rejected iterations keep their output. The agent's copy holds the folders of
the last five iterations and of the last accepted one, at the same paths under
`.autohelix-iteration/iterations/`; its prompt names the exact folders.

Across iterations, put a header before each file so you can tell them apart:
`tail -n +1 .autohelix/runs/<id>/iterations/*/observe/review/stdout.txt`
(plain `cat` loses which iteration each came from). For numbers use
`history.jsonl`, e.g. `jq -c '[.iteration, .accepted, .metrics]'
.autohelix/runs/<id>/history.jsonl`.

### Previous output

Every observe command gets `$AUTOHELIX_PREVIOUS`: a copy, made for that call,
of the entry's own folder (`iterations/NNN/observe/<name>/`) from the most recent accepted iteration in which it
**succeeded** (the baseline counts). It is unset when there is none (the
baseline, or a new or renamed entry). In a run started with
[`run --import <id>`](cli.md#continuing-from-another-run), until the entry has
succeeded in this run (at the baseline, or later if it failed there), it is the
imported run's last successful output of the entry with the same name, so a
tracker continues from the imported run instead of starting over. Rejected iterations and failed runs of
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

Where gate, observe and stop commands run and the variables they get:
[Phases → Command environment](phases.md#command-environment).

### Experiment folder

A config in an experiment folder (`autohelix/NAME/autohelix.yaml`) can keep its
evaluator scripts and material beside it. The run stores the folder when it
starts, as `.autohelix/runs/<id>/experiment/` (without the config and its
template), and each gate, observe and stop command gets its own copy of that
as `$AUTOHELIX_EXPERIMENT`:

```
autohelix/console/
  autohelix.yaml
  render.py
```

```yaml
observe:
  - name: screens
    command: python "$AUTOHELIX_EXPERIMENT/render.py"
```

The copy's files are read-only and keep their exec bit; a command that changes
its copy changes nothing stored. `$AUTOHELIX_EXPERIMENT` is unset for a config
without a folder (the project's `autohelix.yaml`), so a command using it fails
visibly. The agent gets the same files in `.autohelix-iteration/experiment/`
(in its prompt's reading list), and `ask`/`review` in a run in
`.autohelix-iteration/experiment/` of their own copy.

A command that names the folder by its project path (`python
autohelix/console/render.py`) works only for committed files: copies are built
from the commit. `run --check` and the start's checks warn about such a path:
"autohelix/console/render.py (named by a command) isn't in the commit, so not
in the copy: use `$AUTOHELIX_EXPERIMENT/render.py`."

## Stop

`stop:` lists commands that end the run early. Each entry is a string, or a
mapping with `command:` and an optional `timeout:`. They run after the baseline
and after each **accepted** iteration, in a fresh copy of the accepted code,
in order until one exits:

- **0**: stop. The run ends after this iteration, and the last line the command
  printed to stdout is recorded as the reason (`speedup 10.3 ≥ 10`), so print one.
- **1**: keep going.
- **anything else, or a timeout**: an error. On the baseline it stops the run
  before the first iteration (fix the command and run again); later the run
  continues, and the iteration's row (in `run` and `view`) and the web view mark
  the failed command.

Each command's outcome (`stop`, `continue` or `error`), exit code and whether it
timed out are recorded in the iteration's `stops` (as gates are in `gates`).
Python exits 1 on an uncaught exception, which reads as "keep going": a script
should catch its own errors and exit 2 (`sys.exit(0 if done else 1)` inside a
`try`), or use `jq -e`.

On top of the [command environment](phases.md#command-environment), stop commands get:

- `AUTOHELIX_METRICS`: a JSON file with the metrics just measured, e.g.
  `{"speedup": 10.3}`. A target is a one-liner:
  `jq -e '.speedup >= 10' "$AUTOHELIX_METRICS"` (`jq -e` exits 0 when the
  expression is true, 1 when false).
- `AUTOHELIX_OBSERVATIONS`: a copy, made for that call, of the accepted
  iteration's `iterations/NNN/observe/` folder (one subfolder per entry; empty
  if it observed nothing), so `grep` or `autohelix ask` can read a tracker's
  report.

For a qualitative condition use [`autohelix ask`](cli.md#autohelix-ask), e.g.
`autohelix ask -- "Is every item in SPEC.md implemented and tested?"`. Its cost
counts toward the run.

Each stop command that ran keeps its output as a gate does (see
[Stored gate output](#stored-gate-output)), in `iterations/NNN/stop/<k>/`, `<k>`
being its position in `stop:`.

Keep stop commands out of `scope.editable`, and don't let them modify files: a
change is reported as an error (the acceptance stays recorded), and on the
baseline it stops the run. The agent's prompt lists them.

Stop conditions only end a run early: the command line's budget still applies. If the baseline
already meets one, no iterations run. If you interrupt a run while its stop
commands are running, the iteration is kept but not checked; the next accepted
iteration is. A stop pauses the run: `autohelix resume` continues it (and it
stops again after the next accepted iteration if the condition still holds).
Changing or removing the condition changes the config, which `resume` refuses
([CLI → `autohelix resume`](cli.md#autohelix-resume)): to go on past it with
the new condition, start `autohelix run <config> --import <id>`.

Why a run ended is shown at the end of the run, in `autohelix view` (the web page
and `--terminal`), the run summary, and `.autohelix/runs/<id>/.internal/status.json`
(`stop_reason`, `kind: condition`; the other kinds are `budget`, `agent` and
`request`, from [`autohelix stop`](cli.md#autohelix-stop); see
[CLI → Stop reasons](cli.md#stop-reasons)).

## Scope

Controls which files the agent's changes are kept for:

- **`editable`**: only matching paths are kept.
- **`frozen`**: matching paths are never kept.
- Both together: editable minus frozen (`editable: [src/, tests/]`,
  `frozen: [tests/acceptance/]`).

An entry is a path relative to the project folder, or a glob. A path covers
itself and, for a folder, everything inside it (`src`, `src/`). In a glob, `*`
and `?` match within one folder (`*.py` is only the top-level files, not
`tests/test_x.py`), `[abc]` is a character class, `**/` matches zero or more
folders (`tests/**/*.py` covers `tests/test_a.py` and `tests/unit/test_b.py`),
and a final `**` everything below. A glob that names a folder covers what is
inside it (`src/*` covers `src/pkg/a.py`). A leading `./` is ignored (`./src/` is
`src/`); `.` is the whole project; an absolute path or a `..` component is a
config error.

Changes outside the scope are discarded when the agent's copy is frozen, before
any gate runs, so gates check exactly the tree that would be accepted. Nothing
is written back into the agent's copy. Discarded paths are recorded with the
iteration (`discarded` in history, the frozen ones also in `discarded_frozen`),
shown in the console, `view` and the viewer, and the next agent is told ("Your
changes to tests/x.py were discarded: outside the editable scope", or "...:
frozen" for frozen paths). A rejected attempt whose changes were all discarded
keeps no tree and gives the next agent no `rejected.patch`.

The config file, its `prompt_template:` file and its experiment folder are
always frozen (the run keeps the config it started with:
[CLI → Config files](cli.md#config-files)). Scripts
the config calls are code: list them under `frozen:` if they must not change.

With no scope, the agent may edit the tests and benchmarks that gate it; a run
whose config has gate or observe commands but no scope warns at start. A pattern
that matches no committed file is a warning, not an error: an editable one may
name a file or folder the agent is to create (`editable: [solver.py, results/]`).

A git repository inside the copy (one the agent cloned or `git init`ed) rejects
the iteration, inside the scope or not: "nested git repository at vendor/lib:
remove its .git or clone outside the project". An agent that wants to read
another repository clones it outside its copy (under `/tmp`, say).

## Agent

`agent:` configures the main agent, and is the default for every `ask:` and
`review:` call.

| Key | Meaning |
|---|---|
| `cli` | `claude-code` (default), `codex` or `opencode` |
| `model` | The model, passed to the CLI unchanged; unset: the CLI's own default (see below) |
| `effort` | The reasoning effort, passed through unchanged (e.g. `low`, `medium`, `high`) |
| `timeout` | A duration per iteration; unset means no limit (see below) |
| `extra_args` | Extra CLI arguments for the main agent (also the place for a CLI's own settings, e.g. Codex `["-c", "key=value"]`) |
| `auto_memory` | Claude Code's built-in memory; off by default for self-contained iterations |

AutoHelix keeps no list of model names: `model:` goes to the CLI as written, and
the CLI says whether it knows it. Write it explicitly, so a run doesn't change
model when the CLI's default does.

**Claude Code** (`cli: claude-code`)

| | |
|---|---|
| Example `model:` | `opus`, `sonnet`, or a full name such as `claude-opus-4-8` |
| Default model | Claude Code's settings (`model` in `settings.json`), or `ANTHROPIC_MODEL` |
| `effort:` | `--effort` |
| Executable | `$AUTOHELIX_CLAUDE_CODE_CMD`, else `claude` on `PATH` |
| Models | [Claude Code model configuration](https://docs.claude.com/en/docs/claude-code/model-config) |

**Codex** (`cli: codex`)

| | |
|---|---|
| Example `model:` | `gpt-5.5` |
| Default model | `model` in Codex's `config.toml` (`~/.codex/config.toml`) |
| `effort:` | `-c model_reasoning_effort="..."` |
| Executable | `$AUTOHELIX_CODEX_CMD`, else `codex` on `PATH` |
| Models | [Codex models](https://developers.openai.com/codex/models) |

**OpenCode** (`cli: opencode`)

| | |
|---|---|
| Example `model:` | `anthropic/claude-sonnet-4-5` (provider/model) |
| Default model | `model` in OpenCode's config (`opencode.json`) |
| `effort:` | `--variant` |
| Executable | `$AUTOHELIX_OPENCODE_CMD`, else `opencode` on `PATH` |
| Models | [OpenCode models](https://opencode.ai/docs/models/) |

The executable is never in the config: set the variable (a wrapper script, a
Bedrock or proxy launcher) where you run `autohelix`. `run` checks before
starting that every agent the config runs (the main agent, each `ask:` and
`review:` entry's judge) can be found; `ask`, `review` and bare `autohelix` use
the same variables. A `resume` whose agent executable differs from the one the
run's last segment started warns ("The agent now starts …").

`ask:` and `review:` calls keep their own independent, read-only defaults and
use only `cli`, `model` and `effort` from here, never `extra_args` or
`auto_memory`. They don't use `agent.timeout` either: an entry's `timeout:`
bounds its judge (`ask` defaults to 10 minutes, a review to 30).

**`agent.timeout`** is a hard kill: at the deadline the agent is stopped, even
mid-step, and the iteration is rejected unchecked (its tree is still kept, see
[Concepts → Rejected work](concepts.md#rejected-work)); the loop goes on with
the next iteration, as after any failed agent call, until three fail in a row. Where the CLI
supports it, the agent also sees the time left, so it can wrap up and write
notes first: Claude Code through hooks after each tool call, Codex through the
same hook on CLI versions that support it (an older Codex warns and runs without
hints), OpenCode through a plugin. Every agent can run
`bash "$AUTOHELIX_TIME_LEFT_SCRIPT"`, and the prompt says so. See
[ML Experiments](ml-experiments.md) for when this matters.

## Prompt template

`prompt_template:` is optional: a [Jinja2](https://jinja.palletsprojects.com/)
file that replaces the built-in agent prompt, as a path relative to the config
file (so it lives beside it). A template that doesn't parse is a config
error naming the file and line, and one that fails to render (say `{{
last.gates.x.y }}`) stops the run with an error naming the file, before its
agent call. A run stores the template's text with its config when it starts, so
editing the template changes later runs, not this one. Most configs need only
`goal:`. The
variables a template gets are a documented contract; see
[Concepts → Prompt](concepts.md#prompt). `.autohelix/prompt.md` is no longer
read; a run warns while it exists.

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
```

### Metric Gates and Reviews

Regression protection on one number, a second measurement, and a review by
another model family:

```yaml
goal: |
  Optimize the attention kernel for maximum TFLOPS on A100.

agent:
  cli: claude-code
  model: opus
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
    agent: {cli: codex}

scope:
  editable: [kernel.py]
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
```

Run it with a cap: `autohelix run -n 30 --cost 20` (30 iterations or $20,
whichever comes first; the stop condition can end it sooner).

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
- Disabled via `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`; with `auto_memory: true` an
  inherited `CLAUDE_CODE_DISABLE_AUTO_MEMORY` is removed from the agent's
  environment. Ignored by the other CLIs.

## Cost Tracking

Costs are recorded without configuration; what is counted and how Codex costs
are estimated (from the model Codex names, else `agent.model`) is in
[Concepts → Cost Tracking](concepts.md#cost-tracking).
