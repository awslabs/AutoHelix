"""Figures for the AlgoTune comparison: held-out speedup per task, and speedup against cost.

    python results/algotune/make_plot.py    # writes assets/algotune_{tasks,cost}-{light,dark}.png

results.json holds every arm's best dev candidate per task, rescored on held-out instances
(see examples/algotune/README.md). Speedups are held-out test, floored at 1x; the audit's
adjustments are applied (`test_adjusted`) and aes_gcm_encryption is left out (an evaluator
memory artifact during the runs).
"""

import json
import os
import statistics

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "..", "..", "assets")
data = json.load(open(os.path.join(HERE, "results.json")))
subset = data["sets"]["subset"]["tasks"]
EXCLUDED = {"aes_gcm_encryption"}
TASKS = sorted(t for t in subset if t not in EXCLUDED)

ARMS = [  # (label, budget labels in results.json, marker); colors: categorical slots 1-3
    ("AutoHelix", ["autohelix@3", "autohelix@5"], "o"),
    ("Claude Code /goal", ["goal"], "D"),
    ("Best of independent attempts", ["best@3", "best@5"], "s"),
]
THEMES = {
    "light": {"surface": "#fcfcfb", "text": "#0b0b0b", "muted": "#52514e", "grid": "#e4e3df",
              "series": ["#2a78d6", "#eb6834", "#1baf7a"]},
    "dark": {"surface": "#1a1a19", "text": "#f0efec", "muted": "#c3c2b7", "grid": "#383835",
             "series": ["#3987e5", "#d95926", "#199e70"]},
}


def speedup(task, label):
    e = subset[task][label]
    value = e.get("test_adjusted", e["test"])
    return max(value or 1.0, 1.0)


def adjusted(task, label):
    return "test_adjusted" in subset[task][label]


def cost(label):
    return sum(subset[t][label]["cost_usd"] for t in TASKS)


def geomean(label):
    return statistics.geometric_mean([speedup(t, label) for t in TASKS])


