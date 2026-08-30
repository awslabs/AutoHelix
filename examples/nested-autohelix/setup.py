#!/usr/bin/env python3
"""Prepare a self-contained nested AutoHelix project from AlgoTune tasks."""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import yaml


HERE = Path(__file__).resolve().parent
EXAMPLES_DIR = HERE.parent
PROJECT_TEMPLATE = HERE / "project"
ALGOTUNE_SETUP = EXAMPLES_DIR / "algotune" / "setup.py"

TASKS = (
    {"name": "stable_matching", "size": 600, "mode": "development"},
    {"name": "eigenvalues_real", "size": 300, "mode": "development"},
    {"name": "pde_heat1d", "size": 5, "mode": "holdout"},
)
NUM_PROBLEMS = 6
NUM_RUNS = 3
NUM_CHECKS = 12


def load_algotune_setup() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "autohelix_algotune_setup",
        ALGOTUNE_SETUP,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {ALGOTUNE_SETUP}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scaffold_task(
    algotune: ModuleType,
    *,
    task_name: str,
    problem_size: int,
    destination: Path,
) -> None:
    destination.mkdir(parents=True)
    algotune_root = algotune.find_algotune_root()
    if not algotune.download_task(task_name, destination, algotune_root):
        raise RuntimeError(f"failed to fetch AlgoTune task '{task_name}'")

    task_file = destination / f"{task_name}.py"
    variables = {
        "task_name": task_name,
        "class_name": algotune.find_class_name(task_file),
        "problem_size": problem_size,
        "num_problems": NUM_PROBLEMS,
        "num_runs": NUM_RUNS,
        "num_check": NUM_CHECKS,
    }
    for template_path in algotune.TEMPLATES_DIR.glob("*.tmpl"):
        output_name = template_path.name.removesuffix(".tmpl")
        (destination / output_name).write_text(
            algotune.render_template(template_path, variables)
        )
    (destination / ".gitignore").write_text("__pycache__/\n*.pyc\n.autohelix/\n")


def initialize_project(destination: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=destination, check=True)
    try:
        subprocess.run(
            ["autohelix", "init"],
            cwd=destination,
            check=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("'autohelix' is not installed or not on PATH") from exc
    subprocess.run(["git", "add", "-A"], cwd=destination, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "Initial nested AutoHelix project"],
        cwd=destination,
        check=True,
    )


def setup(destination: Path) -> None:
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"{destination} already exists and is not empty")
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PROJECT_TEMPLATE, destination, dirs_exist_ok=True)

    algotune = load_algotune_setup()
    tasks_dir = destination / "tasks"
    tasks_dir.mkdir()
    for task in TASKS:
        print(f"Preparing {task['name']}...")
        scaffold_task(
            algotune,
            task_name=str(task["name"]),
            problem_size=int(task["size"]),
            destination=tasks_dir / str(task["name"]),
        )

    suite = {
        "development": [
            task["name"] for task in TASKS if task["mode"] == "development"
        ],
        "holdout": [
            task["name"] for task in TASKS if task["mode"] == "holdout"
        ],
    }
    (destination / "suite.yaml").write_text(
        yaml.safe_dump(suite, sort_keys=False)
    )
    initialize_project(destination)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch AlgoTune tasks and prepare nested AutoHelix.",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path("./nested-autohelix-run"),
        help="output directory",
    )
    args = parser.parse_args()

    try:
        setup(args.dir.resolve())
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))

    print(f"\nReady: {args.dir.resolve()}")
    print(f"\n  cd {args.dir.resolve()}")
    print("  autohelix run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
