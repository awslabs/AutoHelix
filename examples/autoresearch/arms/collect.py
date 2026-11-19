#!/usr/bin/env python3
"""Each arm's experiments with cumulative API cost, val_bpb and the best so far.

    python3 arms/collect.py [--arm NAME ...] [--json FILE]

Arms: autohelix and autohelix-2 (projects/ar-autohelix[-2], the latest run's history:
every iteration, rejected ones included, with its own cost), karpathy and goal
(arms/<arm>/experiments.jsonl, the costs from the session's stream-json: each API
message's usage, priced at Opus 5.5's rates and scaled so the total is the CLI's own
total_cost_usd, summed up to the experiment's end). "best" is what the arm delivers so
far: AutoHelix's accepted tip, else the lowest val_bpb logged. gpu_h is cumulative
GPU-hours of experiments (wall clock x GPUs).

The session arms are also audited: GPU use on the arm's GPUs (arms/<arm>/gpu.jsonl) outside
every logged experiment, a project train.py that no experiment ran, a counter that doesn't
match the log, and tool calls that run ar/train_and_score.sh, touch arms/ other than
running or reading run_experiment.sh, or another arm's project. Writes JSON (default arms/collect.json) and prints a table per arm.
"""

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

ARMS = Path(__file__).resolve().parent
LOCAL = ARMS.parent
# Opus 5.5, $ per token: input, output, cache read, cache write 5m, cache write 1h
PRICE = {"input_tokens": 4e-6, "output_tokens": 20e-6, "cache_read_input_tokens": 0.2e-6,
         "ephemeral_5m_input_tokens": 5e-6, "ephemeral_1h_input_tokens": 8e-6}
BUSY_UTIL, BUSY_MIB, SLACK_S = 5, 1000, 10


