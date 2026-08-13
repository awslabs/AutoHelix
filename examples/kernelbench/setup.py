#!/usr/bin/env python3
"""Set up a KernelBench GPU-kernel task as a standalone AutoHelix project.

Reads problems from a one-time HuggingFace download (`--download`), scaffolds a
solution stub + evaluation harness from templates, and git-inits the project.

KernelBench tasks are plain PyTorch modules: a reference `Model(nn.Module)` with
`get_inputs()` / `get_init_inputs()`. The agent writes an equivalent `ModelNew`
that computes the same thing faster (custom CUDA/Triton kernels, fused ops, etc.).

Nothing from KernelBench is bundled into this repo — task code is fetched at setup
time from the HuggingFace dataset (MIT, ScalingIntelligence/KernelBench) into the
*scaffolded* project (its own git repo).

Run this from wherever you want the project to live (NOT inside the AutoHelix
repo — the scaffolded project is its own git repo):

    python /path/to/examples/kernelbench/setup.py --download   # one-time
    python /path/to/examples/kernelbench/setup.py L1_1_Square_matrix_multiplication
    python /path/to/examples/kernelbench/setup.py --list
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

SETUP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SETUP_DIR / "config.yaml"
TEMPLATES_DIR = SETUP_DIR / "templates"
DATA_DIR = SETUP_DIR / "data"


# ---------------------------------------------------------------------------
# Shared scaffolding helpers (kept local — benchmark setup scripts stand alone)
# ---------------------------------------------------------------------------


def git_init(directory: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=directory, check=True)
    subprocess.run(["git", "add", "-A"], cwd=directory, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "Initial commit"],
        cwd=directory,
        check=True,
    )


def render_template(template_path: Path, variables: dict) -> str:
    return template_path.read_text().format(**variables)


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())


def _level_key(level: int) -> str:
    """KernelBench task-name prefix for a level (level 1 -> 'L1')."""
    return f"L{level}"


def _clean_name(raw_name: str) -> str:
    """Turn a dataset `name` (e.g. '1_Square_matrix_multiplication_.py') into a
    filesystem-friendly slug ('1_Square_matrix_multiplication')."""
    stem = raw_name[:-3] if raw_name.endswith(".py") else raw_name
    return stem.rstrip("_")


# ---------------------------------------------------------------------------
# Data download
# ---------------------------------------------------------------------------


def download(levels: list[int] | None = None) -> bool:
    """Download KernelBench problems from HuggingFace."""
    try:
        from datasets import load_dataset
    except ImportError:
        print("Error: 'datasets' package required. Install with: pip install datasets")
        return False

    config = load_config()
    hf_dataset = config["source"]["hf_dataset"]
    all_levels = config["source"]["levels"]
    levels = levels or all_levels

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    for level in levels:
        level_dir = DATA_DIR / _level_key(level)
        level_dir.mkdir(parents=True, exist_ok=True)
        print(f"  Downloading level {level}...")
        ds = load_dataset(hf_dataset, split=f"level_{level}")

        for row in ds:
            slug = _clean_name(row["name"])
            problem_dir = level_dir / slug
            problem_dir.mkdir(parents=True, exist_ok=True)
            # The `code` column is a complete .py defining Model / get_inputs /
            # get_init_inputs. Store it verbatim as the reference.
            (problem_dir / "reference.py").write_text(row["code"])
        print(f"  level {level}: {len(ds)} problems")

    print(f"\nKernelBench data saved to: {DATA_DIR}")
    return True


def list_tasks() -> list[str]:
    """List available KernelBench tasks from downloaded data."""
    if not DATA_DIR.exists():
        return []
    tasks = []
    for level_dir in sorted(DATA_DIR.iterdir()):
        if not level_dir.is_dir():
            continue
        level = level_dir.name  # e.g. "L1"
        for problem_dir in sorted(level_dir.iterdir()):
            if (problem_dir / "reference.py").exists():
                tasks.append(f"{level}_{problem_dir.name}")
    return tasks


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


def _find_problem_dir(task_name: str) -> Path | None:
    """Resolve a task name (e.g. L1_1_Square_matrix_multiplication) to its dir."""
    if not DATA_DIR.exists():
        return None

    # Task names are "<level>_<slug>", e.g. "L1_1_Square_matrix_multiplication".
    m = re.match(r"^(L\d+)_(.+)$", task_name)
    if m:
        level, slug = m.group(1), m.group(2)
        candidate = DATA_DIR / level / slug
        if (candidate / "reference.py").exists():
            return candidate

    # Fallback: search every level for a matching slug.
    for level_dir in sorted(DATA_DIR.iterdir()):
        if not level_dir.is_dir():
            continue
        candidate = level_dir / task_name
        if (candidate / "reference.py").exists():
            return candidate
    return None


def setup(task_name: str, output_dir: Path) -> bool:
    problem_dir = _find_problem_dir(task_name)
    if problem_dir is None:
        print(f"Error: KernelBench task '{task_name}' not found.")
        print()
        if not DATA_DIR.exists():
            print("Data not downloaded yet. Run:")
            print(f"  python {Path(__file__).name} --download")
        else:
            print("Use --list to see available tasks.")
        return False

    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir = output_dir.resolve()

    # Copy the reference module into the scaffolded project.
    problem_out = output_dir / "problem"
    problem_out.mkdir(exist_ok=True)
    shutil.copy2(problem_dir / "reference.py", problem_out / "reference.py")

    reference_code = (problem_dir / "reference.py").read_text()
    problem_name = task_name

    config = load_config()
    defaults = config["defaults"]

    variables = {
        "problem_name": problem_name,
        "reference_code": reference_code,
        "evaluation_timeout": defaults["evaluation_timeout"],
        "correctness_trials": defaults["correctness_trials"],
        "iterations": defaults["iterations"],
        "agent_model": defaults["agent_model"],
    }

    for tmpl_file in TEMPLATES_DIR.iterdir():
        if not tmpl_file.name.endswith(".tmpl"):
            continue
        output_name = tmpl_file.name[:-5]  # strip .tmpl
        (output_dir / output_name).write_text(render_template(tmpl_file, variables))

    (output_dir / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.autohelix/\nbuild/\n"
    )

    print("\nTesting setup...")
    for script in ("solution.py", "check_solution.py", "eval_solution.py"):
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", script],
            cwd=output_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"  {script}: failed to compile")
            print(result.stderr.strip())
            return False
        print(f"  {script}: syntax ok")

    result = subprocess.run(
        [sys.executable, "check_solution.py"],
        cwd=output_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  check_solution.py failed:\n{result.stdout}\n{result.stderr}")
        return False
    print(f"  check_solution.py: {result.stdout.strip()}")

    git_init(output_dir)

    print(f"\nReady: {output_dir}")
    print(f"  Problem: {problem_name}")
    print()
    print(f"  cd {output_dir}")
    print(f"  autohelix run")
    print()
    print("  Requires: GPU with PyTorch CUDA support")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set up a KernelBench task as an AutoHelix project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s --download                            # one-time HuggingFace data download
  %(prog)s L1_1_Square_matrix_multiplication     # scaffold into ./L1_1_Square_matrix_multiplication
  %(prog)s L1_1_Square_matrix_multiplication -d ./k  # custom output dir
  %(prog)s --list                                # list downloaded tasks
""",
    )
    parser.add_argument("task", nargs="?", help="KernelBench task (e.g. L1_1_Square_matrix_multiplication)")
    parser.add_argument("--dir", "-d", help="Output directory (default: ./<task>)")
    parser.add_argument("--list", "-l", action="store_true", help="List downloaded tasks")
    parser.add_argument("--download", action="store_true",
                        help="Download KernelBench data from HuggingFace")

    args = parser.parse_args()

    if args.download:
        print("Downloading KernelBench data from HuggingFace...")
        return 0 if download() else 1

    if args.list:
        tasks = list_tasks()
        if not tasks:
            print("KernelBench data not downloaded yet. Run:")
            print(f"  python {Path(__file__).name} --download")
            return 1
        print(f"KernelBench tasks ({len(tasks)}):")
        print()
        for t in tasks:
            print(f"  {t}")
        return 0

    if not args.task:
        parser.print_help()
        return 1

    output_dir = Path(args.dir) if args.dir else Path(f"./{args.task}")
    if output_dir.exists() and any(output_dir.iterdir()):
        print(f"Error: {output_dir} already exists and is not empty.")
        return 1

    return 0 if setup(args.task, output_dir) else 1


if __name__ == "__main__":
    sys.exit(main())
