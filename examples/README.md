# Examples

Copy-and-run AutoHelix projects. Set one up with `setup_example.py`:

```bash
python examples/setup_example.py --list              # see all available examples
python examples/setup_example.py sorting --dir ./my-sort
cd ./my-sort
autohelix run -n 5
```

## Bundled Examples (laptop, no setup)

| Example | Pattern | What it shows |
|---------|---------|---------------|
| **sorting** | Performance optimization | Metrics-driven iteration, scope enforcement |
| **ml-recipe** | Config/hyperparameter tuning | YAML knob-turning, eval pipeline |
| **writing** | Reviewer-driven quality | LLM reviewer, qualitative feedback loop |
| **bin-packing** | Open-ended optimization | Per-iteration time budget + the time-left hook; frozen scorer |
| **budget-demo** | Budget controls | Cost/time/iteration caps stopping the loop (mock agent, no LLM) |

Each is a directory with an `autohelix.yaml`. `setup_example.py` copies it,
git-inits it, and prints run instructions. That's it — `autohelix run` handles
the rest.

## Reference Example (GPU, heavy setup)

**posttrain/** is a reference example showing LLM post-training: the agent
writes training code to fine-tune `Qwen3-4B-Base` on GSM8K. Unlike the bundled
examples it is *not* copy-and-run — it needs a prepared GPU environment (vLLM,
transformers, etc.). See its [README](posttrain/README.md) for setup.

## AlgoTune Tasks (fetch from an upstream suite)

**algotune/** scaffolds any of the 154 [AlgoTune](https://github.com/oripress/AlgoTune)
algorithmic-optimization tasks. Unlike the bundled examples it *fetches* the task
(from GitHub or a local clone) rather than ship a ready-to-run project, so it has
its own `setup.py`:

```bash
python examples/algotune/setup.py --list          # list all 154 tasks
python examples/algotune/setup.py kmeans
```

Run the setup script from wherever you want the project to live — **not** inside
this repo, since the scaffolded project is its own git repo.
