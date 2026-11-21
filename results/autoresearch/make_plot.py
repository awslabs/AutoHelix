"""README figure for the autoresearch comparison (wave 2): improvement over the baseline
against cumulative agent API cost, two runs per loop, test-shard scores at the end.

    python results/autoresearch/make_plot.py    # writes assets/autoresearch_cost-{light,dark}.png

results.json is exported from the GPU box by export_results.py.
"""

import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "..", "..", "assets")
data = json.load(open(os.path.join(HERE, "results.json")))

ARMS = [  # (label, runs); colors are categorical slots 1-3 in this order
    ("AutoHelix", ["autohelix", "autohelix-2"]),
    ("Claude Code /goal", ["goal", "goal-2"]),
    ("Karpathy's program.md loop", ["karpathy", "karpathy-2"]),
]
THEMES = {
    "light": {"surface": "#fcfcfb", "text": "#0b0b0b", "muted": "#52514e", "grid": "#e4e3df",
              "series": ["#2a78d6", "#eb6834", "#1baf7a"]},
    "dark": {"surface": "#1a1a19", "text": "#f0efec", "muted": "#c3c2b7", "grid": "#383835",
             "series": ["#3987e5", "#d95926", "#199e70"]},
}


def curve(run):
    """Best-so-far improvement (%) over the run's own baseline measurement, by cumulative cost."""
    rows = [r for r in run["rows"] if r.get("val_bpb") is not None]
    base = rows[0]["val_bpb"]  # the unmodified train.py, measured in-run (seeds 42/43)
    xs, ys = [0.0], [0.0]
    for r in rows[1:]:
        xs.append(r["cost_usd"])
        ys.append(100 * (base - r["best"]) / base)
    xs.append(run["total_cost_usd"])  # the session's cost after its last experiment
    ys.append(ys[-1])
    return xs, ys


def test_gain(run):
    base = data["baseline"]["test"]["mean"]
    return 100 * (base - run["test"]["mean"]) / base


def draw(theme):
    t = THEMES[theme]
    fig, ax = plt.subplots(figsize=(9, 5.2), dpi=160)
    fig.patch.set_facecolor(t["surface"])
    ax.set_facecolor(t["surface"])
    for (label, names), color in zip(ARMS, t["series"]):
        ends = []
        for name in names:
            run = data["runs"][name]
            xs, ys = curve(run)
            ax.step(xs, ys, where="post", color=color, linewidth=2, solid_capstyle="round", zorder=2,
                    label=label if name == names[0] else None)
            x, y = run["total_cost_usd"], test_gain(run)
            ax.plot([x], [y], marker="o", markersize=8, color=color, markeredgecolor=t["surface"],
                    markeredgewidth=2, linestyle="none", zorder=3)
            ends.append((x, y))
        x = max(e[0] for e in ends)
        y = sum(e[1] for e in ends) / len(ends)
        gains = " and ".join(f"{e[1]:.2f}%" for e in sorted(ends, key=lambda e: -e[1]))
        ax.annotate(f"{label}\n{gains} on test",
                    xy=(x, y), xytext=(10, 0), textcoords="offset points", va="center",
                    color=t["text"], fontsize=9.5)
    ax.plot([], [], marker="o", markersize=7, color=t["muted"], linestyle="none", label="final train.py, test shard")
    legend = ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=t["text"])
    ax.set_xlim(0, 56)
    ax.set_ylim(0, 5.6)
    ax.set_xlabel("cumulative agent API cost (USD)", color=t["muted"], fontsize=10)
    ax.set_ylabel("val_bpb lower than baseline (%)", color=t["muted"], fontsize=10)
    ax.xaxis.set_major_formatter(lambda v, _: f"${v:.0f}")
    ax.grid(True, color=t["grid"], linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(t["grid"])
    ax.tick_params(colors=t["muted"], labelsize=9)
    fig.suptitle("Autoresearch on an H200: three agent loops, same model and budget",
                 x=0.07, ha="left", color=t["text"], fontsize=12.5, fontweight="bold")
    ax.set_title("Lines: best so far during each run (validation shard, seeds 42/43); two runs per loop.\n"
                 "Claude Opus 5.5 · up to 60 experiments per run, each two 380 s trainings · each run isolated in its own container", loc="left", color=t["muted"], fontsize=8.8)
    fig.tight_layout()
    path = os.path.join(ASSETS, f"autoresearch_cost-{theme}.png")
    fig.savefig(path, facecolor=t["surface"])
    print("wrote", os.path.relpath(path))


for name, run in data["runs"].items():
    print(f"{name:12s} ${run['total_cost_usd']:6.2f}  val {run['val']['mean']:.5f}  "
          f"test {run['test']['mean']:.5f} ({test_gain(run):.2f}% better than baseline)")
for theme in THEMES:
    draw(theme)
