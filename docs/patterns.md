# Patterns

Starting points, one for each kind of goal. Pick by two questions: **who
checks the result** (a script, or an agent reading it) and **what counts**
(pass or fail, or better or worse).

| | Pass or fail | Better or worse |
|---|---|---|
| **Checked by a script** | [A checked task](#a-checked-task) | [Optimization](#optimization), [Training a model](#training-a-model) |
| **Checked by an agent** | [Working through a spec](#working-through-a-spec) | [Writing and research](#writing-and-research) |

Each pattern has a config to adapt, the command that launches it and why, and
variants. The commands and files in the configs are placeholders for your
project's own: a gate that doesn't exist yet has to be written first. Every key
is in the [configuration reference](config.md); budgets and landing are
`autohelix run` flags ([budgets](cli.md#budgets), [`--land`](cli.md#options)),
not config. Each config is an
[experiment folder](config.md#experiment-folder), `autohelix/NAME/autohelix.yaml`,
which can also hold the experiment's own scripts. The bundled agent skill
(bare [`autohelix`](cli.md#autohelix)) starts from these patterns too.

## A checked task

One concrete task the tests can decide, or nearly: fix a bug that has a
reproducer, make a failing test pass, add a small feature with its tests.

`autohelix/fix/autohelix.yaml`:

```yaml
goal: |
  Fix parse_date so tests/test_dates.py::test_timezone_offset passes.
  Don't change other behavior.
scope:
  editable: [src/dates.py]
gates:
  - python -m pytest tests/
  - ask: Does the change fix the cause, rather than special-case the test's input?
agent:
  cli: claude-code
```

Launch: `autohelix run fix -n 1`. One checked attempt: the gates decide the
task, so there is nothing for a second iteration to improve, and the change
lands on your branch when the command ends. If the attempt is rejected,
`autohelix resume <id> -n 2` gives it two more, each with the rejected change
and the failing gate's output.

- **Gate on the whole suite**, not only the target test, so the change can't
  break something else.
- **An `ask:` gate for what the tests can't judge**: a read-only agent's yes or
  no, one model call per iteration (the baseline's included, so a `-n 1` run
  makes two). A one-iteration run has no later review,
  and it lands by itself, so this gate is the only agent check before it does.
  Drop it when the tests decide everything (a proof the checker accepts).
- **No observe entry and no review**: a review's feedback only reaches a next
  iteration, which a one-iteration run doesn't have.

Variant: a small feature. The `ask:` gate checks that the change does what the
goal describes:

```yaml
goal: |
  Add a --json flag to `report` that prints the same fields as the table.
scope:
  editable: [src/report.py, tests/test_report.py]
gates:
  - python -m pytest tests/
  - ask: |
      Does the change implement the --json flag as the goal describes, with
      tests that check its output?
agent:
  cli: claude-code
```

## Optimization

Make a number better while the tests keep passing: speed, memory, an agent
workflow's cost.

```
autohelix/perf/
  autohelix.yaml
  bench.py        # runs the benchmark, prints the metric
```

`autohelix/perf/autohelix.yaml`:

```yaml
goal: |
  Make the solver faster on the benchmark cases.
  Keep its results identical; don't add dependencies.
scope:
  editable: [src/solver.py]
gates:
  - python -m pytest tests/test_solver.py
observe:
  - name: bench
    command: python "$AUTOHELIX_EXPERIMENT/bench.py"
    metrics: {runtime_ms: lower}
    capture: [benchmark.json]
agent:
  cli: claude-code
```

Launch: `autohelix run perf -n 3`. Three iterations are a first look at
whether the number moves; `autohelix resume <id> -n 10` continues. Tests and a
measurement check a speedup well enough to land it automatically.

- **A metric adapter.** AutoHelix reads a metric only from a
  `##autohelix[runtime_ms=12.3]` line on stdout, which few tools print. A
  small script in the experiment folder runs the tool and prints that line:

  ```python
  # autohelix/perf/bench.py
  import json, subprocess
  subprocess.run(["python", "benchmark.py", "--out", "benchmark.json"], check=True)
  median = json.load(open("benchmark.json"))["median_ms"]
  print(f"##autohelix[runtime_ms={median}]")
  ```

  The run stores it with the experiment, and commands run it from their own
  copy (`$AUTOHELIX_EXPERIMENT`), so the agent can read it but not change it.
  Running it once during setup is the cheap check that the metric prints.
- **The tests and the benchmark stay outside `scope.editable`**, so the agent
  can't change what it is measured by.
- **`capture`** keeps the detailed results with the iteration, for the next
  agent and for you.
- **No metric gate at first**: the agent sees every number and decides. A
  slower candidate that passes the tests is accepted.

Variants:

- **Reject regressions** once you know how noisy the measurement is (repeat
  it inside the benchmark if needed):

  ```yaml
  gates:
    - python -m pytest tests/test_solver.py
    - metric: runtime_ms
      regress: 5%
  ```

- **Stop at a target** instead of a fixed number of iterations:

  ```yaml
  stop:
    - jq -e '.runtime_ms <= 50' "$AUTOHELIX_METRICS"
  ```

## Training a model

An optimization where the number is an evaluation score and each attempt
trains a model. The hard part is the model itself: it isn't code, so git
doesn't roll it back when an iteration is rejected.

When training is cheap enough to repeat in every evaluation, keep the model
out of it: the agent changes the recipe, and an observe entry trains from the
committed recipe and evaluates. The commit is the model. (The
[ml-recipe example](https://github.com/awslabs/AutoHelix/tree/main/examples/ml-recipe) works this way.)

`autohelix/train/autohelix.yaml`:

```yaml
goal: |
  Improve validation accuracy by tuning train_config.yaml: preprocessing,
  model choice and hyperparameters.
scope:
  editable: [train_config.yaml]
gates:
  - python -m pytest tests/
observe:
  - name: pipeline
    command: bash run_pipeline.sh
    metrics: {accuracy: higher}
    capture: [output/eval_results.json]
agent:
  cli: claude-code
```

Launch: `autohelix run train -n 3`. A first look at whether the score moves;
`resume` continues.

When the agent trains (long training, a recipe it debugs as it goes), the
model goes to a folder outside the project, **one per run and iteration**, so a
rejected iteration's model never replaces the last accepted one:

```
autohelix/train/
  autohelix.yaml
  paths.sh        # $out: this iteration's model folder; $start: the last accepted one's
  eval.sh         # evaluates $out (the base model at the baseline)
```

`paths.sh` is the recipe in
[ML experiments: One folder per iteration](ml-experiments.md#one-folder-per-iteration);
the agent sources it from its copy of the folder, commands from theirs. An
agent's shell commands don't share variables, so every command that uses
`$out` or `$start` sources it first:

```yaml
goal: |
  Improve validation accuracy by changing the training code in train.py.
  Train in the foreground, in one command:
  source .autohelix-iteration/experiment/paths.sh && rm -rf "$out" &&
  python train.py --start "$start" --out "$out"
  (train.py starts from the base model when "$start" doesn't exist). Start
  any other command that uses $out or $start with the same `source`.
scope:
  editable: [train.py]
gates:
  - python -m pytest tests/
observe:
  - name: eval
    command: bash "$AUTOHELIX_EXPERIMENT/eval.sh"
    metrics: {accuracy: higher}
agent:
  cli: claude-code
```

Launch: `autohelix run train -n 3 --time 8h`. `--time` bounds the segment
when each iteration trains for a long time.

- **What it gives**: rollback. The next agent starts from the last accepted
  model, and every accepted model stays on disk.
- **What it doesn't**: cleanup (nothing removes the folders; delete them
  yourself), and accepting a retrain that changes no code as an improvement
  (see [ML experiments](ml-experiments.md#one-folder-per-iteration)).
- **Long training**: offer `agent.timeout` (a hard limit that also tells the
  agent how much time is left) besides `--time`;
  [ML experiments](ml-experiments.md) has the environment and run discipline
  (pre-installed dependencies, cached data, training in the foreground).

## Working through a spec

A list of things to implement, one per iteration, with an independent agent
tracking what is left: a PRD, a task list, a set of review findings.

`autohelix/tasks/autohelix.yaml`:

```yaml
goal: |
  Implement every item in TASKS.md.
  Each iteration, take the highest-priority unfinished item, implement only
  that one, and add tests for it.
scope:
  editable: [src/, tests/]
  frozen: [TASKS.md]
gates:
  - python -m pytest -q
observe:
  - name: progress
    review: |
      For each TASKS.md item, give done / partial / missing with file:line
      evidence. Report `missing`: the number of items that are not fully done.
    previous: true
    metrics: {missing: lower}
stop:
  - jq -e '.missing == 0' "$AUTOHELIX_METRICS"
agent:
  cli: claude-code
```

Launch: `autohelix run tasks -n 10 --land manual`.

- **`-n 10`**: room for the whole list; the stop condition ends the run sooner
  once the review counts nothing missing.
- **No `--cost` unless you want one**: every iteration includes a review, a
  second model call, so `--cost 20` is worth offering.
- **`--land manual`**: changes across many files are worth reading before they
  reach your branch (`autohelix land <id>` when you're satisfied).

The list is frozen, so the agent can't mark its own work done. The review
writes the status, and `previous: true` gives it its own last accepted status
to re-check: the count moves only with accepted iterations. `missing` is an
agent's answer, so it is used for the stop condition and for display, not as a
gate. The stop command needs `jq` on your `PATH`. See
[progress tracking](concepts.md#progress-tracking).

Variant: when a script can count what is left (failing acceptance tests,
unchecked boxes), the observe entry is a command that prints
`##autohelix[missing=N]`, and no review is needed.

## Writing and research

Make a text or an investigation better where judgment decides what better is:
a guide, a report, an experiment and its write-up.

`autohelix/guide/autohelix.yaml`:

```yaml
goal: |
  Make docs/guide.md usable by a first-time API user.
  Keep it accurate against api-spec.json and include a complete introductory
  example.
scope:
  editable: [docs/guide.md]
gates:
  - python check_links.py docs/guide.md
  - ask: Does every statement in docs/guide.md agree with api-spec.json?
observe:
  - name: review
    review: |
      Read docs/guide.md and api-spec.json as a first-time API user would.
      Name the passages that are unclear or incomplete, and the single change
      that would help most next.
agent:
  cli: claude-code
```

Launch: `autohelix run guide -n 3 --land manual`. The review's feedback
reaches the next iteration, so the run needs several; you read the result
before it lands.

- **Gates for what must hold**: a script for what it can check (links,
  required sections), an `ask:` gate for a yes-or-no judgment (accuracy).
- **A review for what could be better**: its text goes to the next agent; it
  never rejects.
- **No numeric score is needed.** A review can report one (`##autohelix`
  lines in its output), but an agent's score is noisy: show it, don't gate on
  it.

Variant: research. The question and the evidence standard are fixed, the
experiment and the report evolve, and negative results stay in:

```yaml
goal: |
  Advance the research question in PROJECT.md with reproducible evidence.
  Keep experiments, results and REPORT.md consistent. Keep negative findings,
  and state uncertainty and limitations.
scope:
  editable: [experiment.py, results/, REPORT.md]
gates:
  - python validate.py
observe:
  - name: summary
    command: python summarize_results.py
    capture: [results/latest.json]
  - name: review
    review: |
      Review PROJECT.md, experiment.py, results/latest.json and REPORT.md.
      Do the claims follow from the evidence? Are uncertainty and negative
      findings stated? Inspect the artifacts; don't rerun experiments.
agent:
  cli: claude-code
```

`validate.py` checks what a script can (the results' schema, recorded seeds
and parameters, every number in the report present in the results); it can't
establish that the science is sound, which is the review's job. If the
experiments call models or remote services, cap them in those commands:
AutoHelix doesn't count what they spend.

## Comparing approaches

To find out which model, prompt or setup does better, start separate runs of
the same goal with `--land manual`, compare them in
[`autohelix view`](cli.md#autohelix-view), land the winner and archive the
rest. A [group](cli.md#groups) is the opposite: its runs read each other's
notes and code to build on each other, so they don't measure their approaches
apart.

## Examples

The repository's [examples](https://github.com/awslabs/AutoHelix/tree/main/examples) are complete, runnable projects for
these patterns: [sorting](https://github.com/awslabs/AutoHelix/tree/main/examples/sorting) (optimization),
[task-queue](https://github.com/awslabs/AutoHelix/tree/main/examples/task-queue) (working through a spec),
[writing](https://github.com/awslabs/AutoHelix/tree/main/examples/writing) and [research](https://github.com/awslabs/AutoHelix/tree/main/examples/research) (writing
and research), [ml-recipe](https://github.com/awslabs/AutoHelix/tree/main/examples/ml-recipe) (training a model).
