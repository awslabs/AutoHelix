# AutoHelix configuration

These fields are supported by the version shipping this skill. If versions
differ, check the installed CLI and the packaged template, read with
AutoHelix's own Python (never by running `autohelix setup`):
`files('autohelix.templates').joinpath('config_template.yaml').read_text()`
(`from importlib.resources import files`).
All paths and commands below are examples; adapt them to implemented commands
in the target project. Schema validation does not execute those commands or
establish their runtime/cost, imports, emitted values, or generated artifacts.

```yaml
goal: |
  Reduce runtime in src/solver.py while preserving the existing behavior.

scope:
  editable:
    - src/solver.py

gates:
  - pytest tests/
  # Optional, if a stable measurement and regression tolerance are agreed:
  # - metric: runtime_ms
  #   regress: 5%  # Illustrative tolerance, not a recommended default.

observe:
  - name: bench
    command: python benchmark.py
    metrics:
      runtime_ms: lower
    capture:
      - results/report.json

budget:
  iterations: 3
```

Top-level keys: `goal`, `name`, `land`, `prompt_template`, `report`, `agent`,
`scope`, `gates`, `observe`, `stop`, `budget`. Keys of the first config format
(`constraints`, `metrics`, `observables`, `acceptance`, `reviewer`,
`agent.type`, `agent.timeout_seconds`, `budget.iteration_time`) are errors that
name their replacement; when editing an existing config, convert them.

## Config files

- A config is `autohelix.yaml` at the project root or `autohelix/<name>.yaml`;
  nowhere else. A bare `autohelix run` uses `autohelix.yaml`, else the only
  config in `autohelix/`.
- Runs are named after the file (`autohelix/perf.yaml` gives
  `20261112-1400-perf-0mws`; `autohelix.yaml` gives no name). There is no
  `name:` key.
- `prompt_template` is a path relative to the config's folder. Keep templates
  and helper scripts in subfolders (`autohelix/prompts/`), since every `.yaml`
  file directly in `autohelix/` is a config, and commit them: iteration copies
  are made from the commit.
- Keep `autohelix/` out of `scope.editable` (or list it under `frozen:`), so
  the agent can't edit its own gates.

## Goal, gates, and scope

- `goal` is the improvement requested of each agent iteration. Its first line is
  the run's title (keep it under about 80 characters, like a commit subject);
  put details on the following lines.
- `gates` lists everything that can reject an iteration, checked in order:
  - A command (a string, or a mapping with `command` and `timeout`) must exit 0.
  - `ask: <question>` is shorthand for `autohelix ask -- "<question>"`, a yes/no
    judgment by a read-only agent; `yes:` / `no:` keys (quoted or not) say what each
    answer means.
  - `metric: NAME` with `regress: N%` or `improve: N%` compares a number from an
    `observe` entry with the last accepted iteration (see below).
- Time values are durations: a bare number is seconds, a string uses `h`, `m`,
  `s` (`90s`, `30m`, `2h30m`). The default command timeout is 30 minutes. Make
  it appropriate for the actual evaluator; a timeout bounds execution but does
  not show that setup verification will be cheap.
- `scope.editable` allows paths/patterns; `scope.frozen` excludes them, also
  inside `editable` (both together mean editable minus frozen). Changes outside
  the scope are discarded before gates run, and the next agent is told. Without
  either, code changes are unrestricted by scope, and a run with gate or observe
  commands warns.
- Prefer relative paths. Copies and scope isolate repository changes, not
  host access: processes can still write to external files and use credentials
  available to the agent.
- Absolute agent/Python executable paths can select an environment. Source,
  evaluation inputs, and normal capture outputs should resolve in the copy;
  an absolute path to the original checkout can accidentally evaluate old code.
  Call `autohelix ask` / `autohelix review` by name: the harness puts its own
  launcher first on commands' `PATH`.

## Observe entries and acceptance

`observe` lists commands whose output the next agent reads and whose declared
metrics become the run's numbers. They run after the gate commands, top to
bottom. Every entry needs a unique `name` (lowercase letters, digits, `_`, `-`)
and exactly one of `command` or `review`.

A command entry takes:

- `command`: the evaluation or evidence-gathering command.
- `metrics`: optional mapping of metric names to `higher` or `lower`. Metric
  names are unique across entries.
- `capture`: optional list of files or folders (relative paths in the copy, not
  globs) to save with the output.
- `timeout`: a duration; defaults to 30 minutes.

