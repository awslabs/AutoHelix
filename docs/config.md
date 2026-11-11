# Configuration

All configuration lives in `autohelix.yaml` at the project root.

**Paths are relative to the project root.** Every path in the config — commands in `constraints` and `metrics`, `capture` files, and the patterns in `scope.editable`/`scope.frozen` — is resolved inside the per-iteration copy of the code, not the main repo. Use relative paths (`src/`, `tests/`, `results/log.json`); absolute paths point outside the copy and will either be silently ignored, revert all the agent's work as out-of-scope, or crash the run. (The one exception is long-lived outputs you deliberately keep outside the repo — e.g. a model checkpoint directory — which are meant to live at a fixed absolute path.) Untracked files such as a virtualenv or downloaded data aren't in the copy; see [Concepts → Candidate Copies](concepts.md#candidate-copies).

## Full Reference

```yaml
# What the agent should achieve
goal: |
  Be specific about what to optimize and success criteria.

# Optional run identity prefix and landing policy
# name: experiment      # lowercase letters/digits/hyphens, at most 32 characters
# land: auto            # default: land at segment end; manual retains only the run ref

# Must pass (exit 0) or iteration is rejected
constraints:
  - pytest tests/
  - python -m py_compile src/*.py

# What files should the agent modify? (pick one, not both)
scope:
  editable: [src/]      # allowlist: only these files get committed
  # frozen: [tests/]    # denylist: these files can't be modified

# Commands run after each iteration — full output captured for the agent.
# Add `values:` to extract scalars for terminal display/gating; capture artifacts too.
# Your script should print: ##autohelix[name=value] for scalar extraction.
# (`observables:` is an accepted alias — see the Metrics section below.)
metrics:
  - command: python benchmark.py
    values:                 # optional: scalars for terminal display
      speed: higher         # "higher" or "lower"
      latency: lower
    capture:                # optional: extra files to save per iteration
      - results/eval_log.json
    capture_stdout: true    # save stdout to history (default true)
    # timeout: 1800         # per-command timeout in seconds (default 1800)

# Optional: reject iterations where metrics regress
acceptance:
  metric_gates:
    - metric: speed
      max_regression_pct: 10

# Agent backend (optional - defaults to claude)
agent:
  type: claude          # claude (default), codex, opencode, mock
  # command: claude     # custom command path
  # model: claude-opus-4-8
  # reasoning_effort: medium  # low, medium, high, xhigh
  # timeout_seconds: 1800
  # auto_memory: false  # disabled by default; set true to enable Claude Code's built-in memory
  # settings: {}        # agent-specific settings
  # extra_args: []      # extra CLI arguments

# Optional: LLM reviewer after each iteration
reviewer:
  prompt: |
    Review the code for clarity, performance, and correctness.
    What specific change would have the most impact next?
  # model: claude-opus-4-8  # override model for reviewer
  # timeout_seconds: 300       # override timeout for reviewer
  # auto_memory: false         # disabled by default for independent reviews

# Optional: end the run early once one of these exits 0 (see Stop Conditions)
# stop:
#   - command: jq -e '.speedup >= 10' "$AUTOHELIX_METRICS"
#   - command: ./scripts/done.sh
#     timeout: 120
#   - autohelix ask "Is every item in SPEC.md implemented and tested?"

# Limit the run (any combination)
budget:
  iterations: 10       # max iterations
  # cost: 20           # cumulative agent + reviewer cost in USD (Codex uses an API-equivalent estimate)
  # time: 2h           # max total wall-clock (duration string: 90m, 2h, 1h30m)
  # iteration_time: 15m  # per-iteration time budget (see below)

# Write a summary of the run when it finishes (default true)
# report: false
```

## Run identity and landing

`name` is an optional readable part of newly generated run ids
(`20261111-1432-<name>-k3f9`); it does not select an existing run. It accepts lowercase letters, digits and hyphens, with
at most 32 characters. Without a name, nondefault config filename stems supply
the name. `land` is optional: `auto` (default) fast-forwards accepted code to the
starting branch when a run segment ends, while `manual` keeps it on the run ref
until `autohelix land`. CLI `--name` and `--land` override these values. A bare
`autohelix resume` picks the config path's most recently active run, or name one;
neither depends on those optional fields.

## Example Configs

### Performance Optimization

The most common use case — make code faster while keeping tests passing:

```yaml
goal: |
  Make sort_list faster. The current implementation is a slow bubble sort.
  Improve the algorithm or implementation to increase throughput.

constraints:
  - pytest test_sort.py

metrics:
  - command: python benchmark.py
    values:
      speed: higher

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

constraints:
  - python test_recipe.py

metrics:
  - command: python evaluate.py
    capture:
      - results/eval_log.json
      - results/per_task.jsonl
    values:
      avg_accuracy: higher

scope:
  editable: [recipe.py]
```

### Code Quality Refactor

Improve readability and maintainability without breaking anything:

```yaml
goal: |
  Refactor utils.py for clarity: better naming, Pythonic idioms,
  reduce duplication. Don't change external behavior.

constraints:
  - pytest tests/

scope:
  editable: [src/utils.py]

budget:
  iterations: 5
```

### With Reviewer and Metric Gates

Full setup with qualitative review and regression protection:

```yaml
goal: |
  Optimize the attention kernel for maximum TFLOPS on A100.

agent:
  type: claude
  model: claude-opus-4-8
  timeout_seconds: 1800
  # auto_memory: true  # opt in to Claude Code's built-in memory (off by default)

constraints:
  - python check_correctness.py
  - python -m py_compile kernel.py

metrics:
  - command: python benchmark.py
    values:
      tflops: higher
  - command: python measure_memory.py
    values:
      memory_mb: lower

acceptance:
  metric_gates:
    - metric: tflops
      max_regression_pct: 5

scope:
  editable: [kernel.py]

reviewer:
  prompt: |
    Review the Triton kernel for correctness, numerical stability,
    and performance. Suggest the single most impactful optimization.
  model: claude-opus-4-8
  auto_memory: false  # independent reviews (default)

budget:
  iterations: 20
```

### Minimal Config

The absolute minimum — just a goal and one constraint:

```yaml
goal: |
  Add type hints to all public functions in src/.

constraints:
  - python -m mypy src/ --strict
```

## Key Concepts

### Goal

A free-text description of what the agent should achieve. Be specific — this is the primary instruction the agent receives each iteration.

### Constraints

Shell commands that must exit 0. If any constraint fails, the iteration is rejected and changes are discarded. The agent sees the failure output in its next prompt.

### Metrics

A list of commands whose full output is captured for the agent, with optional scalar extraction for display and gating. Each command runs exactly once per iteration.

Add `values:` to a command to pull numbers out of its output — those numbers rank the run, feed metric gates, and show up in the terminal. A command with no `values:` still runs and its output is captured for the agent; it just doesn't produce a number. Because of that, this key is also accepted under the more precise name **`observables:`** (a command produces observations, some of which are scalar metrics) — use whichever name reads better for your config. They are the same thing.

**Fields:**

- `command` — shell command to run (required)
- `values` — scalars to extract for display/gating (optional). Each is `name: direction` where direction is `higher` or `lower`
- `capture` — list of file paths (relative to the project) to copy from the checks copy into the history directory (optional). Captured files are outputs; they are never published
- `capture_stdout` — save stdout to history (default `true`)
- `timeout` — seconds (default 1800)

**Scalar extraction formats:**

Structured (recommended) — your script prints:
```
##autohelix[speed=1234.5]
##autohelix[latency=0.003]
```

Heuristic fallback — AutoHelix also recognizes `name: value` and `name=value`.

A single command can report multiple values — declare them all under `values:`:

```yaml
metrics:
  - command: python benchmark.py
    values:
      throughput: higher
      latency: lower
```


**Output storage:**

All captured output lives in `.autohelix/runs/<id>/observations/iter-N/`:
- `stdout.txt` — command stdout+stderr (or `stdout-0.txt`, `stdout-1.txt` for multiple commands)
- Captured files are copied by basename

The agent can read these at any time from its copy.

### Scope

Controls what files the agent can modify:

- **`editable`** (allowlist): only matching files are committed. Everything else is reverted.
- **`frozen`** (denylist): matching files are reverted. Everything else is committed.
- Mutually exclusive — use one or the other.
- Out-of-scope changes are reverted *before* constraints run, so constraints test the actual state that would be accepted.

### Metric Gates

Optional regression guards. If a metric drops more than `max_regression_pct` from its best value, the iteration is rejected even if constraints pass.

### Reviewer

An optional second agent that reviews a change before acceptance into the run and writes qualitative feedback, archived to `.autohelix/runs/<id>/reviews/iter-N.md`. The latest review is included in the next iteration's prompt (materialized into the agent's copy as `review.md`). Its verdict is advisory. Ordinary reviewer errors do not reject an iteration; candidate edits by a reviewer do, because they invalidate its verdict.