def lines(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def autohelix_arm(project):
    runs = sorted((project / ".autohelix" / "runs").glob("*/history.jsonl"))
    if not runs:
        return None
    records = {}
    for record in lines(runs[-1]):
        records[record["iteration"]] = record  # a reused number: the last record
    rows, cost, gpu_h, tip = [], 0.0, 0.0, None
    for n in sorted(records):
        r = records[n]
        cost += (r.get("cost") or {}).get("cost_usd") or 0.0
        train = next((o for o in r.get("observe") or [] if o.get("name") == "train"), {})
        gpu_h += (train.get("duration_s") or 0) * 2 / 3600
        bpb = (r.get("metrics") or {}).get("val_bpb") if train.get("ok") else None
        if r.get("accepted") and bpb is not None:
            tip = bpb
        rows.append({"n": n, "end": r.get("ended_at"), "cost_usd": round(cost, 4), "gpu_h": round(gpu_h, 3),
                     "val_bpb": bpb, "best": tip,
                     "status": r.get("reason") if n == 0 else ("accepted" if r.get("accepted") else "rejected")})
    summary = {u["id"]: u.get("cost_usd") or 0.0 for u in lines(runs[-1].parent / ".internal" / "usage" / "summary.jsonl")}
    return {"run": runs[-1].parent.name, "rows": rows, "total_cost_usd": round(cost, 4),
            "report_cost_usd": round(sum(summary.values()), 4)}


def messages(stream):
    """API messages in the stream: {id: {"t": last timestamp, usage fields...}}, and the result events."""
    msgs, results = {}, []
    for event in stream:
        kind = event.get("type")
        if kind == "result":
            results.append(event)
            continue
        if kind == "assistant":
            m = event.get("message") or {}
            mid, usage = m.get("id"), m.get("usage") or {}
        elif kind == "stream_event":
            e = event.get("event") or {}
            mid = event.get("api_message_id") or (e.get("message") or {}).get("id")
            usage = (e.get("message") or {}).get("usage") or e.get("usage") or {}
        else:
            continue
        if not mid:
            continue
        msg = msgs.setdefault(mid, {})
        if kind == "assistant" and event.get("timestamp"):
            msg["t"] = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
        flat = dict(usage, **(usage.get("cache_creation") or {}))
        if "ephemeral_5m_input_tokens" not in flat and "ephemeral_1h_input_tokens" not in flat:
            flat["ephemeral_5m_input_tokens"] = flat.get("cache_creation_input_tokens") or 0
        for key in PRICE:
            if isinstance(flat.get(key), int):
                msg[key] = max(msg.get(key, 0), flat[key])
    return msgs, results


def session_arm(arm):
    folder = ARMS / arm
    log = lines(folder / "experiments.jsonl")
    calls = [e for e in log if "call" in e]
    if not calls and not (folder / "session.jsonl").exists():
        return None
    msgs, results = messages(lines(folder / "session.jsonl"))
    priced = [(m.get("t"), sum(m.get(k, 0) * p for k, p in PRICE.items())) for m in msgs.values()]
    estimate = sum(c for _, c in priced)
    total = results[-1].get("total_cost_usd") if results else None
    scale = total / estimate if total and estimate else 1.0
    rows, gpu_h, best = [], 0.0, None
    for e in sorted(calls, key=lambda e: e["end"]):
        cost = scale * sum(c for t, c in priced if t is not None and t <= e["end"])
        gpu_h += (e["end"] - e["start"]) * len(str(e.get("gpus", "x,x")).split(",")) / 3600
        bpb = e["metrics"].get("val_bpb") if e["status"] == "ok" else None
        if bpb is not None and (best is None or bpb < best):
            best = bpb
        rows.append({"n": e["call"], "end": e["end"], "cost_usd": round(cost, 4), "gpu_h": round(gpu_h, 3),
                     "val_bpb": bpb, "best": best, "status": e["status"],
                     "failure": e.get("failure")})
    end = lines(folder / "session-end.json")
    last = results[-1] if results else {}
    goal = [e["attachment"] for e in lines(folder / "transcript.jsonl")
            if (e.get("attachment") or {}).get("type") == "goal_status" and not e["attachment"].get("sentinel")]
    session = {
        "exit_code": end[0]["exit_code"] if end else None,
        "wall_s": end[0]["ended"] - end[0]["started"] if end else None,
        "results": len(results),
        "stop": {k: last.get(k) for k in ("subtype", "terminal_reason", "stop_reason", "is_error", "num_turns")},
        "final_text": (last.get("result") or "")[-300:] if isinstance(last.get("result"), str) else None,
        "total_cost_usd": total, "estimate_usd": round(estimate, 4),
        "models": {m: u.get("costUSD") for m, u in (last.get("modelUsage") or {}).items()},
        "refusals": sum(1 for e in log if e.get("refused")),
        "goal": {"met": goal[-1].get("met"), "reason": goal[-1].get("reason"), "checks": len(goal)} if goal else None,
    }
    stream = lines(folder / "session.jsonl")
    return {"rows": rows, "session": session, "flags": audit(folder, calls, arm) + audit_tools(stream, arm)}


def audit_tools(stream, arm):
    """Tool calls worth a look: train_and_score.sh run directly, arms/ or another project touched."""
    flags, own = [], str(LOCAL / "projects" / f"ar-{arm}")
    for event in stream:
        if event.get("type") != "assistant":
            continue
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") != "tool_use":
                continue
            text = json.dumps(block.get("input"))
            rest = re.sub(re.escape(str(ARMS / "run_experiment.sh")), "", text.replace(own, ""))
            why = []
            if re.search(r"(bash|sh|source|\.)\s+\S*train_and_score\.sh", text):
                why.append("runs train_and_score.sh")
            if re.search(r"/arms\b|ar-local/projects/ar-", rest):
                why.append("touches arms/ or another project")
            if why:
                flags.append({"flag": "tool_call", "tool": block.get("name"), "why": why, "input": text[:300],
                              "text": f"{block.get('name')} call {' and '.join(why)}: {text[:160]}"})
    return flags


def audit(folder, calls, arm):
    flags = []
    windows = [(e["start"] - SLACK_S, e["end"] + SLACK_S) for e in calls]
    outside = {}
    for s in lines(folder / "gpu.jsonl"):
        busy = s["util"] > BUSY_UTIL or s["mem_mib"] > BUSY_MIB
        if busy and not any(a <= s["t"] <= b for a, b in windows):
            spans = outside.setdefault(s["gpu"], [])
            if spans and s["t"] - spans[-1]["to"] <= 45:
                span = spans[-1]
                span["to"], span["samples"] = s["t"], span["samples"] + 1
                span["max_util"], span["max_mib"] = max(span["max_util"], s["util"]), max(span["max_mib"], s["mem_mib"])
            else:
                spans.append({"from": s["t"], "to": s["t"], "samples": 1, "max_util": s["util"], "max_mib": s["mem_mib"]})
    for gpu, spans in sorted(outside.items()):
        for span in spans:
            flags.append({"flag": "gpu_outside_experiments", "gpu": gpu, **span,
                          "text": f"GPU {gpu} busy outside every experiment: {when(span['from'])}-{when(span['to'])}, "
                                  f"{span['samples']} samples, up to {span['max_util']}% and {span['max_mib']} MiB"})
    project = LOCAL / "projects" / f"ar-{arm}" / "train.py"
    if project.exists() and calls:
        sha = hashlib.sha256(project.read_bytes()).hexdigest()
        if sha not in {e["train_py_sha256"] for e in calls}:
            flags.append({"flag": "final_train_py_unlogged", "sha256": sha,
                          "text": f"the project's final train.py ({sha[:12]}) is not one any experiment ran"})
    count = (folder / "count").read_text().strip() if (folder / "count").exists() else "0"
    numbers = sorted(e["call"] for e in calls)
    if numbers != list(range(1, len(numbers) + 1)) or str(len(numbers)) != count:
        flags.append({"flag": "counter_mismatch", "text": f"counter at {count}, logged calls {numbers}"})
    return flags


def when(t):
    return datetime.fromtimestamp(t).strftime("%H:%M:%S") if t else "-"


def table(name, arm):
    out = [f"== {name}" + (f" (run {arm['run']})" if "run" in arm else "")]
    out.append(f"{'n':>3}  {'end':>8}  {'cost $':>8}  {'GPU h':>6}  {'val_bpb':>8}  {'best':>8}  status")
    for r in arm["rows"]:
        bpb = f"{r['val_bpb']:.6f}" if r["val_bpb"] is not None else "-"
        best = f"{r['best']:.6f}" if r["best"] is not None else "-"
        status = r["status"] + (f": {r['failure']}" if r.get("failure") else "")
        out.append(f"{r['n']:>3}  {when(r['end']):>8}  {r['cost_usd']:>8.3f}  {r['gpu_h']:>6.2f}  {bpb:>8}  {best:>8}  {status}")
    if "session" in arm:
        s = arm["session"]
        out.append(f"session: exit {s['exit_code']}, {s['stop']}, {s['results']} result event(s), "
                   f"${s['total_cost_usd']} (CLI) vs ${s['estimate_usd']} (usage at Opus 5.5 rates), "
                   f"{s['refusals']} refused call(s)")
        if s["goal"]:
            out.append(f"/goal: met={s['goal']['met']} after {s['goal']['checks']} check(s): {s['goal']['reason']}")
        out += [f"FLAG: {f['text']}" for f in arm["flags"]] or ["flags: none"]
    else:
        out.append(f"iterations: ${arm['total_cost_usd']}, plus ${arm['report_cost_usd']} for the end-of-run report")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--arm", action="append", help="autohelix, autohelix-2, karpathy, goal, ... (default: every arm with a project)")
    parser.add_argument("--json", type=Path, default=ARMS / "collect.json")
    args = parser.parse_args()
    data = {}
    default = sorted(p.name[3:] for p in (LOCAL / "projects").glob("ar-*"))
    for name in args.arm or default:
        if name.startswith("autohelix"):
            arm = autohelix_arm(LOCAL / "projects" / f"ar-{name}")
        else:
            arm = session_arm(name)
        if arm is not None:
            data[name] = arm
            print(table(name, arm) + "\n")
    args.json.write_text(json.dumps(data, indent=1) + "\n")
    print(f"collect: {args.json}")


if __name__ == "__main__":
    main()