Emit numbers on standard output as `##autohelix[runtime_ms=12.3]`. Names must
match `metrics`. A command can provide evidence without any metrics. Output is
stored in `.autohelix/runs/<id>/observations/iter-N/<name>/` (`stdout.txt`,
`stderr.txt`, captures).

A review entry is shorthand for `autohelix review`, a read-only LLM review of
exactly the candidate (its own fresh copy, with the complete change as
`.autohelix-iteration/candidate.diff`):

```yaml
observe:
  - name: review
    review: |
      Assess the candidate against the goal. Give concrete feedback supported
      by the supplied artifacts.
  - name: progress
    review: |
      For each TASKS.md item, give done / partial / missing with evidence.
      Report `missing`: the number of items that are not fully done.
    previous: true
    metrics:
      missing: lower
    timeout: 20m
```

- The prompt is the reviewer's whole request: it is not handed the goal or
  metrics, so include the context it needs.
- `previous: true` gives it its own output from the last accepted iteration as
  a starting point to re-verify (progress tracking).
- `metrics` makes the review report those numbers (structured output, printed
  as `##autohelix[...]` lines by the command). What a number means is defined
  only by the prompt. Review numbers are noisy and gameable: suitable for
  display and `stop`, not recommended for gates.
- `agent` on a review or `ask` entry takes only `backend` (`claude` or
  `codex`), `model`, `reasoning_effort`, `timeout`, `command`.

An observe entry that fails (non-zero exit, timeout, a review without a valid
answer) does not reject the iteration: the failure is recorded and shown, and
it contributes no metrics. Only gates reject. A command or reviewer that
changes the candidate's files does reject the iteration.

