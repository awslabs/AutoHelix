#!/usr/bin/env python3
"""Each run's experiments with cumulative API cost, val_bpb and the best so far.

    python3 arms/collect.py [--run NAME ...] [--json FILE]

Runs, by name (container.sh's): AutoHelix runs (autohelix, autohelix-2, ...: the latest
AutoHelix run's history in projects/ar-NAME, every iteration, rejected ones included,
with its own cost) and session runs (karpathy, goal, ...: arms/runs/NAME/experiments.jsonl,
the costs from the session's stream-json: each API message's usage, priced at Opus 5.5's
rates and scaled so the total is the CLI's own total_cost_usd, summed up to the
experiment's end). "best" is what the run delivers so far: AutoHelix's accepted tip, else
the lowest val_bpb logged. gpu_h is cumulative GPU-hours of experiments (wall clock x GPUs).

Audits: every run's GPU entry-point calls (arms/runs/NAME/calls.jsonl, written by
container/ar-gpu: smokes, refusals); for the session runs also a project train.py that no
experiment ran, a counter that doesn't match the log, and tool calls that run
ar/train_and_score.sh, touch arms/ or another run's project; for the box, GPU compute
processes that no ar-gpu call started (arms/gpu.jsonl, from gpu_poll.py: all GPUs; see
box_audit). Writes JSON (default arms/collect.json) and prints a table per run.
"""

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

ARMS = Path(__file__).resolve().parent
LOCAL = ARMS.parent
RUNS = ARMS / "runs"  # each run's logs (container.sh)
# Opus 5.5, $ per token: input, output, cache read, cache write 5m, cache write 1h
PRICE = {"input_tokens": 4e-6, "output_tokens": 20e-6, "cache_read_input_tokens": 0.2e-6,
         "ephemeral_5m_input_tokens": 5e-6, "ephemeral_1h_input_tokens": 8e-6}
BUSY_UTIL, BUSY_MIB = 5, 1000  # a GPU in use, for the box audit


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


def autohelix_run(project):
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
    calls, flags = wrapper_calls(project.name[3:])
    # One train_and_score call per iteration whose observe command ran, the baseline's included
    observed = sum(1 for r in records.values() if any(o.get("name") == "train" for o in r.get("observe") or []))
    made = calls.get("train_and_score") or {"calls": 0, "refused": 0}
    if made["calls"] - made["refused"] != observed:
        flags.append({"flag": "train_and_score_count", "text": f"{made['calls'] - made['refused']} train_and_score "
                      f"call(s), but {observed} iteration(s) ran the observe command"})
    return {"run": runs[-1].parent.name, "rows": rows, "total_cost_usd": round(cost, 4),
            "report_cost_usd": round(sum(summary.values()), 4), "calls": calls, "flags": flags}


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


def session_run(name):
    folder = RUNS / name
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
        gpu_h += (e["end"] - e["start"]) * len(e["gpus"].split(",")) / 3600
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
    summary, flags = wrapper_calls(name)
    return {"rows": rows, "session": session, "calls": summary,
            "flags": flags + audit(folder, calls, name) + audit_tools(stream, name)}


def wrapper_calls(name):
    """ar-gpu's calls for run NAME: {what: {"calls", "gpu_min", "refused"}}, and a flag per refusal."""
    summary, flags = {}, []
    for c in lines(RUNS / name / "calls.jsonl"):
        s = summary.setdefault(c["what"], {"calls": 0, "gpu_min": 0.0, "refused": 0})
        s["calls"] += 1
        if c["status"] == "refused":
            s["refused"] += 1
            if c["what"] != "run_experiment":  # the cap's refusals are in the session's stop
                flags.append({"flag": "wrapper_refused", "what": c["what"], "at": c["start"],
                              "text": f"ar-gpu {c['what']} refused at {when(c['start'])}"})
        else:
            s["gpu_min"] += (c["end"] - c["start"]) * (1 if c["what"] == "smoke" else 2) / 60
    for s in summary.values():
        s["gpu_min"] = round(s["gpu_min"], 1)
    return summary, flags


def run_gpus():
    """{container: the host GPUs its run owns}, from its ar-gpu logs (calls.jsonl, experiments.jsonl)."""
    owned = {}
    for folder in RUNS.glob("*"):
        for e in lines(folder / "calls.jsonl") + lines(folder / "experiments.jsonl"):
            if e.get("gpus"):
                owned.setdefault(f"ar-{folder.name}", set()).update(int(g) for g in e["gpus"].split(","))
    return owned


