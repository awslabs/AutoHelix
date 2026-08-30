#!/usr/bin/env python3
"""Evaluate an AutoHelix template through fresh inner AlgoTune runs."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from autohelix.checks import parse_metric_output


HERE = Path(__file__).resolve().parent
TEMPLATE_PATH = HERE / "inner-template.yaml"
RUBRIC_PATH = HERE / "rubric.md"
SUITE_PATH = HERE / "suite.yaml"
MAX_GOAL_CHARS = 6000
MAX_RUBRIC_CHARS = 3000
MIN_ITERATIONS = 1
MAX_ITERATIONS = 4
DEFAULT_TOTAL_SECONDS = 240
DEFAULT_COST_CAP = 0.5
FIXED_INNER_INSTRUCTIONS = """
Harness rule: leave solver.py changes uncommitted. Do not run git commit;
AutoHelix validates and commits accepted changes.
""".strip()


@dataclass(frozen=True)
class InnerTemplate:
    iterations: int
    goal: str
    rubric: str


def validate_template(template_path: Path, rubric_path: Path) -> InnerTemplate:
    raw = yaml.safe_load(template_path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("inner-template.yaml must be a mapping")
    unknown = set(raw) - {"iterations", "goal"}
    if unknown:
        raise ValueError(f"unknown template keys: {', '.join(sorted(unknown))}")

    iterations = raw.get("iterations")
    if (
        isinstance(iterations, bool)
        or not isinstance(iterations, int)
        or not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS
    ):
        raise ValueError(
            f"iterations must be an integer from {MIN_ITERATIONS} to "
            f"{MAX_ITERATIONS}"
        )

    goal = str(raw.get("goal") or "").strip()
    if not goal:
        raise ValueError("template goal must not be empty")
    if len(goal) > MAX_GOAL_CHARS:
        raise ValueError(
            f"template goal is {len(goal)} characters; limit is {MAX_GOAL_CHARS}"
        )
    for placeholder in ("{{TASK_GOAL}}", "{{RUBRIC}}"):
        if placeholder not in goal:
            raise ValueError(f"template goal must contain {placeholder}")

    rubric = rubric_path.read_text().strip()
    if not rubric:
        raise ValueError("rubric.md must not be empty")
    if len(rubric) > MAX_RUBRIC_CHARS:
        raise ValueError(
            f"rubric.md is {len(rubric)} characters; limit is "
            f"{MAX_RUBRIC_CHARS}"
        )
    return InnerTemplate(iterations=iterations, goal=goal, rubric=rubric)


def load_suite(path: Path) -> dict[str, tuple[str, ...]]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("suite.yaml must be a mapping")
    result: dict[str, tuple[str, ...]] = {}
    for mode in ("development", "holdout"):
        names = raw.get(mode)
        if not isinstance(names, list) or not names:
            raise ValueError(f"suite.yaml {mode} must be a non-empty list")
        result[mode] = tuple(str(name) for name in names)
    return result


def render_goal(template: InnerTemplate, task_goal: str) -> str:
    editable_goal = (
        template.goal.replace("{{TASK_GOAL}}", task_goal.strip())
        .replace("{{RUBRIC}}", template.rubric)
        .strip()
    )
    return f"{editable_goal}\n\n{FIXED_INNER_INSTRUCTIONS}"


def configure_inner_project(
    project: Path,
    template: InnerTemplate,
    *,
    total_seconds: int,
    cost_cap: float,
) -> None:
    config_path = project / "autohelix.yaml"
    config = yaml.safe_load(config_path.read_text())
    task_goal = str(config["goal"])
    per_iteration_seconds = max(1, total_seconds // template.iterations)

    agent: dict[str, Any] = {
        "type": os.environ.get("NESTED_AUTOHELIX_AGENT", "claude"),
        "timeout_seconds": per_iteration_seconds + 30,
    }
    model = os.environ.get("NESTED_AUTOHELIX_MODEL")
    if model:
        agent["model"] = model

    config["goal"] = render_goal(template, task_goal)
    config["agent"] = agent
    config["budget"] = {
        "iterations": template.iterations,
        "iteration_time": per_iteration_seconds,
        "cost": cost_cap,
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))


def scaffold_task(
    destination: Path,
    task_name: str,
    template: InnerTemplate,
    *,
    total_seconds: int,
    cost_cap: float,
) -> None:
    shutil.copytree(HERE / "tasks" / task_name, destination)
    configure_inner_project(
        destination,
        template,
        total_seconds=total_seconds,
        cost_cap=cost_cap,
    )
    subprocess.run(["git", "init", "-q"], cwd=destination, check=True)
    subprocess.run(
        ["autohelix", "init"],
        cwd=destination,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=destination, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "Initial optimization task"],
        cwd=destination,
        check=True,
    )


def read_history(project: Path) -> list[dict[str, Any]]:
    path = project / ".autohelix" / "history.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def summarize_history(history: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = next(
        (row for row in history if row.get("iteration") == 0),
        None,
    )
    if not baseline or "speedup" not in (baseline.get("metrics") or {}):
        raise ValueError("inner history has no baseline speedup metric")

    final_speedup = float(baseline["metrics"]["speedup"])
    tokens = 0
    cost = 0.0
    curve = [final_speedup]
    for row in history:
        if row.get("iteration") == 0:
            continue
        usage = row.get("usage") or {}
        tokens += int(usage.get("input_tokens") or 0)
        tokens += int(usage.get("output_tokens") or 0)
        cost += float(usage.get("cost_usd") or 0)
        metrics = row.get("metrics") or {}
        if row.get("accepted") and "speedup" in metrics:
            final_speedup = float(metrics["speedup"])
        curve.append(final_speedup)

    return {
        "speedup": final_speedup,
        "speedup_curve": curve,
        "tokens": tokens,
        "cost_usd": cost,
        "history": history,
    }


def collect_notes(project: Path) -> dict[str, str]:
    notes_dir = project / ".autohelix" / "notes"
    if not notes_dir.exists():
        return {}
    return {
        path.name: path.read_text()[:12000]
        for path in sorted(notes_dir.glob("iter-*.md"))
    }


def collect_diff(project: Path) -> str:
    root = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return subprocess.run(
        ["git", "diff", root, "HEAD", "--", "solver.py"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout[:24000]


def evaluate_persisted_project(project: Path) -> dict[str, Any]:
    validation = subprocess.run(
        ["python", "check.py"],
        cwd=project,
        capture_output=True,
        text=True,
    )
    benchmark = subprocess.run(
        ["python", "benchmark.py"],
        cwd=project,
        capture_output=True,
        text=True,
    )
    benchmark_output = benchmark.stdout + benchmark.stderr
    speedup = parse_metric_output(benchmark_output, metric_name="speedup")
    return {
        "valid": (
            validation.returncode == 0
            and benchmark.returncode == 0
            and speedup is not None
        ),
        "speedup": speedup,
        "validation_output": (
            validation.stdout + validation.stderr
        )[-4000:],
        "benchmark_output": benchmark_output[-4000:],
    }


def run_task(
    task_name: str,
    *,
    template: InnerTemplate,
    work_root: Path,
    total_seconds: int,
    cost_cap: float,
) -> dict[str, Any]:
    project = work_root / task_name
    scaffold_task(
        project,
        task_name,
        template,
        total_seconds=total_seconds,
        cost_cap=cost_cap,
    )

    started = time.monotonic()
    process = subprocess.run(
        ["autohelix", "run", "-n", str(template.iterations)],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=total_seconds + 180,
    )
    persisted = evaluate_persisted_project(project)
    history = read_history(project)
    summary = summarize_history(history)
    history_speedup = summary["speedup"]
    if persisted["speedup"] is not None:
        summary["speedup"] = persisted["speedup"]
    summary.update(
        {
            "task": task_name,
            "valid": process.returncode == 0 and persisted["valid"],
            "returncode": process.returncode,
            "wall_seconds": time.monotonic() - started,
            "notes": collect_notes(project),
            "solution_diff": collect_diff(project),
            "history_speedup": history_speedup,
            "output_tail": (process.stdout + process.stderr)[-8000:],
            "validation_output": persisted["validation_output"],
            "benchmark_output": persisted["benchmark_output"],
        }
    )
    return summary


def geometric_mean(values: list[float]) -> float:
    return math.exp(
        sum(math.log(max(value, 1e-9)) for value in values) / len(values)
    )


def aggregate(tasks: list[dict[str, Any]]) -> dict[str, float]:
    return {
        "speedup": geometric_mean([task["speedup"] for task in tasks]),
        "cost": sum(task["cost_usd"] for task in tasks),
        "valid_task_rate": (
            sum(task["valid"] for task in tasks) / len(tasks)
        ),
        "inner_tokens": sum(task["tokens"] for task in tasks),
        "wall_seconds": sum(task["wall_seconds"] for task in tasks),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the editable template without launching agents",
    )
    parser.add_argument(
        "--holdout",
        action="store_true",
        help="run the transfer suite instead of development tasks",
    )
    parser.add_argument("--task", action="append", dest="task_names")
    parser.add_argument(
        "--report",
        type=Path,
        default=HERE / "results" / "report.json",
    )
    args = parser.parse_args()

    try:
        template = validate_template(TEMPLATE_PATH, RUBRIC_PATH)
        suite = load_suite(SUITE_PATH)
    except ValueError as exc:
        parser.error(str(exc))

    if args.check:
        print(
            f"OK: {template.iterations} iteration(s), "
            f"{len(template.goal)} goal chars, "
            f"{len(template.rubric)} rubric chars"
        )
        return 0

    mode = "holdout" if args.holdout else "development"
    available = suite[mode]
    selected = tuple(args.task_names) if args.task_names else available
    unknown = set(selected) - set(available)
    if unknown:
        parser.error(f"unknown task(s): {', '.join(sorted(unknown))}")

    total_seconds = int(
        os.environ.get(
            "NESTED_AUTOHELIX_TOTAL_SECONDS",
            DEFAULT_TOTAL_SECONDS,
        )
    )
    cost_cap = float(
        os.environ.get(
            "NESTED_AUTOHELIX_COST_CAP",
            DEFAULT_COST_CAP,
        )
    )
    if total_seconds < template.iterations:
        parser.error("inner total seconds must cover every iteration")
    if cost_cap <= 0:
        parser.error("inner cost cap must be positive")

    with tempfile.TemporaryDirectory(prefix="nested-autohelix-") as temp:
        task_results = [
            run_task(
                name,
                template=template,
                work_root=Path(temp),
                total_seconds=total_seconds,
                cost_cap=cost_cap,
            )
            for name in selected
        ]

    metrics = aggregate(task_results)
    report = {
        "template": {
            "iterations": template.iterations,
            "goal": template.goal,
            "rubric": template.rubric,
        },
        "mode": mode,
        "inner_budget": {
            "total_seconds_per_task": total_seconds,
            "cost_cap_per_task": cost_cap,
        },
        "metrics": metrics,
        "tasks": task_results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    for task in task_results:
        status = "valid" if task["valid"] else "invalid"
        print(
            f"{task['task']}: {task['speedup']:.3f}x, "
            f"${task['cost_usd']:.3f}, {status}"
        )
    print(f"##autohelix[speedup={metrics['speedup']:.8g}]")
    print(f"##autohelix[cost={metrics['cost']:.8g}]")
    if metrics["valid_task_rate"] < 1:
        print("one or more inner projects failed final validation")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
