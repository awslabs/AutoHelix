"""Export the comparison's results for make_plot.py, on the GPU box.

    python3 export_results.py LOCAL > results.json

LOCAL is the arms' folder (setup.py's --local, holding arms/ and logs/). Reads
arms/collect.json (arms/collect.py) and the rescoring logs of arms/score_chain.sh:
logs/rescore/NAME-val.log and NAME-test.log for each run, and autohelix-0-val.log and
autohelix-0-test.log (iteration 0 of the AutoHelix run, the base commit) for the baseline.
"""

import json
import re
import sys
from pathlib import Path


def mean(path):
    m = re.search(r"mean val_bpb ([0-9.]+), sd ([0-9.]+) over (\d+)", path.read_text())
    return {"mean": float(m[1]), "sd": float(m[2]), "pairs": int(m[3])}


def scores(logs, tag):
    return {"val": mean(logs / f"{tag}-val.log"), "test": mean(logs / f"{tag}-test.log")}


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__.split("\n\n")[1])
    local = Path(sys.argv[1]).expanduser()
    logs = local / "logs" / "rescore"
    collected = json.loads((local / "arms" / "collect.json").read_text())
    out = {"baseline": scores(logs, "autohelix-0"), "runs": {}}
    for name, run in collected.items():
        if name == "box":
            continue
        rows = [{k: r.get(k) for k in ("n", "end", "cost_usd", "gpu_h", "val_bpb", "best", "status")}
                for r in run["rows"]]
        out["runs"][name] = {"rows": rows,
                             "total_cost_usd": run.get("total_cost_usd") or (run.get("session") or {}).get("total_cost_usd"),
                             "report_cost_usd": run.get("report_cost_usd"), "gpu_calls": run.get("calls"),
                             **scores(logs, name)}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
