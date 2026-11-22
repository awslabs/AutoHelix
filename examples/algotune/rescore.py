#!/usr/bin/env python3
"""Rescore each arm's best dev solver on held-out data, in fresh containers (Linux host, Docker).

    python3 rescore.py pick --root ~/algotune/subset --set subset --out ~/algotune/rescore
    python3 rescore.py score --rescore ~/algotune/rescore --tasks kmeans svm \\
        --cpus 0-7,16-23,32-39 --test-data ~/algotune/test-data --dev-data ~/algotune/data

`pick` (on the node that ran the arms; needs collect.py next to it) chooses, per task:
- autohelix@3 / autohelix@5: the accepted iteration with the best dev speedup among the first
  3 / 5 (pilot 2 ran 3);
- best@3 / best@5: the attempt whose iteration was accepted with the best dev speedup among
  attempts 1-3 / 1-5;
- goal: the session's final working tree (tracked and untracked, not ignored files; it often
  left its solution uncommitted).
Each candidate's files go to `solvers/<set>/<task>/<candidate>/` (git archive of the commit, or
the working tree), with `<task>.py` restored from the scaffold's initial commit; a label with
no accepted iteration has no candidate (scored 1x). One line per label in
`manifest-<set>.jsonl`.

`score` evaluates every candidate of the given tasks (all sets' manifests in --rescore):
on test, and the goal candidates on dev too (it has no dev score of its own). A task's
candidates run one after another on one core set (from --cpus, a slot per task), each in a
fresh container with --network none, 32 GB, sharing only the task's reference-cache folder
(`work/<task>/`, mounted as the evaluator's root-only WORK_BASE), so the reference is timed
once per task and split. Results: `results/<set>/<task>/<candidate>-<split>.json` and `.log`;
a result that exists is skipped.
"""

import argparse
import glob
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect  # noqa: E402

IMAGE = "algotune-agent"


def git(project, *args, text=True):
    return subprocess.run(["git", "-C", str(project), "-c", "safe.directory=*", *args],
                          capture_output=True, text=text, check=True).stdout


def export_commit(project: Path, commit: str, dest: Path, task: str) -> None:
    final, dest = dest, dest.with_name(f".tmp-{dest.name}")
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    archive = git(project, "archive", commit, text=False)
    subprocess.run(["tar", "x", "-C", str(dest)], input=archive, check=True)
    restore_task_file(project, commit, dest, task)
    dest.rename(final)


def export_worktree(project: Path, dest: Path, task: str) -> list[str]:
    final, dest = dest, dest.with_name(f".tmp-{dest.name}")
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    skipped = []
    names = git(project, "ls-files", "-co", "--exclude-standard", "-z").split("\0")
    for name in filter(None, names):
        src = project / name
        if src.is_symlink() or not src.is_file():
            skipped.append(name)
            continue
        (dest / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / name)
    restore_task_file(project, "HEAD", dest, task)
    dest.rename(final)
    return skipped


def restore_task_file(project: Path, commit: str, dest: Path, task: str) -> None:
    root = git(project, "rev-list", "--max-parents=0", commit).split()[-1]
    (dest / f"{task}.py").write_text(git(project, "show", f"{root}:{task}.py"))


def best_iteration(runs: list, limit: int):
    """The accepted iteration with the best dev speedup, with `code`: its commit, or for a
    no-op accept (no commit) the last commit before it, or the run's base."""
    found = None
    for run in runs:
        code = run["base"]
        for it in run["iterations"]:
            if it["accepted"] and it["commit"]:
                code = it["commit"]
            if 1 <= it["n"] <= limit and it["accepted"] and it["speedup"]:
                if found is None or it["speedup"] > found["speedup"]:
                    found = {**it, "code": code}
    return found


def pick(args) -> int:
    root = Path(args.root).expanduser()
    out = Path(args.out).expanduser()
    solvers = out / "solvers" / args.set
    rows = []
    for task_dir in sorted(p for p in root.glob("*/*") if p.parent.name in args.arms):
        arm, task = task_dir.parent.name, task_dir.name
        if arm == "autohelix":
            runs = collect.autohelix_runs(str(task_dir))
            for limit in args.limits:
                it = best_iteration(runs, limit)
                row = {"set": args.set, "task": task, "label": f"autohelix@{limit}",
                       "candidate": None, "dev_speedup": None}
                if it:
                    name = f"autohelix-it{it['n']}"
                    row.update(candidate=name, dev_speedup=it["speedup"], commit=it["code"])
                    if not (solvers / task / name).exists():
                        export_commit(task_dir / "project", it["code"], solvers / task / name, task)
                rows.append(row)
        elif arm == "best":
            attempts = []
            for a in sorted(task_dir.glob("attempt-*"), key=lambda p: int(p.name.split("-")[1])):
                it = best_iteration(collect.autohelix_runs(str(a)), 1)
                attempts.append((int(a.name.split("-")[1]), a, it))
            for limit in args.limits:
                chosen = [x for x in attempts if x[0] <= limit and x[2]]
                row = {"set": args.set, "task": task, "label": f"best@{limit}",
                       "candidate": None, "dev_speedup": None,
                       "attempts": len([x for x in attempts if x[0] <= limit])}
                if chosen:
                    number, folder, it = max(chosen, key=lambda x: x[2]["speedup"])
                    name = f"best-a{number}"
                    row.update(candidate=name, dev_speedup=it["speedup"], commit=it["code"])
                    if not (solvers / task / name).exists():
                        export_commit(folder / "project", it["code"], solvers / task / name, task)
                rows.append(row)
        elif arm == "goal":
            row = {"set": args.set, "task": task, "label": "goal", "candidate": "goal",
                   "dev_speedup": None}
            if not (solvers / task / "goal").exists():
                skipped = export_worktree(task_dir / "project", solvers / task / "goal", task)
                if skipped:
                    row["skipped"] = skipped
            rows.append(row)
    path = out / f"manifest-{args.set}-{'-'.join(args.arms)}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"{path}: {len(rows)} labels, "
          f"{len({(r['task'], r['candidate']) for r in rows if r['candidate']})} candidates")
    return 0


