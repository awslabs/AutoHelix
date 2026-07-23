# Configuration

All configuration lives in `autohelix.yaml` at the project root.

**Paths are relative to the project root.** Every path in the config — commands in `constraints` and `metrics`, `capture` files, and the patterns in `scope.editable`/`scope.frozen` — is resolved inside the per-iteration git worktree, not the main repo. Use relative paths (`src/`, `tests/`, `results/log.json`); absolute paths point outside the worktree and will either be silently ignored, revert all the agent's work as out-of-scope, or crash the run. (The one exception is long-lived outputs you deliberately keep outside the repo — e.g. a model checkpoint directory — which are meant to live at a fixed absolute path.)

## Full Reference

```yaml
# What the agent should achieve
goal: |
  Be specific about what to optimize and success criteria.

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

# Limit the run (any combination)
budget:
  iterations: 10       # max iterations
  # cost: 20           # max spend in USD (needs a cost-reporting backend: Claude Code or OpenCode)
  # time: 2h           # max total wall-clock (duration string: 90m, 2h, 1h30m)
  # iteration_time: 15m  # per-iteration time budget (see below)
```

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
- `capture` — list of file paths (relative to worktree) to copy into the history directory (optional)
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

All captured output lives in `.autohelix/observations/iter-N/`:
- `stdout.txt` — command stdout+stderr (or `stdout-0.txt`, `stdout-1.txt` for multiple commands)
- Captured files are copied by basename

The agent can read these at any time from the worktree.

### Scope

Controls what files the agent can modify:

- **`editable`** (allowlist): only matching files are committed. Everything else is reverted.
- **`frozen`** (denylist): matching files are reverted. Everything else is committed.
- Mutually exclusive — use one or the other.
- Out-of-scope changes are reverted *before* constraints run, so constraints test the actual state that would be merged.

### Metric Gates

Optional regression guards. If a metric drops more than `max_regression_pct` from its best value, the iteration is rejected even if constraints pass.

### Reviewer

An optional second agent that reviews a change before it merges and writes qualitative feedback, archived to `.autohelix/reviews/iter-N.md`. The latest review is included in the next iteration's prompt (materialized into the worktree as `review.md`). It is advisory — it never rejects an iteration; only constraints and metric gates decide what merges.

The `reviewer.prompt` is the reviewer's entire instruction (it inspects the diff itself and is not handed the goal or metrics), so put any context it should consider there. The reviewer never sees the `reviews/` archive, so each review is independent by construction. See [Concepts → Reviewer](concepts.md#reviewer) for the full mechanics.

### Budget

Caps how far a run goes. Combine any of:

- `iterations` — maximum number of iterations (default 5).
- `cost` — maximum spend in USD. Requires a backend that reports cost (Claude Code, OpenCode); ignored otherwise.
- `time` — maximum total wall-clock, as a duration string (`90m`, `2h`, `1h30m`).
- `iteration_time` — a *per-iteration* time budget. Unlike the others it can interrupt work mid-iteration: it enforces a hard deadline **and** injects the remaining time into the agent's context so it can wrap up and save notes first. See [ML Experiments](ml-experiments.md) for when this matters.

`cost` and `time` are **checked after each iteration completes, never mid-iteration** — the loop stops once the cumulative total crosses the limit, so a run can overshoot by up to one iteration's worth of cost or time. For a hard mid-iteration cutoff use `iteration_time` (or `agent.timeout_seconds`).

### Auto Memory

Controls Claude Code's built-in memory system:

- **Agent**: defaults to off for reproducible, self-contained iterations
- **Reviewer**: defaults to off for independent reviews
- Both disable via `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`; set `auto_memory: true` to opt back in
- Ignored for non-Claude backends

### Cost Tracking

Token usage and cost are captured from all agent backends and shown:

- Per iteration (after accepted/rejected)
- Cumulative in the final summary
- Persisted in `.autohelix/history.jsonl`
