# Nested AutoHelix

This example uses an outer AutoHelix run to optimize a bounded configuration
template for fresh inner AutoHelix runs:

```text
outer AutoHelix: goal template + iteration handoff rubric + loop cadence
  -> fresh inner AutoHelix runs on several AlgoTune tasks
       -> validated implementation speedups
```

The outer loop may change how an inner run is instructed, how many iterations
divide a fixed total budget, and what each iteration records for the next one.
It cannot change task checks, metrics, editable scope, agent settings, or the
total inner budget. By default, each task gets four minutes and a $0.50 agent
cost cap.

## Setup

Install the example dependencies, then fetch and scaffold the small default
AlgoTune suite:

```bash
source .venv/bin/activate
python -m pip install -e ".[examples]"
python examples/nested-autohelix/setup.py --dir /tmp/nested-autohelix
cd /tmp/nested-autohelix
autohelix run
```

The setup fetches two development tasks and one transfer task from the public
AlgoTune repository. The resulting directory is self-contained and committed
as a new git repository.

Each outer evaluation launches fresh model-backed inner runs and incurs their
cost in addition to the outer agent cost shown by AutoHelix. The nested cost is
reported as the outer `cost` metric. The default outer run uses three
iterations.

To exercise project creation and report aggregation without model calls:

```bash
NESTED_AUTOHELIX_AGENT=mock python evaluate.py
```

After the outer run, evaluate the selected template once on the transfer task:

```bash
python evaluate.py --holdout --report results/holdout_report.json
```

The task suite is inspectable and intended to teach the pattern, not to serve as
a secure benchmark.

## Optimized Surface

The outer agent may edit:

- `inner-template.yaml`: the task-agnostic goal template and inner iteration
  count.
- `rubric.md`: instructions for the notes passed between iterations of one
  inner run.

Generated notes never cross task boundaries. `results/report.json` captures
per-task histories, notes, accepted diffs, failures, validity, token usage,
wall time, and cost so the outer agent can diagnose outcomes without exposing
all of them as headline metrics.