def box_audit():
    """GPU use no ar-gpu call of the GPU's own run made (arms/gpu.jsonl): one flag per compute
    process outside a wrapper, or wrapped on a GPU its container's run doesn't own, and one
    per sample of a busy GPU (over BUSY_UTIL % or BUSY_MIB) with no compute process listed.

    Not flagged, as the operator's or as noise:
    - a process outside any container on a GPU while no run used it, i.e. outside the
      first to last sample with a container's process on that GPU (rescoring after a run);
    - a process outside any container seen in one sample only, whose pid an ar-gpu call
      had in earlier samples: it was exiting, and /proc no longer named its ancestors.
    """
    samples = lines(ARMS / "gpu.jsonl")
    used = {}  # GPU: [first, last] sample time with a container's process on it
    for s in samples:
        if any(app.get("container") for app in s.get("apps") or []):
            used.setdefault(s["gpu"], [s["t"], s["t"]])[1] = s["t"]
    owned, flags = run_gpus(), []
    wrapped, seen = set(), {}
    for s in samples:
        if not s.get("apps") and (s["util"] > BUSY_UTIL or s["mem_mib"] > BUSY_MIB):
            flags.append({"flag": "gpu_busy_without_process", "gpu": s["gpu"], "t": s["t"],
                          "text": f"GPU {s['gpu']} busy at {when(s['t'])} ({s['util']}%, {s['mem_mib']} MiB) "
                                  "with no compute process listed"})
        for app in s.get("apps") or []:
            key = (s["gpu"], app["pid"])
            if app.get("wrapper") is not None:
                wrapped.add(key)
                container = app.get("container")
                if container in owned and s["gpu"] not in owned[container]:
                    flags.append({"flag": "gpu_not_owned", "gpu": s["gpu"], "t": s["t"], **app,
                                  "text": f"GPU {s['gpu']}: pid {app['pid']} (ar-gpu {app['wrapper']} in {container}) "
                                          f"at {when(s['t'])}, but {container} owns GPUs {sorted(owned[container])}"})
                continue
            span = seen.setdefault(key, {"gpu": s["gpu"], "pid": app["pid"], "container": app.get("container"),
                                         "from": s["t"], "to": s["t"], "samples": 0, "max_mib": 0,
                                         "was_wrapped": key in wrapped})
            span["to"], span["samples"] = s["t"], span["samples"] + 1
            span["max_mib"] = max(span["max_mib"], app.get("mib") or 0)

    def flagged(span):
        if span["container"]:
            return True
        first, last = used.get(span["gpu"], (None, None))
        during_runs = first is not None and span["from"] <= last and span["to"] >= first
        return during_runs and not (span["samples"] == 1 and span["was_wrapped"])

    return flags + [{"flag": "gpu_process_outside_wrapper", **span,
                     "text": f"GPU {span['gpu']}: pid {span['pid']} ({span['container'] or 'no container'}) not "
                             f"started by ar-gpu, {when(span['from'])}-{when(span['to'])}, {span['samples']} samples, "
                             f"up to {span['max_mib']} MiB"}
                    for span in seen.values() if flagged(span)]


def audit_tools(stream, name):
    """Tool calls worth a look: train_and_score.sh run directly, arms/ or another project touched."""
    flags, own, projects = [], str(LOCAL / "projects" / f"ar-{name}"), re.escape(str(LOCAL / "projects" / "ar-"))
    for event in stream:
        if event.get("type") != "assistant":
            continue
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") != "tool_use":
                continue
            text = json.dumps(block.get("input"))
            rest = text.replace(own, "")
            why = []
            # json.dumps writes a newline or tab as the two characters \n, \t
            if re.search(r"(?:^|[\s;&|(\"]|\\[nt])(?:bash|sh|source|\.)\s+\S*train_and_score\.sh", text):
                why.append("runs train_and_score.sh")
            if re.search(rf"/arms\b|{projects}", rest):
                why.append("touches arms/ or another project")
            if why:
                flags.append({"flag": "tool_call", "tool": block.get("name"), "why": why, "input": text[:300],
                              "text": f"{block.get('name')} call {' and '.join(why)}: {text[:160]}"})
    return flags


def audit(folder, calls, name):
    flags = []
    project = LOCAL / "projects" / f"ar-{name}" / "train.py"
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


def table(name, run):
    out = [f"== {name}" + (f" (AutoHelix run {run['run']})" if "run" in run else "")]
    out.append(f"{'n':>3}  {'end':>8}  {'cost $':>8}  {'GPU h':>6}  {'val_bpb':>8}  {'best':>8}  status")
    for r in run["rows"]:
        bpb = f"{r['val_bpb']:.6f}" if r["val_bpb"] is not None else "-"
        best = f"{r['best']:.6f}" if r["best"] is not None else "-"
        status = r["status"] + (f": {r['failure']}" if r.get("failure") else "")
        out.append(f"{r['n']:>3}  {when(r['end']):>8}  {r['cost_usd']:>8.3f}  {r['gpu_h']:>6.2f}  {bpb:>8}  {best:>8}  {status}")
    if "session" in run:
        s = run["session"]
        out.append(f"session: exit {s['exit_code']}, {s['stop']}, {s['results']} result event(s), "
                   f"${s['total_cost_usd']} (CLI) vs ${s['estimate_usd']} (usage at Opus 5.5 rates), "
                   f"{s['refusals']} refused call(s)")
        if s["goal"]:
            out.append(f"/goal: met={s['goal']['met']} after {s['goal']['checks']} check(s): {s['goal']['reason']}")
    else:
        out.append(f"iterations: ${run['total_cost_usd']}, plus ${run['report_cost_usd']} for the end-of-run report")
    out.append("ar-gpu calls: " + (", ".join(f"{what} {c['calls']} ({c['refused']} refused, {c['gpu_min']} GPU-min)"
                                            for what, c in sorted(run["calls"].items())) or "none"))
    out += [f"FLAG: {f['text']}" for f in run["flags"]] or ["flags: none"]
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run", action="append", metavar="NAME",
                        help="autohelix, autohelix-2, karpathy, goal, ... (default: every run with a project)")
    parser.add_argument("--json", type=Path, default=ARMS / "collect.json")
    args = parser.parse_args()
    data = {}
    default = sorted(p.name[3:] for p in (LOCAL / "projects").glob("ar-*"))
    for name in args.run or default:
        if name.startswith("autohelix"):
            run = autohelix_run(LOCAL / "projects" / f"ar-{name}")
        else:
            run = session_run(name)
        if run is not None:
            data[name] = run
            print(table(name, run) + "\n")
    box = box_audit()
    data["box"] = {"flags": box}
    print("== box (all GPUs, arms/gpu.jsonl)\n" + ("\n".join(f"FLAG: {f['text']}" for f in box) or "flags: none") + "\n")
    args.json.write_text(json.dumps(data, indent=1) + "\n")
    print(f"collect: {args.json}")


if __name__ == "__main__":
    main()