def score(args) -> int:
    rescore = Path(args.rescore).expanduser().resolve()
    rows = [json.loads(line) for m in sorted(rescore.glob("manifest-*.jsonl"))
            for line in m.read_text().splitlines() if line.strip()]
    data = {"test": Path(args.test_data).expanduser().resolve() / "test",
            "dev": Path(args.dev_data).expanduser().resolve() / "dev"}
    jobs: dict[str, list] = {}
    for task in args.tasks:
        seen = set()
        for r in rows:
            if r["task"] != task or not r["candidate"] or args.only and r["candidate"] != args.only:
                continue
            splits = ["test", "dev"] if r["candidate"] == "goal" else ["test"]
            for split in splits:
                key = (r["set"], r["candidate"], split)
                if key not in seen:
                    seen.add(key)
                    jobs.setdefault(task, []).append(key)
    missing = [t for t in jobs if not (data["test"] / t / "test.jsonl").is_file()]
    if missing:
        raise SystemExit(f"no test data for {missing}")
    free: queue.Queue = queue.Queue()
    for slot in args.cpus.split(","):
        free.put(slot)

    def run_task(task: str) -> None:
        cpus = free.get()
        try:
            for set_name, candidate, split in jobs[task]:
                one(task, set_name, candidate, split, cpus)
        finally:
            free.put(cpus)

    def one(task, set_name, candidate, split, cpus) -> None:
        out = rescore / args.results / set_name / task
        out.mkdir(parents=True, exist_ok=True)
        result = out / f"{candidate}-{split}.json"
        if result.exists():
            print(f"{task} {set_name} {candidate} {split}: done already", flush=True)
            return
        solver = rescore / "solvers" / set_name / task / candidate
        work = rescore / "work" / task
        work.mkdir(parents=True, exist_ok=True)
        inner = (
            "set -e; chown root:root /var/lib/algotune-eval; chmod 711 /var/lib/algotune-eval; "
            f"mkdir -m 700 /data; cp -r /data-in/{task} /data/; chmod -R go-rwx /data; "
            "cp -r /solver-in /solver; "
            f"python -I /opt/algotune-eval/evaluate.py --task {task} --solver /solver "
            f"--data /data --split {split} --run-as solver --algotune /opt/algotune "
            f"{f'--limit {args.limit} ' if args.limit else ''}--json /out/.{candidate}-{split}.json; "
            f"mv /out/.{candidate}-{split}.json /out/{candidate}-{split}.json")
        started = time.time()
        with open(out / f"{candidate}-{split}.log", "w") as log:
            done = subprocess.run(
                ["docker", "run", "--rm", "--name", f"rescore-{task}-{candidate}-{split}",
                 "--network", "none", "--cpuset-cpus", cpus, "--memory", args.memory,
                 "-v", f"{solver}:/solver-in:ro", "-v", f"{data[split] / task}:/data-in/{task}:ro",
                 "-v", f"{out}:/out", "-v", f"{work}:/var/lib/algotune-eval",
                 "--entrypoint", "sh", IMAGE, "-c", inner],
                stdout=log, stderr=subprocess.STDOUT)
            log.write(f"\nrescore: exit {done.returncode} after "
                      f"{(time.time() - started) / 60:.1f} min on cores {cpus}\n")
        print(f"{task} {set_name} {candidate} {split}: exit {done.returncode} after "
              f"{(time.time() - started) / 60:.1f} min", flush=True)

    threads = [threading.Thread(target=run_task, args=(t,)) for t in args.tasks if t in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    print("RESCORE DONE", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("pick")
    p.add_argument("--root", required=True, help="e.g. ~/algotune/subset")
    p.add_argument("--set", required=True, help="e.g. subset, pilot2")
    p.add_argument("--arms", nargs="+", default=["autohelix", "best", "goal"])
    p.add_argument("--limits", nargs="+", type=int, default=[3, 5])
    p.add_argument("--out", required=True)
    s = commands.add_parser("score")
    s.add_argument("--rescore", required=True)
    s.add_argument("--tasks", nargs="+", required=True, help="in the order to start them")
    s.add_argument("--cpus", required=True, help="core sets, one task at a time on each")
    s.add_argument("--test-data", required=True)
    s.add_argument("--dev-data", required=True)
    s.add_argument("--results", default="results", help="folder under --rescore")
    s.add_argument("--memory", default="32g", help="container memory")
    s.add_argument("--limit", type=int, help="only the first N instances (smoke test)")
    s.add_argument("--only", help="only this candidate (smoke test)")
    args = parser.parse_args()
    return pick(args) if args.command == "pick" else score(args)


if __name__ == "__main__":
    sys.exit(main())
