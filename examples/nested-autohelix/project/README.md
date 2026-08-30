# Nested AutoHelix Run

This project optimizes a reusable template for fresh AutoHelix runs over a
small AlgoTune development suite.

The outer agent may edit:

- `inner-template.yaml`: task-agnostic goal instructions and iteration count.
- `rubric.md`: the handoff notes requested between iterations of one task.

The evaluator freezes each task's implementation scope, correctness check,
speed metric, model, and total budget. Generated notes and optimized code are
discarded before the next task. The default per-task budget is four minutes
with a $0.50 agent cost cap.

```bash
python evaluate.py --check
autohelix run
```

Each evaluation launches model-backed inner runs. `cost` is their combined
reported cost; it is separate from the outer-agent cost shown by AutoHelix.
The scored speedup is re-measured from each persisted inner repository after
its AutoHelix run completes.

Run the transfer task only after selecting a template:

```bash
python evaluate.py --holdout --report results/holdout_report.json
```
