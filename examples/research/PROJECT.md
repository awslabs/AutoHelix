# Research project: confidence intervals under skew

## Research question

How reliably do common 95% confidence intervals cover a population mean at
small sample sizes, and how does skew change the comparison?

The starter experiment compares a normal-theory interval with a percentile
bootstrap interval on normal and exponential data. This is a starting point,
not a prescribed final method or conclusion.

## Deliverables

- `experiment.py`: deterministic, CPU-only simulation code
- `results/latest.json`: machine-readable results from the current experiment
- `REPORT.md`: methods, evidence, limitations, conclusions, and open questions

## Research standards

- Separate observations from conclusions.
- Report uncertainty or simulation resolution where it matters.
- Preserve useful negative results.
- Prefer paired or otherwise controlled comparisons.
- Treat one iteration as one bounded research advance, not the whole project.
- Keep the quick experiment reproducible and under 20 seconds on a laptop.
- Do not optimize toward a predetermined answer.
