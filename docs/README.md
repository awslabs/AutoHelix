# AutoHelix Documentation

**Autonomous iterative improvement harness.**

AutoHelix runs AI agents in a loop — each iteration is isolated in a git worktree, validated against constraints, and only merged when everything checks out. What doesn't pass gets discarded; what the agent learns persists.

See the [main README](../README.md) for an overview, quick start, and the config reference.

## Guides

- **[Getting Started](getting-started.md)** — install, initialize a project, run your first loop
- **[Configuration](config.md)** — the full `autohelix.yaml` surface
- **[CLI Reference](cli.md)** — `init`, `run`, `clear`, `watch`, `report`
- **[Docker Sandbox](docker.md)** — run the agent with filesystem isolation
- **[Concepts](concepts.md)** — how iteration, isolation, and verified descent work
- **[ML Experiments](ml-experiments.md)** — patterns for training/eval loops (post-training, benchmarks)
