# Confidence intervals under skew

## Status

This is the baseline research scaffold. It establishes a reproducible
simulation but does not yet make a substantive empirical claim.

## Baseline method

For normal and exponential populations, the experiment repeatedly draws
samples of size 10 and 30. It evaluates a normal-theory interval and a
percentile bootstrap interval for the population mean, recording empirical
coverage and mean interval width. Both methods are evaluated on the same
sample within each trial.

The quick run uses a modest number of simulation trials and bootstrap
replicates. Its output in `results/latest.json` is suitable for debugging and
choosing the next experiment, but its Monte Carlo resolution must be considered
before drawing conclusions.

## Current interpretation

No conclusion has been established. The baseline results are starting evidence,
and any later claim should account for the quick run's Monte Carlo resolution.