The `reviewer.prompt` is the reviewer's entire instruction (it inspects the diff itself and is not handed the goal or metrics), so put any context it should consider there. The reviewer's copy omits the `reviews/` archive, but shared project state remains reachable; this is not access isolation. See [Concepts → Reviewer](concepts.md#reviewer) for the full mechanics.

### Stop Conditions

`stop:` lists commands that end the run early. Each entry takes the same forms as a constraint: a string, or `command` plus an optional `timeout` (seconds). They run after the baseline and after each **accepted** iteration, on the code that was just accepted (rejected iterations don't change it). They run in order until one exits:

- **0** — stop. The run ends after this iteration, and the last line the command printed to stdout is recorded as the reason (`speedup 10.3 ≥ 10`), so print one.
- **1** — keep going.
- **anything else, or a timeout** — an error. On the baseline it stops the run before the first iteration (fix the command and run again); later it prints a warning and the run continues.

Each command gets these environment variables (on top of the ones every command the harness runs gets: `AUTOHELIX_CONFIG`, `AUTOHELIX_USAGE_FILE`, `AUTOHELIX_LOG_DIR` and `AUTOHELIX_COMMAND_DEADLINE`):

- `AUTOHELIX_METRICS` — path to a JSON file with the metrics just measured, e.g. `{"speedup": 10.3}`. A metric target is a one-liner: `jq -e '.speedup >= 10' "$AUTOHELIX_METRICS"` (`jq -e` exits 0 when the expression is true, 1 when false).
- `AUTOHELIX_ITERATION` — the iteration number (0 for the baseline).

For a qualitative condition, use [`autohelix ask`](cli.md#autohelix-ask): it asks an agent a yes/no question and exits 0 for yes, e.g. `autohelix ask "Is every item in SPEC.md implemented and tested?"`. Its cost counts toward the run.

They run in a fresh copy of the accepted code (HEAD at the accepted commit), after iterations and on the baseline. Keep stop commands out of `scope.editable`, and don't let them modify files: a change is reported as an error (the acceptance stays recorded), and on the baseline it stops the run. The agent's prompt lists them.

Stop conditions only end a run early: budgets still apply. If the baseline already meets one, no iterations run. If you interrupt a run while its stop commands are running, the iteration is kept but not checked; the next accepted iteration is. A stop pauses the run: `autohelix resume` continues it (and it stops again after the next accepted iteration if the condition still holds); raise the target or remove the condition first.

Why a run ended is shown at the end of the run, in `autohelix view`, `autohelix watch`, the run summary, and `.autohelix/runs/<id>/runtime/status.json` (`stop_reason`).

### Budget

Caps how far a run goes. Combine any of:

- `iterations` — maximum number of iterations (default 5).
- `cost` — cumulative cost threshold in USD for the optimization agent,
  reviewer, and model calls reported by commands, including baseline commands
  and reviews. Uses backend-reported amounts
  for Claude Code and OpenCode, and calculated API-equivalent estimates for Codex.
  The same budget option applies to all three backends.
- `time` — maximum total wall-clock, as a duration string (`90m`, `2h`, `1h30m`).
- `iteration_time` — a *per-iteration* time budget. Unlike the others it can interrupt work mid-iteration: it enforces a hard deadline **and** injects the remaining time into the agent's context so it can wrap up and save notes first. See [ML Experiments](ml-experiments.md) for when this matters.

`cost` and `time` are **checked between iterations, never mid-iteration** — the loop stops once the cumulative total crosses the limit, so a run can overshoot by up to one iteration's worth of cost or time. For a hard mid-iteration cutoff use `iteration_time` (or `agent.timeout_seconds`).

Both are also checked before starting an iteration, including after the
baseline review and when resuming a run: a run past its budget stops at once
(`resume -n` extends only the iteration budget; raise `cost` or `time` here).

### Report

When a run finishes after at least one iteration (a stop condition is met, or the iteration, cost or time budget is reached), one more agent call reads the run's history, notes, reviews, logs and commits and writes a short summary to `.autohelix/runs/<id>/output/report.md`: what was achieved, which changes mattered, what didn't work, what to try next, and anything worth checking before relying on the results. An interrupted run gets no summary; run [`autohelix report`](cli.md#autohelix-report) to write one. Set `report: false` to skip it. The call uses the same agent backend and model as the run, with a timeout of `agent.timeout_seconds` capped at 15 minutes. Its time and cost are shown next to the summary in `autohelix view` and `report.html`; the cost is not counted against `budget.cost`.

### Auto Memory

Controls Claude Code's built-in memory system:

- **Agent**: defaults to off for reproducible, self-contained iterations
- **Reviewer**: defaults to off for independent reviews
- Both disable via `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`; set `auto_memory: true` to opt back in
- Ignored for non-Claude backends

### Cost Tracking

Available token usage and cost are captured from agent backends and shown:

- Per iteration (after accepted/rejected). A `+` after the amount (`$0.10+`) means part of that iteration's cost (agent or reviewer) is unavailable, so the amount is what is known.
- Cumulative in the final summary
- Persisted in `.autohelix/runs/<id>/history.jsonl`

Iteration costs include the optimization agent, the reviewer, and model calls
made by the commands the harness runs (such as `autohelix ask` in `stop:`,
`constraints:` or `metrics:`). Run totals and `budget.cost` also include
baseline reviews and the baseline's commands; report generation remains separate.
History's `calls` list records each attempted agent, reviewer and command judge
with its role, usage and log path. The older `usage`, `reviewer_usage` and
`command_usage` fields remain readable for compatibility.

A command of your own can report its model calls the same way: append one JSON
line per call, e.g. `{"id": "...", "source": "my-eval", "cost_usd": 0.12}`, to the
file named by `$AUTOHELIX_USAGE_FILE` (set for every command the harness runs).
Lines with the same `id` are one call (the last line wins), so a call can be
recorded with `"cost_usd": null` when it starts and its cost when it ends.
Interrupted or failed actual main-agent attempts are recorded as rejected
iterations, including reviewer/command calls and any available usage. Missing
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
sessions, an unambiguous explicit `agent.model` or `agent.settings.model` can
provide the model instead.

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
