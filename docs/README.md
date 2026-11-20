# AutoHelix Documentation

**Autonomous iterative improvement harness.**

AutoHelix runs AI agents in a loop — each iteration is isolated in private copies of the code, validated by its gates, and only accepted when they pass. What doesn't pass gets discarded; what the agent learns persists.

See the [main README](https://github.com/awslabs/AutoHelix/blob/main/README.md) for an overview, quick start, and the config reference.

## Guides

- **[Getting Started](getting-started.md)** — install, write a config (with your agent or by hand), run your first loop
- **[Patterns](patterns.md)** — a starting config for each kind of goal: a checked task, optimization, training a model, working through a spec, writing and research
- **[Configuration](config.md)** — the full config surface (`autohelix/<name>/config.yaml`): gates, observe, stop, scope, agent (budgets are `run`/`resume` flags)
- **[CLI Reference](cli.md)** — skill installation/export, bare `autohelix` (the entry screen), `run` (and groups of runs), `resume`, `stop`, `land`, `archive`, `delete`, `view`, `wait`, `hint`, `ask`, `review`, `report`, `docs`
- **[Docker Sandbox](docker.md)** — run the agent with filesystem isolation
- **[Concepts](concepts.md)** — how iteration, isolation, and verified improvement
  work, and how to design a loop
- **[Phases](phases.md)** — where each phase runs (agent, checks, judges, stop,
  summary), what it gets, and where its output goes
- **[ML Experiments](ml-experiments.md)** — patterns for training/eval loops (post-training, benchmarks)
