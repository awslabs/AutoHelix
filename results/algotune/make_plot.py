"""Figures for the AlgoTune comparison (30 tasks): speedup against cost through the iterations,
and held-out speedup per task.

    python results/algotune/make_plot.py    # writes assets/algotune_{progress,tasks}-{light,dark}.png

results.json holds every arm's best dev candidate per task, rescored on held-out instances,
and every iteration's and attempt's dev speedup (see examples/algotune/README.md). Speedups are
floored at 1x; the audit's adjustments are applied (`test_adjusted`, `dev_adjustments`).
aes_gcm_encryption stays in as measured, flagged: the evaluator's memory use during the runs
hurt AutoHelix and best-of there, not /goal.
"""

import itertools
import json
import math
import os
import statistics

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "..", "..", "assets")
data = json.load(open(os.path.join(HERE, "results.json")))
subset = data["sets"]["subset"]["tasks"]
progress = data["sets"]["subset"]["dev_progress"]
TASKS = sorted(subset)
FLAGGED = {"aes_gcm_encryption"}  # † without an adjustment

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
    return "test_adjusted" in subset[task][label] or task in FLAGGED


def dev_steps(task):
    """Best-so-far dev speedup after 0..5 AutoHelix iterations and after 1..5 best-of attempts
    (index 0: the baseline, 1x), floored at 1x, with the audit's adjustments."""
    ah, best = [1.0], [1.0]
    for k in range(1, 6):
        accepted = [i["speedup"] for i in progress[task]["autohelix_dev"]
                    if i["iteration"] <= k and i["accepted"] and i["speedup"]
                    and not (task == "wasserstein_dist" and i["iteration"] == 5)]
        ah.append(max(accepted + [1.0]))
        attempts = [3.0 if task == "shortest_path_dijkstra" and a["attempt"] in (1, 5) and a["speedup"]
                    else a["speedup"] or 1.0 for a in progress[task]["best_dev"] if a["attempt"] <= k]
        best.append(max(attempts + [1.0]))
    return ah, best


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


def step_costs(task):
    """Agent cost after 0..5 AutoHelix iterations (in run order)."""
    return [0.0] + [sum(i["cost_usd"] for i in progress[task]["autohelix_dev"] if i["iteration"] <= k)
                    for k in range(1, 6)]


def best_of(task):
    """Best of N independent attempts for N = 0..5, averaged over every subset of N of the 5
    attempts (as pass@k is estimated), so the attempts' run order adds no noise: (speedup as the
    geometric mean over subsets of the best, cost as N times the mean attempt cost)."""
    attempts = [(3.0 if task == "shortest_path_dijkstra" and a["attempt"] in (1, 5) and a["speedup"]
                 else max(a["speedup"] or 1.0, 1.0), a["cost_usd"]) for a in progress[task]["best_dev"]]
    speed, cost = [1.0], [0.0]
    for k in range(1, 6):
        subsets = list(itertools.combinations(attempts, k))
        speed.append(math.exp(statistics.mean(math.log(max(a[0] for a in c)) for c in subsets)))
        cost.append(statistics.mean(sum(a[1] for a in c) for c in subsets))
    return speed, cost


