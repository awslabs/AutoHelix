#!/usr/bin/env python3
"""Set up an AlgoTune task as a standalone AutoHelix project.

Fetches an AlgoTune task (from a local clone if present, else GitHub), strips
the AlgoTune framework so it stands alone, scaffolds a solver + benchmark +
correctness check from templates, and git-inits the project.

Run this from wherever you want the project to live (NOT inside the AutoHelix
repo — the scaffolded project is its own git repo):

    python /path/to/examples/algotune/setup.py eigenvalues_real
    python /path/to/examples/algotune/setup.py kmeans --size 500
    python /path/to/examples/algotune/setup.py --list
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

SETUP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SETUP_DIR / "config.yaml"
TEMPLATES_DIR = SETUP_DIR / "templates"


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


# ---------------------------------------------------------------------------
# Fetching the task
# ---------------------------------------------------------------------------


def find_class_name(task_file: Path) -> str:
    """Find the task class (the one with solve + generate_problem methods)."""
    content = task_file.read_text()
    class_pattern = re.compile(
        r"^class (\w+?)(?:\(|:).*?(?=^class |\Z)", re.MULTILINE | re.DOTALL
    )
    for match in class_pattern.finditer(content):
        class_name = match.group(1)
        body = match.group(0)
        if "def solve(" in body and "def generate_problem(" in body:
            return class_name
    # Fallback: first class
    matches = re.findall(r"^class (\w+?)(?:\(|:)", content, re.MULTILINE)
    if matches:
        return matches[0]
    # Last resort: derive from task name
    name = task_file.stem
    return "".join(word.capitalize() for word in name.split("_"))


def make_self_contained(source: Path, dest: Path) -> None:
    """Strip AlgoTune framework imports to make the task standalone."""
    lines = source.read_text().split("\n")
    new_lines: list[str] = []
    for line in lines:
        if "from AlgoTuneTasks" in line or "import AlgoTuneTasks" in line:
            continue
        if "@register_task" in line:
            continue
        if line.strip().startswith("class ") and "(Task)" in line:
            line = line.replace("(Task)", "")
            new_lines.append(line)
            continue
        if "super().__init__" in line:
            indent = len(line) - len(line.lstrip())
            new_lines.append(" " * indent + "pass")
            continue
        new_lines.append(line)
    dest.write_text("\n".join(new_lines))


def find_algotune_root() -> Path | None:
    """Walk up from cwd looking for a local AlgoTune clone."""
    p = Path.cwd()
    for _ in range(10):
        if (p / "AlgoTuneTasks").is_dir():
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def get_algotune_generation(algotune_root: Path | None) -> dict | None:
    """Load generation.json from a local AlgoTune clone."""
    if algotune_root is None:
        return None
    gen_file = algotune_root / "reports" / "generation.json"
    if not gen_file.exists():
        return None
    with open(gen_file) as f:
        return json.load(f)


def download_algotune_generation() -> dict | None:
    """Download generation.json from GitHub."""
    url = "https://raw.githubusercontent.com/oripress/AlgoTune/main/reports/generation.json"
    result = subprocess.run(["curl", "-fsSL", url], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def list_tasks(algotune_root: Path | None) -> list[str]:
    """Get available AlgoTune task names."""
    gen = get_algotune_generation(algotune_root)
    if gen:
        return sorted(gen.keys())
    gen = download_algotune_generation()
    if gen:
        return sorted(gen.keys())
    return []


def download_task(task_name: str, output_dir: Path, algotune_root: Path | None) -> bool:
    """Get the task module — from local clone or GitHub."""
    # Try local first
    if algotune_root:
        candidate = algotune_root / "AlgoTuneTasks" / task_name / f"{task_name}.py"
        if candidate.exists():
            print(f"Using local {task_name}.py from {candidate.parent}")
            make_self_contained(candidate, output_dir / f"{task_name}.py")
            desc = candidate.parent / "description.txt"
            if desc.exists():
                shutil.copy2(desc, output_dir / "description.txt")
            return True

    # Fall back to GitHub
    config = load_config()
    base_url = config["source"]["raw_url"]
    url = f"{base_url}/{task_name}/{task_name}.py"
    print(f"Downloading {task_name}.py from AlgoTune GitHub...")
    result = subprocess.run(["curl", "-fsSL", url], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error: Failed to download '{task_name}' — not found in AlgoTune repo.")
        print(result.stderr)
        return False
    task_file = output_dir / f"{task_name}.py"
    task_file.write_text(result.stdout)
    make_self_contained(task_file, task_file)

    # Try description
    desc_url = f"https://raw.githubusercontent.com/oripress/AlgoTune/main/AlgoTuneTasks/{task_name}/description.txt"
    desc_result = subprocess.run(["curl", "-fsSL", desc_url], capture_output=True, text=True)
    if desc_result.returncode == 0:
        (output_dir / "description.txt").write_text(desc_result.stdout)

    return True


def get_problem_size(task_name: str, algotune_root: Path | None) -> int | None:
    """Auto-detect problem size from generation.json."""
    gen = get_algotune_generation(algotune_root)
    if gen and task_name in gen and "n" in gen[task_name]:
        return gen[task_name]["n"]
    gen = download_algotune_generation()
    if gen and task_name in gen and "n" in gen[task_name]:
        return gen[task_name]["n"]
    return None


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


def setup(task_name: str, output_dir: Path, problem_size: int | None = None) -> bool:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir = output_dir.resolve()

    algotune_root = find_algotune_root()
    if algotune_root:
        print(f"AlgoTune root: {algotune_root}")

    if not download_task(task_name, output_dir, algotune_root):
        return False

    task_file = output_dir / f"{task_name}.py"
    class_name = find_class_name(task_file)

    config = load_config()
    defaults = config["defaults"]

    if problem_size is None:
        problem_size = get_problem_size(task_name, algotune_root)
    if problem_size is None:
        problem_size = defaults["problem_size"]
        print(f"Note: could not determine problem size, defaulting to {problem_size}")
    print(f"Problem size: {problem_size}")

    variables = {
        "task_name": task_name,
        "class_name": class_name,
        "problem_size": problem_size,
        "num_problems": defaults["num_problems"],
        "num_runs": defaults["num_runs"],
        "num_check": defaults["num_check"],
    }

    for tmpl_file in TEMPLATES_DIR.iterdir():
        if not tmpl_file.name.endswith(".tmpl"):
            continue
        output_name = tmpl_file.name[:-5]  # strip .tmpl
        (output_dir / output_name).write_text(render_template(tmpl_file, variables))

    (output_dir / ".gitignore").write_text("__pycache__/\n*.pyc\n.autohelix/\n")

    # Sanity-check the scaffold
    print("\nTesting setup...")
    result = subprocess.run(
        ["python", "check.py"], cwd=output_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  check.py failed:\n{result.stdout}\n{result.stderr}")
    else:
        print(f"  check.py: {result.stdout.strip()}")

    result = subprocess.run(
        ["python", "benchmark.py"], cwd=output_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  benchmark.py failed:\n{result.stdout}\n{result.stderr}")
    else:
        for line in result.stdout.strip().split("\n"):
            print(f"  {line}")

    git_init(output_dir)

    print(f"\nReady: {output_dir}")
    print()
    print(f"  cd {output_dir}")
    print(f"  autohelix run -n 5")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set up an AlgoTune task as an AutoHelix project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s eigenvalues_real         # scaffold into ./eigenvalues_real
  %(prog)s kmeans --size 500        # custom problem size
  %(prog)s kmeans --dir ./my-kmeans # custom output dir
  %(prog)s --list                   # list all AlgoTune tasks
""",
    )
    parser.add_argument("task", nargs="?", help="AlgoTune task name (e.g. eigenvalues_real)")
    parser.add_argument("--dir", "-d", help="Output directory (default: ./<task>)")
    parser.add_argument("--size", "-s", type=int, default=None,
                        help="Problem size (auto-detected if omitted)")
    parser.add_argument("--list", "-l", action="store_true", help="List available tasks")

    args = parser.parse_args()

    if args.list:
        algotune_root = find_algotune_root()
        tasks = list_tasks(algotune_root)
        if not tasks:
            print("Could not fetch AlgoTune task list. Try cloning the repo locally.")
            return 1
        print(f"AlgoTune tasks ({len(tasks)}):")
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

    return 0 if setup(args.task, output_dir, args.size) else 1


if __name__ == "__main__":
    sys.exit(main())
