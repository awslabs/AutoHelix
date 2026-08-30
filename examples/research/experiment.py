#!/usr/bin/env python3
"""Reproducible CPU simulation for the research example."""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from pathlib import Path
from typing import Callable


SEED = 20250814
Z_975 = 1.959963984540054


def percentile(values: list[float], probability: float) -> float:
    """Linearly interpolate a percentile from a non-empty sample."""
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def normal_theory_interval(
    sample: list[float],
    _rng: random.Random,
    _bootstrap_replicates: int,
) -> tuple[float, float]:
    mean = statistics.fmean(sample)
    standard_error = statistics.stdev(sample) / math.sqrt(len(sample))
    margin = Z_975 * standard_error
    return mean - margin, mean + margin


def percentile_bootstrap_interval(
    sample: list[float],
    rng: random.Random,
    bootstrap_replicates: int,
) -> tuple[float, float]:
    means = [
        statistics.fmean(rng.choices(sample, k=len(sample)))
        for _ in range(bootstrap_replicates)
    ]
    return percentile(means, 0.025), percentile(means, 0.975)


METHODS: dict[
    str,
    Callable[[list[float], random.Random, int], tuple[float, float]],
] = {
    "normal_theory": normal_theory_interval,
    "percentile_bootstrap": percentile_bootstrap_interval,
}


def draw_sample(
    distribution: str,
    size: int,
    rng: random.Random,
) -> tuple[list[float], float]:
    if distribution == "normal":
        return [rng.gauss(0.0, 1.0) for _ in range(size)], 0.0
    if distribution == "exponential":
        return [rng.expovariate(1.0) for _ in range(size)], 1.0
    raise ValueError(f"unknown distribution: {distribution}")


def run_simulation(
    *,
    trials: int,
    bootstrap_replicates: int,
    seed: int = SEED,
) -> list[dict[str, float | int | str]]:
    records: list[dict[str, float | int | str]] = []
    distributions = ("normal", "exponential")
    sample_sizes = (10, 30)

    for distribution_index, distribution in enumerate(distributions):
        for sample_size in sample_sizes:
            summaries = {
                method: {"covered": 0, "widths": []}
                for method in METHODS
            }
            for trial in range(trials):
                data_seed = (
                    seed
                    + distribution_index * 1_000_000
                    + sample_size * 10_000
                    + trial
                )
                sample, true_mean = draw_sample(
                    distribution,
                    sample_size,
                    random.Random(data_seed),
                )
                for method_index, (method, interval_fn) in enumerate(
                    METHODS.items()
                ):
                    method_seed = data_seed + (method_index + 1) * 100_000_000
                    lower, upper = interval_fn(
                        sample,
                        random.Random(method_seed),
                        bootstrap_replicates,
                    )
                    summaries[method]["covered"] += int(
                        lower <= true_mean <= upper
                    )
                    summaries[method]["widths"].append(upper - lower)

            for method, summary in summaries.items():
                coverage = summary["covered"] / trials
                records.append(
                    {
                        "distribution": distribution,
                        "sample_size": sample_size,
                        "method": method,
                        "trials": trials,
                        "bootstrap_replicates": bootstrap_replicates,
                        "coverage": round(coverage, 6),
                        "coverage_mc_se": round(
                            math.sqrt(coverage * (1 - coverage) / trials),
                            6,
                        ),
                        "mean_width": round(
                            statistics.fmean(summary["widths"]),
                            6,
                        ),
                    }
                )
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/latest.json"),
    )
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    trials = 120 if args.quick else 600
    bootstrap_replicates = 150 if args.quick else 500
    result = {
        "schema_version": 1,
        "seed": args.seed,
        "mode": "quick" if args.quick else "full",
        "question": (
            "How do common 95% confidence intervals for a mean behave "
            "at small sample sizes under skew?"
        ),
        "experiments": run_simulation(
            trials=trials,
            bootstrap_replicates=bootstrap_replicates,
            seed=args.seed,
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {len(result['experiments'])} results to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
