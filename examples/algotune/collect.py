#!/usr/bin/env python3
"""Collect one arm's results on a node as JSON lines (one per task, or per best-of attempt).

    python3 collect.py ~/algotune/subset autohelix > autohelix.jsonl
    python3 collect.py ~/algotune/subset best      # one line per attempt
    python3 collect.py ~/algotune/subset goal

AutoHelix/best: per iteration (accepted, dev speedup, cost, wall time, whether an instance
failed the prepare-CPU check or the observe was killed (137)), the base and each accepted
commit and the files it changed. goal: goal.json's cost/time/turns, the project's initial
commit, HEAD, uncommitted files and the files changed since the initial commit.
"""

import glob
import json
import os
import re
import subprocess
import sys


def git(project, *args):
    r = subprocess.run(["git", "-C", project, "-c", "safe.directory=*", *args],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def done_line(folder):
    try:
        text = open(os.path.join(folder, "done")).read().strip()
    except OSError:
        return None
    m = re.match(r"exit (-?\d+), ([\d.]+) min", text)
    return {"exit": int(m[1]), "minutes": float(m[2])} if m else {"raw": text}


def read_text(pattern):
    out = ""
    for f in glob.glob(pattern, recursive=True):
        try:
            out += open(f, errors="replace").read()
        except OSError:
            pass
    return out


def autohelix_runs(folder):
    project = os.path.join(folder, "project")
    runs = []
    for run_dir in sorted(glob.glob(os.path.join(project, ".autohelix/runs/*"))):
        hist = os.path.join(run_dir, "history.jsonl")
        if not os.path.exists(hist):
            continue
        iterations = []
        for line in open(hist):
            if not line.strip():
                continue
            r = json.loads(line)
            n = r["iteration"]
            costs = [(c.get("usage") or {}).get("cost_usd") for c in r.get("calls") or []]
            it_dir = os.path.join(run_dir, "iterations", f"{n:03d}")
            out = read_text(os.path.join(it_dir, "observe/**/stdout*"))
            iterations.append({
                "n": n, "accepted": bool(r.get("accepted")),
                "speedup": (r.get("metrics") or {}).get("speedup"),
                "cost": sum(c for c in costs if c is not None),
                "cost_unknown": any(c is None for c in costs),
                "seconds": (r.get("ended_at") or 0) - (r.get("started_at") or 0),
                "commit": r.get("commit"), "reason": r.get("reason"),
                "agent_error": bool(r.get("agent_error")),
                "prepare_invalid": "while receiving the problem" in out,
                "killed_137": "exited 137" in json.dumps(r.get("observe")),
                "observe_error": [o.get("error") for o in r.get("observe") or [] if not o.get("ok")],
                "summary": r.get("summary"),
            })
        run = json.load(open(os.path.join(run_dir, "run.json")))
        base = run.get("base_commit")  # where the run started (rescore: a no-op accept's code)
        for it in iterations:
            if it["commit"]:  # this iteration's files, not everything since the base
                files = git(project, "diff", "--name-only", it["commit"] + "~", it["commit"])
                it["files"] = files.split("\n") if files else []
        summary_cost = 0.0
        for f in glob.glob(os.path.join(run_dir, ".internal/usage/summary.jsonl")):
            for line in open(f):
                if line.strip():
                    summary_cost += json.loads(line).get("cost_usd") or 0
        runs.append({"run": os.path.basename(run_dir), "base": base,
                     "iterations": iterations, "summary_cost": summary_cost})
    return runs


def goal(folder):
    project = os.path.join(folder, "project")
    row = {"done": done_line(folder)}
    try:
        g = json.load(open(os.path.join(folder, "goal.json")))
    except (OSError, ValueError) as e:
        g = {}
        row["goal_json_error"] = str(e)
    for k in ("total_cost_usd", "duration_ms", "num_turns", "subtype", "terminal_reason",
              "stop_reason", "is_error"):
        row[k] = g.get(k)
    result = g.get("result") or ""
    row["result_tail"] = result[-2500:]
    row["claimed"] = re.findall(r"(\d[\d,.]*)\s*[x×]", result)[-8:]
    try:
        row["stderr_tail"] = open(os.path.join(folder, "goal.stderr")).read()[-300:]
    except OSError:
        pass
    first = git(project, "rev-list", "--max-parents=0", "HEAD")
    row["initial"] = first.split("\n")[-1] if first else None
    row["head"] = git(project, "rev-parse", "HEAD")
    row["commits"] = int(git(project, "rev-list", "--count", "HEAD") or 0)
    status = git(project, "status", "--porcelain", "--untracked-files=all")
    row["uncommitted"] = [l for l in (status or "").split("\n") if l][:50]
    if row["initial"]:
        files = git(project, "diff", "--name-only", row["initial"], "HEAD")
        row["files"] = files.split("\n") if files else []
    return row


def main():
    root, arm = os.path.expanduser(sys.argv[1]), sys.argv[2]
    for task_dir in sorted(glob.glob(os.path.join(root, arm, "*"))):
        task = os.path.basename(task_dir)
        if arm == "goal":
            row = {"task": task, "arm": arm, **goal(task_dir)}
            print(json.dumps(row))
        elif arm == "best":
            for a in sorted(glob.glob(os.path.join(task_dir, "attempt-*")),
                            key=lambda p: int(p.rsplit("-", 1)[1])):
                print(json.dumps({"task": task, "arm": arm, "attempt": int(a.rsplit("-", 1)[1]),
                                  "done": done_line(a), "runs": autohelix_runs(a)}))
        else:
            print(json.dumps({"task": task, "arm": arm, "done": done_line(task_dir),
                              "runs": autohelix_runs(task_dir)}))


if __name__ == "__main__":
    main()
