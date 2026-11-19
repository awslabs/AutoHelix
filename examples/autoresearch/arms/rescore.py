#!/usr/bin/env python3
"""Score one experiment's train.py again, in a fresh copy of the arms' base commit.

    AR_GPU=J,K python3 arms/rescore.py ARM EXPERIMENT|best [-k K]

ARM: autohelix, autohelix-2 (projects/ar-ARM's latest run; EXPERIMENT is an iteration,
"best" the run's accepted tip), karpathy or goal (EXPERIMENT is a call number in
arms/ARM/experiments.jsonl, "best" the lowest val_bpb logged). Runs ar/train_and_score.sh
K times (default 1) with seed pairs 42/43, 44/45, ... on AR_GPU; the first pair is the
one the arms score with. With AR_SCORE_DATA set (the operator's test shard), scores on it
instead of the validation shard. Appends the result to arms/rescores.jsonl.
"""

import argparse
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ARMS = Path(__file__).resolve().parent
LOCAL = ARMS.parent


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def train_py(arm, experiment):
    """(train.py's bytes, the experiment it is) for ARM's EXPERIMENT or "best"."""
    if arm.startswith("autohelix"):
        project = LOCAL / "projects" / f"ar-{arm}"
        run = sorted((project / ".autohelix" / "runs").glob("*/history.jsonl"))[-1].parent.name
        if experiment == "best":
            return git("show", f"refs/autohelix/{run}:train.py", cwd=project), f"{run} tip"
        records = {}
        for line in (project / ".autohelix" / "runs" / run / "history.jsonl").read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                records[r["iteration"]] = r
        r = records[int(experiment)]
        rev = r.get("commit") or r.get("rejected_ref") or r.get("base")
        return git("show", f"{rev}:train.py", cwd=project), f"{run} iteration {experiment}"
    calls = [json.loads(line) for line in (ARMS / arm / "experiments.jsonl").read_text().splitlines() if line.strip()]
    calls = [c for c in calls if "call" in c]
    if experiment == "best":
        scored = [c for c in calls if c["status"] == "ok" and "val_bpb" in c["metrics"]]
        call = min(scored, key=lambda c: c["metrics"]["val_bpb"])
    else:
        call = next(c for c in calls if c["call"] == int(experiment))
    source = (ARMS / arm / "train_py" / f"{call['call']:03d}.py").read_bytes()
    assert hashlib.sha256(source).hexdigest() == call["train_py_sha256"], "train_py copy changed since it ran"
    return source, f"call {call['call']} (logged val_bpb {call['metrics'].get('val_bpb')})"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("arm")
    parser.add_argument("experiment", help="an iteration or call number, or best")
    parser.add_argument("-k", type=int, default=1, help="seed pairs (default 1: 42/43)")
    args = parser.parse_args()
    if not os.environ.get("AR_GPU"):
        sys.exit("rescore: set AR_GPU to two free GPUs, e.g. AR_GPU=6,7")
    source, what = train_py(args.arm, args.experiment)
    base = (ARMS / "BASE").read_text().strip()
    data = os.environ.get("AR_SCORE_DATA") or "val"
    print(f"rescore: {args.arm} {what}, train.py {hashlib.sha256(source).hexdigest()[:12]}, {args.k} seed pair(s), data {data}")
    scores = []
    for k in range(args.k):
        seed = 42 + 2 * k
        with tempfile.TemporaryDirectory(dir=LOCAL / "tmp", prefix="ar-rescore.") as work:
            archive = subprocess.run(["git", f"--git-dir={ARMS / 'base.git'}", "archive", base], check=True,
                                     capture_output=True).stdout
            subprocess.run(["tar", "-x", "-C", work], input=archive, check=True)
            Path(work, "train.py").write_bytes(source)
            env = dict(os.environ, AR_SEED=str(seed), TMPDIR=work)
            started = time.time()
            result = subprocess.run(["bash", "ar/train_and_score.sh"], cwd=work, env=env, capture_output=True, text=True)
        found = re.findall(r"^##autohelix\[val_bpb=([0-9.]+)\]$", result.stdout, re.M)
        per = re.findall(r"^val_bpb per training: (.*)$", result.stdout, re.M)
        if result.returncode != 0 or not found:
            print(result.stdout[-3000:])
            sys.exit(f"rescore: seeds {seed}/{seed + 1} failed (exit {result.returncode})")
        scores.append(float(found[0]))
        print(f"  seeds {seed}/{seed + 1}: val_bpb {found[0]} (per training {per[0] if per else '?'}), {time.time() - started:.0f}s")
    mean = statistics.mean(scores)
    sd = statistics.stdev(scores) if len(scores) > 1 else None
    print(f"rescore: mean val_bpb {mean:.6f}" + (f", sd {sd:.6f} over {len(scores)} pairs" if sd else ""))
    with open(ARMS / "rescores.jsonl", "a") as f:
        f.write(json.dumps({"arm": args.arm, "experiment": args.experiment, "what": what, "at": time.time(),
                            "train_py_sha256": hashlib.sha256(source).hexdigest(), "data": data,
                            "gpus": os.environ["AR_GPU"], "val_bpb": scores, "mean": mean}) + "\n")


if __name__ == "__main__":
    main()
