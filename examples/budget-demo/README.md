# Budget control demo

Watch AutoHelix's budget caps stop a run — offline, in seconds, with no LLM and
no cost. This example uses the **mock agent**, a built-in test backend that makes
a trivial change each iteration and reports a *synthetic* cost and runtime. It
exists to exercise the budget machinery, not to optimize anything.

```bash
python examples/setup_example.py budget-demo --dir ./budget-demo
cd ./budget-demo
autohelix run
```

## What you'll see

The mock reports `$0.03` and 1s per iteration (set in `agent.settings`). With a
`$0.10` cost cap, cumulative cost reaches `$0.12 ≥ $0.10` after iteration 4, so
the loop stops there — well short of the 20-iteration cap:

```
iter 1      1s │ $0.03 │ ✓ merged
iter 2      1s │ $0.03 │ ✓ merged
iter 3      1s │ $0.03 │ ✓ merged
iter 4      1s │ $0.03 │ ✓ merged
Cost  $0.12
```

## The three caps

Budgets are OR'd — whichever is hit first stops the run:

| Cap | Config key | Meaning |
|-----|-----------|---------|
| Iterations | `budget.iterations` | Hard cap on iteration count |
| Cost | `budget.cost` | Stop once cumulative agent cost ≥ this (USD) |
| Time | `budget.time` | Stop once total wall-clock ≥ this |

Cost and time accumulate across `autohelix run` resumes (loaded from history), so
they bound the *whole* run, not a single invocation.

Edit the caps in `autohelix.yaml` to see a different one win:
- Drop `iterations` to `2` → the iteration cap fires first.
- Drop `time` to `2s` → with 1s/iter, the time cap fires around iteration 2.

## Why the mock

Cost can't be demonstrated with a real slow task — cost is only reported by an
actual LLM backend, and making something merely *slow* burns wall-clock, not
dollars. The mock reports a synthetic `cost_usd` so the cost cap can be shown
deterministically and for free. This demonstrates the *enforcement mechanism*;
the dollar figure is fabricated, not a real model bill.

For the per-iteration time budget (`budget.iteration_time`) and its graceful
"time's running out, wrap up and save notes" hook — which only a real agent can
exercise — see the `bin-packing` example.
