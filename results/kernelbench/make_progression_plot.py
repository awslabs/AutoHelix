import json
import math
import os
import statistics as st

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results.json")
MAX_ITER = 5

tasks = json.load(open(RESULTS))


def series(task):
    """Best-so-far speedup: baseline followed by optimization iterations."""
    best_by_iter = {
        int(iteration): speedup
        for iteration, speedup in task.get("best_so_far", {}).items()
    }
    values = [1.0]
    last = 1.0
    for iteration in range(1, MAX_ITER + 1):
        last = best_by_iter.get(iteration, last)
        values.append(last)
    return values


all_series = [series(task) for task in tasks]


def column(iteration):
    return [values[iteration] for values in all_series]


def geomean(values):
    return math.exp(sum(math.log(value) for value in values) / len(values))


median = [st.median(column(iteration)) for iteration in range(MAX_ITER + 1)]
geometric_mean = [
    geomean(column(iteration)) for iteration in range(MAX_ITER + 1)
]

print("iter   median   geomean   n")
for iteration in range(MAX_ITER + 1):
    print(
        f"  {iteration}    {median[iteration]:7.2f}  "
        f"{geometric_mean[iteration]:7.2f}   {len(tasks)}"
    )
print(f"\nfinal: median {median[-1]:.2f}x over {len(tasks)} tasks")

x = np.arange(MAX_ITER + 1)
fig, ax = plt.subplots(1, 1, figsize=(6.5, 5))
color = "#2a6f97"

for values in all_series:
    ax.plot(x, values, color=color, alpha=0.08, linewidth=0.7)

ax.plot(
    x,
    median,
    color=color,
    linewidth=2.6,
    marker="o",
    markersize=7,
    label=f"median ({len(tasks)} tasks)",
    zorder=10,
)
ax.annotate(
    f"{median[-1]:.1f}x",
    xy=(MAX_ITER, median[-1]),
    xytext=(8, 0),
    textcoords="offset points",
    fontsize=9,
    fontweight="bold",
    color=color,
    ha="left",
    va="center",
)

ax.set_yscale("log")
ax.set_ylim(0.95, 6)
ax.set_xticks(x)
ax.set_xticklabels(["baseline"] + [f"iter {i}" for i in range(1, MAX_ITER + 1)])
ax.set_xlim(-0.3, MAX_ITER + 0.7)
ax.set_ylabel("Speedup over reference", fontsize=10)
ax.set_title(
    "Median KernelBench speedup (Claude Opus 4.8, H200)",
    fontsize=11,
    fontweight="bold",
    pad=10,
)
ax.legend(loc="upper left", fontsize=8.5, framealpha=0.8)
ax.grid(True, alpha=0.15, which="major", linewidth=0.6)
ax.grid(True, alpha=0.07, which="minor", linewidth=0.4)
for spine in ("top", "right"):
    ax.spines[spine].set_visible(False)

fig.tight_layout()
output = os.path.abspath(
    os.path.join(HERE, "..", "..", "assets", "progression_kernelbench.png")
)
fig.savefig(output, dpi=150, bbox_inches="tight")
print("saved", output)