def draw_progress(theme):
    t = THEMES[theme]
    blue, orange, aqua = t["series"]
    n = len(TASKS)
    ah_speed = [dev_steps(task)[0] for task in TASKS]
    ah_cost = [step_costs(task) for task in TASKS]
    best = [best_of(task) for task in TASKS]
    fig, ax = plt.subplots(figsize=(9, 5.4), dpi=160)
    fig.patch.set_facecolor(t["surface"])
    style(ax, t)
    series = [("Best of N independent attempts", aqua, "s", 3,
               [sum(b[1][k] for b in best) / n for k in range(6)],
               [statistics.geometric_mean([b[0][k] for b in best]) for k in range(6)]),
              ("AutoHelix: iterations 1-5", blue, "o", 4,
               [sum(c[k] for c in ah_cost) / n for k in range(6)],
               [statistics.geometric_mean([s[k] for s in ah_speed]) for k in range(6)])]
    for name, color, marker, z, xs, ys in series:
        # Straight segments, not steps: each point averages tasks whose iterations cost very
        # different amounts, so no single spend marks where the next result arrives.
        ax.plot(xs, ys, color=color, linewidth=2.2, zorder=z, label=name, solid_capstyle="round")
        ax.scatter(xs[1:], ys[1:], s=48, marker=marker, color=color, edgecolors=t["surface"],
                   linewidths=1.5, zorder=z + 0.5)
        ax.annotate(f"{ys[-1]:.0f}×", xy=(xs[-1], ys[-1]), xytext=(9, 0), textcoords="offset points",
                    va="center", color=t["text"], fontsize=10.5, fontweight="bold")
    goal_x = sum(subset[task]["goal"]["cost_usd"] for task in TASKS) / n
    goal = [max(subset[task]["goal"].get("dev_rescored") or 1.0, 1.0) for task in TASKS]
    goal_y = statistics.geometric_mean(goal)
    ax.scatter([goal_x], [goal_y], s=75, marker="D", color=orange, edgecolors=t["surface"],
               linewidths=1.5, zorder=6, label="Claude Code /goal: one session per task")
    ax.annotate(f"{goal_y:.0f}×", xy=(goal_x, goal_y), xytext=(-9, 0), textcoords="offset points",
                ha="right", va="center", color=t["text"], fontsize=10.5, fontweight="bold")
    ax.set_xlim(0, 13.6)
    ax.set_ylim(0, 200)
    ax.xaxis.set_major_formatter(lambda v, _: f"${v:.0f}")
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}×")
    ax.set_xlabel(f"agent API cost per task (USD, mean over the {n} tasks)", color=t["muted"], fontsize=10)
    ax.set_ylabel("speedup over reference (geometric mean)", color=t["muted"], fontsize=10)
    handles, labels = ax.get_legend_handles_labels()
    order = [labels.index(name) for name in ("AutoHelix: iterations 1-5",
             "Claude Code /goal: one session per task", "Best of N independent attempts")]
    ax.legend([handles[i] for i in order], [labels[i] for i in order], loc="upper left",
              frameon=False, fontsize=9, labelcolor=t["text"])
    fig.suptitle(f"AlgoTune, {n} random tasks: speedup against what the agent cost",
                 x=0.07, ha="left", color=t["text"], fontsize=12.5, fontweight="bold")
    ax.set_title("Best development score so far · Claude Opus 5.5 · /goal stopped on its own in every task",
                 loc="left", color=t["muted"], fontsize=8.8)
    fig.tight_layout()
    path = os.path.join(ASSETS, f"algotune_progress-{theme}.png")
    fig.savefig(path, facecolor=t["surface"])
    print("wrote", os.path.relpath(path))


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
    fig.suptitle(f"AlgoTune, {len(TASKS)} random tasks: held-out speedup per task",
                 x=0.02, ha="left", color=t["text"], fontsize=12.5, fontweight="bold")
    ax.set_title("Each arm's best development candidate, rescored on 100 instances generated after\n"
                 "the runs (10 runs each). Claude Opus 5.5, 8 cores. † audit adjustment: wasserstein dist\n"
                 "AutoHelix uses iteration 3 (iteration 5 inverts the generator); shortest path dijkstra\n"
                 "best-of set to 3× (its output exploited an evaluator flaw); aes gcm encryption as measured\n"
                 "(the evaluator's memory use during the runs hurt AutoHelix and best-of there).", loc="left", color=t["muted"], fontsize=8.3)
    fig.tight_layout()
    path = os.path.join(ASSETS, f"algotune_tasks-{theme}.png")
    fig.savefig(path, facecolor=t["surface"])
    print("wrote", os.path.relpath(path))


for _, labels, _ in ARMS:
    for label in labels:
        print(f"{label:12s} ${cost(label):7.2f}  geomean {geomean(label):6.1f}x  "
              f"median {statistics.median(speedup(t, label) for t in TASKS):6.1f}x")
for theme in THEMES:
    draw_progress(theme)
    draw_tasks(theme)
