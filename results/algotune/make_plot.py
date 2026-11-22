"""Figures for the AlgoTune comparison (30 tasks): speedup against cost through the iterations, held-out
speedup per task, and speedup against cost.

    python results/algotune/make_plot.py    # writes assets/algotune_{progress,tasks,cost}-{light,dark}.png

results.json holds every arm's best dev candidate per task, rescored on held-out instances,
and every iteration's and attempt's dev speedup (see examples/algotune/README.md). Speedups are
floored at 1x; the audit's adjustments are applied (`test_adjusted`, `dev_adjustments`).
aes_gcm_encryption stays in as measured, flagged: the evaluator's memory use during the runs
hurt AutoHelix and best-of there, not /goal.
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
    """Agent cost after 0..5 AutoHelix iterations and after 0..5 best-of attempts."""
    ah = [0.0] + [sum(i["cost_usd"] for i in progress[task]["autohelix_dev"] if i["iteration"] <= k)
                  for k in range(1, 6)]
    best = [0.0] + [sum(a["cost_usd"] for a in progress[task]["best_dev"] if a["attempt"] <= k)
                    for k in range(1, 6)]
    return ah, best


def draw_progress(theme):
    t = THEMES[theme]
    blue, orange, aqua = t["series"]
    n = len(TASKS)
    steps = [dev_steps(task) for task in TASKS]
    costs = [step_costs(task) for task in TASKS]
    curves = []
    for arm, (name, color, marker, z) in enumerate((("AutoHelix: iterations 1-5", blue, "o", 4),
                                                    ("Best of 1-5 independent attempts", aqua, "s", 3))):
        xs = [sum(c[arm][k] for c in costs) / n for k in range(6)]
        ys = [statistics.geometric_mean([s[arm][k] for s in steps]) for k in range(6)]
        curves.append((name, color, marker, z, xs, ys))
    goal_x = sum(subset[task]["goal"]["cost_usd"] for task in TASKS) / n
    goal_dev = statistics.geometric_mean([max(subset[task]["goal"].get("dev_rescored") or 1.0, 1.0)
                                          for task in TASKS])
    fig, ax = plt.subplots(figsize=(9, 5.4), dpi=160)
    fig.patch.set_facecolor(t["surface"])
    style(ax, t)
    for name, color, marker, z, xs, ys in curves:
        ax.plot(xs, ys, color=color, linewidth=2.2, zorder=z, label=name, solid_capstyle="round")
        ax.scatter(xs, ys, s=48, marker=marker, color=color, edgecolors=t["surface"], linewidths=1.5,
                   zorder=z + 0.5)
    ax.scatter([goal_x], [goal_dev], s=70, marker="D", color=orange, edgecolors=t["surface"],
               linewidths=1.5, zorder=5, label="Claude Code /goal: one session per task")
    for (name, color, marker, z, xs, ys), arm in zip(curves, ("autohelix", "best")):
        for k in (3, 5):
            ax.annotate(f"{'iteration' if arm == 'autohelix' else 'attempt'} {k}", xy=(xs[k], ys[k]),
                        xytext=(7, -12 if arm == "best" else 6), textcoords="offset points",
                        color=t["muted"], fontsize=8.5)
    held = [(curves[0][4][k], statistics.geometric_mean([speedup(task, f"autohelix@{k}") for task in TASKS]),
             blue, "o") for k in (3, 5)]
    held += [(curves[1][4][k], statistics.geometric_mean([speedup(task, f"best@{k}") for task in TASKS]),
              aqua, "s") for k in (3, 5)]
    held.append((goal_x, statistics.geometric_mean([speedup(task, "goal") for task in TASKS]), orange, "D"))
    for x, y, color, marker in held:
        ax.scatter([x], [y], s=95, marker=marker, facecolors="none", edgecolors=color, linewidths=1.8,
                   zorder=6)
    ax.scatter([], [], s=70, marker="o", facecolors="none", edgecolors=t["muted"], linewidths=1.8,
               label="same candidate, held-out instances")
    for name, color, marker, z, xs, ys in curves:
        ax.annotate(f"{ys[-1]:.0f}×", xy=(xs[-1], ys[-1]), xytext=(9, 0), textcoords="offset points",
                    va="center", color=t["text"], fontsize=10.5, fontweight="bold")
    ax.annotate(f"{goal_dev:.0f}×", xy=(goal_x, goal_dev), xytext=(9, 4), textcoords="offset points",
                va="center", color=t["text"], fontsize=10.5, fontweight="bold")
    ax.set_xlim(0, 13.5)
    ax.set_ylim(0, 210)
    ax.xaxis.set_major_formatter(lambda v, _: f"${v:.0f}")
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}×")
    ax.set_xlabel("agent API cost per task (USD, mean over the 30 tasks)", color=t["muted"], fontsize=10)
    ax.set_ylabel("speedup over reference (geometric mean)", color=t["muted"], fontsize=10)
    ax.legend(loc="lower right", frameon=False, fontsize=9, labelcolor=t["text"])
    fig.suptitle(f"AlgoTune, {n} random tasks: speedup against what the agent cost",
                 x=0.07, ha="left", color=t["text"], fontsize=12.5, fontweight="bold")
    ax.set_title("Filled: best development score so far (100 dev instances). Open: the same candidates "
                 "rescored on 100\nheld-out instances generated after the runs. Claude Opus 5.5, 8 cores; "
                 "audit adjustments applied; floored at 1×.", loc="left", color=t["muted"], fontsize=8.8)
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
    ax.set_title("Held-out instances, floored at 1×, audit adjustments applied, all 30 tasks. Claude Opus 5.5. "
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
    draw_progress(theme)
    draw_tasks(theme)
    draw_cost(theme)