def style(ax, t):
    ax.set_facecolor(t["surface"])
    ax.grid(True, color=t["grid"], linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(t["grid"])
    ax.tick_params(colors=t["muted"], labelsize=9)


def draw_tasks(theme):
    t = THEMES[theme]
    shown = [(name, labels[-1], marker, color) for (name, labels, marker), color in zip(ARMS, t["series"])]
    order = sorted(TASKS, key=lambda task: speedup(task, "autohelix@5"))
    fig, ax = plt.subplots(figsize=(9, 9.6), dpi=160)
    fig.patch.set_facecolor(t["surface"])
    style(ax, t)
    ax.grid(True, axis="x", color=t["grid"], linewidth=0.8)
    ax.grid(False, axis="y")
    for y, task in enumerate(order):
        values = [speedup(task, label) for _, label, _, _ in shown]
        ax.plot([min(values), max(values)], [y, y], color=t["grid"], linewidth=6, zorder=1,
                solid_capstyle="round", alpha=0.7)
    for k, (name, label, marker, color) in enumerate(shown):
        dodge = (k - 1) * 0.22  # arms side by side within a row, so close values don't hide
        budget = label.split("@")[1] if "@" in label else None
        legend = f"{name} ({'-n ' + budget if name == 'AutoHelix' else budget + ' attempts'})" \
            if budget else name
        xs = [speedup(task, label) for task in order]
        ax.scatter(xs, [y + dodge for y in range(len(order))], s=58, marker=marker, color=color, zorder=3,
                   edgecolors=t["surface"], linewidths=1.5, label=legend)
    names = [task.replace("_", " ") + (" †" if any(adjusted(task, l) for _, l, _, _ in shown) else "")
             for task in order]
    ax.set_yticks(range(len(order)), names, color=t["text"], fontsize=9)
    ax.set_ylim(-0.7, len(order) - 0.3)
    ax.set_xscale("log")
    ax.set_xlim(1, 10000)
    ax.xaxis.set_major_formatter(lambda v, _: f"{v:g}×")
    ax.set_xlabel("held-out speedup over AlgoTune's reference solve() (log scale)", color=t["muted"],
                  fontsize=10)
    ax.legend(loc="lower right", frameon=False, fontsize=9, labelcolor=t["text"])
    fig.suptitle("AlgoTune, 29 random tasks: held-out speedup per task",
                 x=0.02, ha="left", color=t["text"], fontsize=12.5, fontweight="bold")
    ax.set_title("Each arm's best development candidate, rescored on 100 instances generated after\n"
                 "the runs (10 runs each). Claude Opus 5.5, 8 cores. † audit adjustment: wasserstein dist\n"
                 "AutoHelix uses iteration 3 (iteration 5 inverts the generator); shortest path dijkstra\n"
                 "best-of set to 3× (its output exploited an evaluator flaw). aes gcm encryption left out\n"
                 "(an evaluator memory artifact during the runs).", loc="left", color=t["muted"], fontsize=8.3)
    fig.tight_layout()
    path = os.path.join(ASSETS, f"algotune_tasks-{theme}.png")
    fig.savefig(path, facecolor=t["surface"])
    print("wrote", os.path.relpath(path))


def draw_cost(theme):
    t = THEMES[theme]
    fig, ax = plt.subplots(figsize=(9, 5.2), dpi=160)
    fig.patch.set_facecolor(t["surface"])
    style(ax, t)
    for (name, labels, marker), color in zip(ARMS, t["series"]):
        xs, ys = [cost(l) for l in labels], [geomean(l) for l in labels]
        ax.plot(xs, ys, color=color, linewidth=2, zorder=2, solid_capstyle="round")
        ax.scatter(xs, ys, s=70, marker=marker, color=color, edgecolors=t["surface"], linewidths=2,
                   zorder=3, label=name)
        for x, y, label in zip(xs, ys, labels):
            budget = label.split("@")[1] if "@" in label else None
            text = (f"-n {budget}" if name == "AutoHelix" else f"{budget} attempts") if budget else "one session"
            offset = {"autohelix@3": (-8, 6), "autohelix@5": (8, -3), "best@3": (-6, -16),
                      "best@5": (8, -3), "goal": (-8, 9)}[label]
            ax.annotate(f"{text}: {y:.0f}×", xy=(x, y), xytext=offset, textcoords="offset points",
                        ha="right" if offset[0] < 0 else "left", color=t["text"], fontsize=9)
    wins = sum(speedup(task, "autohelix@5") > speedup(task, "best@5") for task in TASKS)
    wins_goal = sum(speedup(task, "autohelix@5") > speedup(task, "goal") for task in TASKS)
    ax.text(0.99, 0.04, f"AutoHelix -n 5 is faster on {wins}/{len(TASKS)} tasks than the best of 5 "
            f"attempts\nand on {wins_goal}/{len(TASKS)} than /goal", transform=ax.transAxes, ha="right",
            va="bottom", color=t["muted"], fontsize=9)
    ax.set_xlim(0, 400)
    ax.set_ylim(0, 220)
    ax.xaxis.set_major_formatter(lambda v, _: f"${v:.0f}")
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}×")
    ax.set_xlabel(f"total agent API cost, {len(TASKS)} tasks (USD)", color=t["muted"], fontsize=10)
    ax.set_ylabel("geometric mean held-out speedup", color=t["muted"], fontsize=10)
    ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=t["text"])
    fig.suptitle(f"AlgoTune, {len(TASKS)} random tasks: speedup against cost",
                 x=0.07, ha="left", color=t["text"], fontsize=12.5, fontweight="bold")
    ax.set_title("Held-out instances, floored at 1×, audit adjustments applied. Claude Opus 5.5. "
                 "Best-of: independent one-iteration\nAutoHelix runs; /goal: one Claude Code session "
                 "per task, which stopped on its own (never at its $15 cap).",
                 loc="left", color=t["muted"], fontsize=8.8)
    fig.tight_layout()
    path = os.path.join(ASSETS, f"algotune_cost-{theme}.png")
    fig.savefig(path, facecolor=t["surface"])
    print("wrote", os.path.relpath(path))


for _, labels, _ in ARMS:
    for label in labels:
        print(f"{label:12s} ${cost(label):7.2f}  geomean {geomean(label):6.1f}x  "
              f"median {statistics.median(speedup(t, label) for t in TASKS):6.1f}x")
for theme in THEMES:
    draw_tasks(theme)
    draw_cost(theme)
