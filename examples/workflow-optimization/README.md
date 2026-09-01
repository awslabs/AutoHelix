# LLM workflow optimization

This example places AutoHelix around a separate LLM workflow. The inner
workflow reads an order-support request, selects policy context, asks a model
for a structured decision, validates it, and executes it in a deterministic
simulator.

The support task is deliberately small. It illustrates a broader pattern:

1. run an LLM system on a frozen workload;
2. capture quality metrics and per-case execution traces;
3. let AutoHelix edit the workflow's tunable surfaces; and
4. evaluate each revision on the same workload.

The optimization surface is intentionally compact:

- `workflow.py` - context selection, prompt assembly, validation, and retries;
- `prompt.md` - the task prompt; and
- `SKILL.md` - reusable instructions supplied to the inner model.

Here, `prompt.md` and `SKILL.md` belong to the inner workflow. The outer
optimization agent still receives AutoHelix's prompt from
`.autohelix/prompt.md`.

`benchmark.py` freezes the workload, policies, simulator, provider adapter, and
evaluator. The baseline router deliberately omits a broadly applicable policy,
leaving a general improvement for AutoHelix to discover from failed-case
traces. `workflow.py` remains editable because real workflow optimization often
needs more than prompt changes.

## Run it

```bash
python examples/setup_example.py workflow-optimization --dir ./workflow-run
cd ./workflow-run
python benchmark.py --mock
autohelix run
```

The mock command is a free smoke test. A real evaluation contains six inner
model calls. The default two-iteration run evaluates the baseline and two
candidates, for at least 18 calls total.

By default, the inner workflow invokes the user's configured Claude CLI model
as a one-shot completion with tools and session persistence disabled. The CLI
is only the model transport; it cannot use tools to inspect or modify the
project. The outer AutoHelix agent is configured independently.

To request a specific model from Claude CLI:

```bash
export WORKFLOW_MODEL_NAME=sonnet
```

Managed CLI settings may resolve an alias to a different model. The detailed
report records the actual model reported by Claude when that information is
available.

## Evidence

AutoHelix tracks `task_success_rate` and average model tokens per case. Tokens
are a more reproducible efficiency signal than provider-reported dollars,
which can change with prompt-cache state. `results/report.json` also retains
validity, unsafe actions, model calls, provider-reported cost, actual model
names, latency, and the complete execution trace for every case.

The included cases are an inspectable teaching workload, not a secure holdout.
For a real system, optimize on a development workload and evaluate the selected
workflow once on cases the outer agent could not inspect.

## Other model providers

Set `WORKFLOW_MODEL_COMMAND` to any command that reads a prompt from stdin and
writes only the completion text to stdout:

```bash
export WORKFLOW_MODEL_COMMAND="/path/to/my-model-wrapper"
```

That wrapper can call Bedrock, a hosted API, or a local model. Keeping this
adapter command-based avoids requiring one cloud account or SDK for the
example. Custom commands do not report model identity, token counts, or cost
unless the adapter in `benchmark.py` is extended.
