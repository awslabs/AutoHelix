#!/usr/bin/env python3
"""Set up a bundled example as a standalone AutoHelix project.

Bundled examples are copy-and-run: this copies the example directory, git-inits
it, and prints run instructions. A bundled example is any directory here that
contains an `autohelix.yaml`.

AlgoTune tasks live under `examples/algotune/` with their own `setup.py` — it
fetches a task from an upstream suite rather than ship a ready-to-run project.

Usage:
    python examples/setup_example.py sorting
    python examples/setup_example.py ml-recipe --dir ./my-run
    python examples/setup_example.py --list
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# Reference examples ship in examples/ but need manual setup (a prepared
# environment, agent-written code, etc.) — they aren't copy-and-run, so setup
# points at their README instead of scaffolding.
REFERENCE_EXAMPLES = {"posttrain"}


def git_init_repo(directory: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=directory, check=True)


def git_commit_all(directory: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=directory, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "Initial commit"],
        cwd=directory,
        check=True,
    )


def autohelix_init(directory: Path) -> bool:
    """Run `autohelix init` in the scaffolded dir. Returns False if not on PATH.

    Output is captured — setup_example prints its own next-steps block, and
    init's "edit your goal" hint doesn't apply here (the example ships a goal).
    """
    try:
        subprocess.run(
            ["autohelix", "init"], cwd=directory, check=True, capture_output=True
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def list_bundled() -> list[str]:
    """A bundled example is any dir here with an autohelix.yaml."""
    return sorted(
        d.name for d in SCRIPT_DIR.iterdir()
        if d.is_dir()
        and d.name not in REFERENCE_EXAMPLES
        and (d / "autohelix.yaml").exists()
    )


def list_reference() -> list[str]:
    return sorted(
        name for name in REFERENCE_EXAMPLES if (SCRIPT_DIR / name).is_dir()
    )


def setup_bundled(name: str, output_dir: Path) -> bool:
    src = SCRIPT_DIR / name
    output_dir.mkdir(parents=True, exist_ok=True)

    for item in src.iterdir():
        if item.name in ("__pycache__", ".autohelix"):
            continue
        if item.is_dir():
            shutil.copytree(
                item, output_dir / item.name,
                ignore=shutil.ignore_patterns("__pycache__", ".autohelix"),
            )
        else:
            shutil.copy2(item, output_dir / item.name)

    # git init first (autohelix init needs a repo), then autohelix init (writes
    # .autohelix/ + a .gitignore), then commit so the tree is clean for `run`.
    git_init_repo(output_dir)
    initialized = autohelix_init(output_dir)
    git_commit_all(output_dir)

    print(f"Ready: {output_dir}")
    print()
    print(f"  cd {output_dir}")
    if initialized:
        print(f"  autohelix run")
    else:
        print(f"  autohelix init      # 'autohelix' not found on PATH — install it first")
        print(f"  autohelix run")
    return True


def print_list() -> None:
    bundled = list_bundled()
    if bundled:
        print("Bundled examples (copy-and-run):")
        for name in bundled:
            print(f"  {name}")
        print()

    reference = list_reference()
    if reference:
        print("Reference examples (manual setup — see each dir's README):")
        for name in reference:
            print(f"  {name}")
        print()

    print("AlgoTune tasks (fetch a task from an upstream suite):")
    print("  examples/algotune/setup.py <task>")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set up a bundled example as an AutoHelix project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s sorting                 # scaffold into ./sorting
  %(prog)s ml-recipe --dir ./run   # custom output dir
  %(prog)s --list                  # show all examples

AlgoTune tasks live under examples/algotune/ with their own setup.py:
  python examples/algotune/setup.py eigenvalues_real
""",
    )
    parser.add_argument("name", nargs="?", help="Example name (e.g. sorting)")
    parser.add_argument("--dir", "-d", help="Output directory (default: ./<name>)")
    parser.add_argument("--list", "-l", action="store_true", help="List available examples")

    args = parser.parse_args()

    if args.list:
        print_list()
        return 0

    if not args.name:
        parser.print_help()
        return 1

    name = args.name

    if name in REFERENCE_EXAMPLES:
        readme = SCRIPT_DIR / name / "README.md"
        print(f"'{name}' is a reference example that needs manual setup.")
        print(f"See {readme} for instructions.")
        return 1

    if name not in list_bundled():
        print(f"Error: '{name}' is not a bundled example.")
        print()
        print(f"Bundled examples: {', '.join(list_bundled())}")
        print("AlgoTune tasks live under examples/algotune/ (see its setup.py).")
        print("Use --list to see everything.")
        return 1

    output_dir = Path(args.dir) if args.dir else Path(f"./{name}")
    if output_dir.exists() and any(output_dir.iterdir()):
        print(f"Error: {output_dir} already exists and is not empty.")
        return 1

    return 0 if setup_bundled(name, output_dir) else 1


if __name__ == "__main__":
    sys.exit(main())
