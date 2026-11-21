#!/usr/bin/env python3
"""Set up one AlgoTune task as a standalone AutoHelix project.

Needs an AlgoTune checkout (--algotune, else $ALGOTUNE_ROOT), pinned to one commit, which
is recorded in the project. Writes the task's module made self-contained (`<task>.py`, for
reading and as the starting solver's import), description.txt, solver.py (the reference) and
the experiment autohelix/algotune/config.yaml, and makes the folder a git repository with one
commit.

    python setup.py eigenvalues_real [--dir DIR]
    python setup.py --list

The evaluation the config runs, `sudo -n algotune-gate`, exists in the agent image
(Dockerfile.agent), with the task and its development instances fixed when the container
starts. Elsewhere, run evaluate.py directly (see its docstring).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent / "templates"
CONFIG = Path("autohelix") / "algotune" / "config.yaml"  # the experiment `autohelix run` finds


def algotune_root(given: str | None) -> Path:
    root = Path(given or os.environ.get("ALGOTUNE_ROOT", "")).expanduser()
    if not (root / "AlgoTuneTasks").is_dir():
        raise SystemExit("needs an AlgoTune checkout: --algotune PATH or $ALGOTUNE_ROOT")
    return root.resolve()


def tasks(root: Path) -> list[str]:
    return sorted(json.loads((root / "reports/generation.json").read_text()))


def class_name(task_file: Path) -> str:
    """The task class: the one defining solve() and generate_problem()."""
    content = task_file.read_text()
    for match in re.finditer(r"^class (\w+?)(?:\(|:).*?(?=^class |\Z)", content,
                             re.MULTILINE | re.DOTALL):
        if "def solve(" in match.group(0) and "def generate_problem(" in match.group(0):
            return match.group(1)
    raise SystemExit(f"no task class with solve() and generate_problem() in {task_file}")


def self_contained(source: Path, dest: Path) -> None:
    """The task module without AlgoTune's base class and registration."""
    out = []
    for line in source.read_text().split("\n"):
        if "from AlgoTuneTasks" in line or "import AlgoTuneTasks" in line or "@register_task" in line:
            continue
        if line.strip().startswith("class ") and "(Task)" in line:
            line = line.replace("(Task)", "")
        elif "super().__init__" in line:
            line = " " * (len(line) - len(line.lstrip())) + "pass"
        out.append(line)
    dest.write_text("\n".join(out))


def git(folder: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=AutoHelix", "-c", "user.email=autohelix@localhost",
                    "-c", "commit.gpgsign=false", *args], cwd=folder, check=True)


def setup(task: str, folder: Path, root: Path) -> None:
    source = root / "AlgoTuneTasks" / task
    if not (source / f"{task}.py").is_file():
        raise SystemExit(f"no task {task!r} in {root / 'AlgoTuneTasks'}")
    folder.mkdir(parents=True, exist_ok=True)
    self_contained(source / f"{task}.py", folder / f"{task}.py")
    shutil.copy2(source / "description.txt", folder / "description.txt")
    variables = {"task_name": task, "class_name": class_name(folder / f"{task}.py")}
    (folder / "solver.py").write_text((TEMPLATES / "solver.py.tmpl").read_text().format(**variables))
    config = folder / CONFIG
    config.parent.mkdir(parents=True)
    config.write_text((TEMPLATES / "config.yaml.tmpl").read_text().format(**variables))
    commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    (folder / ".algotune.json").write_text(json.dumps({"task": task, "algotune_commit": commit}))
    (folder / ".gitignore").write_text("__pycache__/\n*.pyc\n*.so\nbuild/\n.autohelix/\n")
    git(folder, "init", "-q")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "--no-verify", "-m", f"AlgoTune task {task} (AlgoTune {commit[:7]})")
    print(f"{folder}: {task}, AlgoTune {commit[:7]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("task", nargs="?")
    parser.add_argument("--dir", type=Path, help="project folder (default ./<task>)")
    parser.add_argument("--algotune", help="AlgoTune checkout (default $ALGOTUNE_ROOT)")
    parser.add_argument("--list", action="store_true", help="list the tasks")
    args = parser.parse_args()
    root = algotune_root(args.algotune)
    if args.list:
        print("\n".join(tasks(root)))
        return 0
    if not args.task:
        parser.error("name a task (or --list)")
    folder = (args.dir or Path(args.task)).resolve()
    if folder.exists() and any(folder.iterdir()):
        raise SystemExit(f"{folder} exists and isn't empty")
    setup(args.task, folder, root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
