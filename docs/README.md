# AutoHelix Documentation

**Run agents with protocols, not conversations.**

One file sets the goal, the checks and what to observe. An AI agent iterates on
your code in private copies, and only changes that pass your gates are kept.

See the [main README](https://github.com/awslabs/AutoHelix/blob/main/README.md) for an overview and quick start.

## Guides

- **[Getting Started](getting-started.md)** — install, write a config (with your agent or by hand), run your first loop
- **[Patterns](patterns.md)** — a starting config for each kind of goal: a checked task, optimization, training a model, working through a spec, writing and research
- **[Concepts](concepts.md)** — how iteration, isolation, and verified improvement
  work, and how to design a loop
- **[Configuration](config.md)** — the full config surface (`autohelix/<name>/config.yaml`): gates, observe, stop, scope, agent (budgets are `run`/`resume` flags)
- **[CLI Reference](cli.md)** — skill installation/export, bare `autohelix` (the entry screen), `run` (and groups of runs), `resume`, `stop`, `land`, `archive`, `delete`, `view`, `wait`, `hint`, `ask`, `review`, `report`, `docs`
- **[Phases](phases.md)** — where each phase runs (agent, checks, judges, stop,
  summary), what it gets, and where its output goes
- **[ML Experiments](ml-experiments.md)** — patterns for training/eval loops (post-training, benchmarks)
- **[Docker Sandbox](docker.md)** — run the agent with filesystem isolation