Without metric gates, passing gates can accept a change even if a measured
value regresses. A metric gate compares with the last accepted iteration (the
run's tip commit, or the baseline), not the best value:

- `regress: N%`: at most N% worse; `regress: 0%` means no worse.
- `improve: N%`: strictly better by at least N%; `improve: 0%` means strictly
  better. Use it for counts (`missing` 1 → 0).
- Thresholds are percent strings (`5%`); a bare number is an error.
- With a reference of 0, `regress` means no worse than 0 and `improve` any
  strict improvement.
- A missing (or non-finite) gated value rejects the iteration.
- Under `improve`, a noisy metric keeps lucky measurements and the loop
  stalls; make the benchmark stable and set thresholds above its noise.

Order within an iteration: gate commands (first failure rejects), then observe
entries in order, each followed by the metric gates on its numbers (a failure
rejects and skips the remaining entries), then acceptance and stop commands.
List reviews last. The baseline (iteration 0) runs every gate command and
observe entry without rejecting; a declared command metric missing there stops
the run as a setup error.

Capture preserves evidence. It does not restore or promote external artifacts
when code is rejected or accepted. Static inspection can check the expected
capture locations and output format; confirming produced values and files
requires execution under the entrypoint's verification policy.

## Agent

```yaml
agent:
  backend: claude
  # command: /path/to/agent-wrapper
  # model: your-preferred-model
  timeout: 30m
```

`agent.backend` selects `claude` (default), `codex`, or `opencode`. Optional
`command` and `model` select the executable and model; `reasoning_effort` the
effort. Respect the user's installed agent and preferences. `agent` is also the
default backend and model for `ask` and `review` entries; OpenCode has no
read-only mode, so with an OpenCode main agent each `ask`/`review` entry needs
its own `agent.backend`.

`agent.timeout` is a per-iteration hard kill (default: none); where the backend
supports it the agent also sees the remaining time. `ask` defaults to 600
seconds and a review to its entry's timeout, independently of `agent.timeout`.

## Stop conditions

`stop` lists commands, in the same forms as gate commands, that end the run
early. They run after the baseline and after each accepted iteration: exit 0
stops (the last stdout line is the recorded reason), 1 continues, anything else
is an error (fatal on the baseline, a warning later). `$AUTOHELIX_METRICS` is a
JSON file of the metrics just measured, so a target is
`jq -e '.speedup >= 10' "$AUTOHELIX_METRICS"`; `$AUTOHELIX_OBSERVATIONS` is a
copy of the accepted iteration's observations (one folder per entry name);
`$AUTOHELIX_ITERATION` is the iteration number. For a qualitative condition use
`autohelix ask -- "<yes/no question>"` (an agent judgment: exit 0 = yes, 1 = no,
2 = no valid answer; cost counted toward the run). Use one when the user states
when the run is done; keep the command and any criteria files outside
`scope.editable`. Budgets still apply.

## Budgets and run state

Omitting `budget.iterations` defaults to five attempts, including when `budget`
is omitted, empty, or YAML `null`. Optional `cost` and `time` may be omitted or
set to `null`, which means no configured cap from that field. Other limits, such
as `agent.timeout` or a command timeout, may still apply. Time values accept
seconds or durations such as `30m`.

A preparation-only template can leave those choices open:

```yaml
budget:
  # iterations: 5  # Omitted uses the runtime default five, not unlimited.
  cost: null       # No configured dollar cap; choose only with reliable reporting.
  time: null       # No configured loop-time cap.
```

Proposed values can stay in comments until selected; retain any stated/existing
budgets. `iterations` must be a positive integer when set; never `null`.

`budget.time` measures elapsed time in the iteration loop, including its gates
and observe entries; initial preflight and baseline work happen before that
timer. Total time/cost budgets are checked between iterations, so they can
overshoot rather than interrupt an iteration. Prior recorded usage counts on
resume, and both are checked before starting another iteration: an
already-exhausted cost or time budget starts no new attempt.

`agent.timeout` limits the main-agent call and provides remaining-time feedback,
not a deadline for the whole evaluation cycle. Gate and observe commands have
per-command timeouts.

`budget.cost` compares the accumulated cost of the optimization agent and of
model calls the harness's commands report (`ask` gates, reviews), including the
baseline's, with the requested dollar limit. History keeps each actual model
call in the `calls` list, including its role, usage and relative log path.
Claude Code and OpenCode can report dollar costs; Codex can provide calculated
API-equivalent estimates. Only rely on this optional budget when usable dollar
usage is recorded for the selected backend/configuration; missing cost is not
evidence of free usage. Report generation, model calls made by external
commands that don't record usage, and the user's supervising/setup agent are
separate. These boundaries make the budget a run-control threshold, not an
absolute all-inclusive financial cap.

Commit the config and any scripts it uses, then start with `autohelix run
[NAME] --path PROJECT -n 3` (`NAME` for `autohelix/NAME.yaml`, none for
`autohelix.yaml`): always a new run, of three iterations. A run keeps the config file it started with
(edit that file to change it). Continue one with `autohelix resume [RUN_ID]
--path PROJECT`: `--until N` runs up to iteration N (a total; repeating it is
harmless), `-n 3` runs three more. Either overrides only `budget.iterations`;
cost and time stay run totals. A bare `resume` reads the config a bare `run`
would; with several configs, name the run.

`autohelix archive RUN_ID` hides a run and stops a bare `resume` from picking
it, retaining its history and unlanded commits; `autohelix delete RUN_ID`
removes its history and its kept rejected trees (with `--force`, even unlanded
commits). Both leave code unchanged. Resuming, archiving, deleting, and
resetting code or external artifacts are different operations. Explain the
intended operation before changing state.

## Explain the generated configuration

For setup-only requests, display the complete generated YAML in the final
response before explaining it. Include the full goal and every configured
section, not just selected settings. Comments can clarify defaults or open
choices, but are not required. Then give a numbered walkthrough in the file's
section order, connecting each choice to its effect on the experiment.

After showing the complete YAML at the start of this reference, a walkthrough
could be:

1. **Goal:** Ask the agent to reduce runtime while preserving behavior. This
   guides its work; the gates below supply the configured correctness evidence.
   Edit the goal to change the requested improvement or behavioral requirements.
2. **Scope:** Only `src/solver.py` may retain changes. Keeping the tests and
   benchmark outside this scope preserves the evaluation. Widen the scope if
   the intended improvement requires other files; it does not restrict host access.
3. **Gates:** `pytest tests/` must exit successfully for a candidate to be
   accepted. These are the existing correctness checks. Add or change a gate
   when a requirement needs different evidence, after agreeing on that evaluator.
   The commented metric gate illustrates a possible regression tolerance; agree
   on a tolerance and measurement reliability before enabling it.
4. **Observe:** The `bench` entry tracks `runtime_ms`, with lower values
   preferred, and preserves `results/report.json` as evidence for the next
   agent. No metric gate is active, so passing tests can accept a slower
   candidate.
5. **Budget:** Three attempts are configured. Total-time, per-agent-time, and
   dollar limits are unset. Choose applicable limits before a requested launch;
   dollar limits depend on reliable backend reporting and do not cover all calls.

This example does not override the default Claude backend or configure a
review. Explain the actual backend/model choices and omitted defaults in the
user's configuration, and flag choices still open.

Finish with the actual validation and execution results, including verification
not performed, followed by commands to verify and launch later. Link the config
and any durable explanation as supporting detail.
