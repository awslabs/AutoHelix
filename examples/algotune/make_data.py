#!/usr/bin/env python3
"""Write AlgoTune instances for one task and split, with AlgoTune's own generator and our seed.

    python -I make_data.py --task svm --split dev --seed 1000 --out DATA [--size 100]

Writes DATA/<task>/<split>.jsonl (plus any .npy files it refers to) and DATA/<task>/<split>.json
with the seed and AlgoTune commit. Problem size n and target time come from AlgoTune's
reports/generation.json, so instances are the leaderboard's size. AlgoTune's create_dataset
uses seeds SEED+i for its train file and SEED+train+i for its test file (plus retries when
the reference fails an instance); we keep one of the two, so the records' seeds are
SEED.. for dev and SEED+1.. for test. Keep dev and test seeds far apart (dev and test
from the same seed would share 99 instances).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--task", required=True)
    parser.add_argument("--split", choices=("dev", "test"), required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--size", type=int, default=100, help="instances (AlgoTune: 100)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--algotune", type=Path, default=os.environ.get("ALGOTUNE_ROOT"))
    args = parser.parse_args()
    if args.algotune is None:
        parser.error("set --algotune or $ALGOTUNE_ROOT to an AlgoTune checkout")
    root = args.algotune.resolve()
    sys.path.insert(0, str(root))
    os.environ["ALGOTUNE_HF_DISABLE"] = "1"
    generation = json.loads((root / "reports/generation.json").read_text())
    if args.task not in generation:
        raise SystemExit(f"{args.task} has no entry in reports/generation.json")
    settings = generation[args.task]
    from AlgoTuneTasks.factory import TaskFactory

    task = TaskFactory(args.task, oracle_time_limit=None,
                       target_time_ms=settings["target_time_ms"])
    train, test = (args.size, 1) if args.split == "dev" else (1, args.size)
    keep = "train" if args.split == "dev" else "test"
    target = args.out.resolve() / args.task
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target) as scratch:
        work = Path(scratch) / args.task
        work.mkdir()  # create_dataset writes into DATA/<task>/ only if it exists
        task.create_dataset(k=settings["n"], train_size=train, test_size=test,
                            random_seed=args.seed, data_dir=str(Path(scratch)))
        [made] = work.glob(f"*_{keep}.jsonl")
        for path in work.iterdir():  # array files keep their names; the jsonl refers to them
            if path.suffix == ".jsonl":
                continue
            if path.is_dir():  # e.g. _npy_data/: merge into an existing one
                shutil.copytree(path, target / path.name, dirs_exist_ok=True)
            else:
                shutil.copy2(path, target / path.name)
        shutil.move(str(made), target / f"{args.split}.jsonl")
    commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    (target / f"{args.split}.json").write_text(json.dumps({
        "task": args.task, "split": args.split, "seed": args.seed, "size": args.size,
        "n": settings["n"], "target_time_ms": settings["target_time_ms"],
        "algotune_commit": commit, "source_file": made.name}, indent=1))
    print(f"{target / (args.split + '.jsonl')}: {args.size} instances, seed {args.seed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
