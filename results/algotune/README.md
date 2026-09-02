# AlgoTune Results

This directory contains the compact aggregate behind the AutoHelix AlgoTune
plot.

## Headline

- Model: Claude Opus 4.8
- Execution: single-core CPU with CUDA hidden
- Collection date: July 22, 2026
- Optimization budget: five iterations per task
- Tasks: 154
- Final median best-so-far speedup: **8.50×**
- Iterations: 770 total; 754 accepted

Median best-so-far progression:

```text
baseline  iter 1  iter 2  iter 3  iter 4  iter 5
1.00×     2.54×   4.01×   5.60×   7.59×   8.50×
```

![Median AlgoTune speedup progression](../../assets/progression_algotune.png)

## Methodology

Each task starts with a wrapper around its reference solver. AutoHelix may edit
only the solver implementation. Correctness is checked on seeded generated
problems, and speedup compares the candidate with the reference under the same
single-core, CPU-only evaluation setup.

These numbers use AutoHelix's evaluation setup, which is not identical to
AlgoTune's official harness and should not be compared directly with official
AlgoTune results.

All 154 tasks have five recorded iterations. For rejected iterations, the
progression plot carries forward the last accepted speedup, starting from
1.00× if no iteration has yet been accepted.

## Files

- `results.json` — per-task baseline, accepted-iteration count, and best-so-far
  speedup by iteration.
- `make_progression_plot.py` — regenerates the median progression figure.

To regenerate the plot:

```bash
python -m pip install matplotlib numpy
python results/algotune/make_progression_plot.py
```

Raw agent logs and task worktrees are not included because they are large and
are not needed to reproduce the aggregate figure from `results.json`.
