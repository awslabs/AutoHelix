# AutoHelix Documentation

**Autonomous iterative improvement harness.**

AutoHelix runs AI agents in a loop — each iteration is isolated in private copies of the code, validated by its gates, and only accepted when they pass. What doesn't pass gets discarded; what the agent learns persists.

See the [main README](../README.md) for an overview, quick start, and the config reference.

## Guides

- **[Getting Started](getting-started.md)** — install, write a config (with your agent or by hand), run your first loop
- **[Configuration](config.md)** — the full config surface (`autohelix.yaml`, `autohelix/<name>.yaml`): gates, observe, stop, scope, agent, budget
- **[CLI Reference](cli.md)** — skill installation/export, `setup`, `run` (and groups of runs), `resume`, `land`, `archive`, `delete`, `view`, `dashboard`, `hint`, `ask`, `review`, `report`
- **[Docker Sandbox](docker.md)** — run the agent with filesystem isolation
- **[Concepts](concepts.md)** — how iteration, isolation, and verified improvement
  work, and how to design a loop
- **[ML Experiments](ml-experiments.md)** — patterns for training/eval loops (post-training, benchmarks)
