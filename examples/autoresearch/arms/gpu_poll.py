#!/usr/bin/env python3
"""Poll every GPU of the box for the audit (collect.py reads it), until killed.

    nohup python3 arms/gpu_poll.py > logs/gpu_poll.log 2>&1 &

Every 30 s, one line per GPU in arms/gpu.jsonl: utilization, memory, and each compute process
(nvidia-smi --query-compute-apps) with its container (ar-NAME, from its cgroup) and the
ar-gpu call it belongs to (the subcommand of the ar-gpu among its ancestors: a root process
running /usr/local/sbin/ar-gpu; null: a process no wrapper started).
"""

import json
import re
import subprocess
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent / "gpu.jsonl"
EVERY = 30


def query(*args):
    out = subprocess.run(["nvidia-smi", *args, "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
    return [[f.strip() for f in line.split(",")] for line in out.splitlines() if line.strip()]


def status(pid, key):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(key + ":"):
                return line.split()[1]
    except OSError:
        pass
    return None


def wrapper(pid):
    """The ar-gpu subcommand among pid's ancestors, else None. Only root runs ar-gpu (sudo),
    as `bash /usr/local/sbin/ar-gpu SUBCOMMAND`: an agent can fake the arguments, not the uid."""
    while pid and pid != "1":
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            return None
        if status(pid, "Uid") == "0" and len(argv) > 2 and argv[1] == b"/usr/local/sbin/ar-gpu":
            return argv[2].decode(errors="replace")
        pid = status(pid, "PPid")
    return None


def container(pid, names):
    try:
        found = re.search(r"([0-9a-f]{64})", Path(f"/proc/{pid}/cgroup").read_text())
    except OSError:
        return None
    return names.get(found.group(1), found.group(1)[:12]) if found else None


def main():
    while True:
        t = time.time()
        ps = subprocess.run(["docker", "ps", "--no-trunc", "--format", "{{.ID}} {{.Names}}"],
                            capture_output=True, text=True).stdout
        names = dict(line.split(" ", 1) for line in ps.splitlines() if " " in line)
        gpus = query("--query-gpu=index,uuid,utilization.gpu,memory.used")
        index = {uuid: int(i) for i, uuid, *_ in gpus}
        apps = {}
        for uuid, pid, mib in query("--query-compute-apps=gpu_uuid,pid,used_memory"):
            apps.setdefault(index.get(uuid), []).append(
                {"pid": int(pid), "mib": int(mib) if mib.isdigit() else None,
                 "container": container(pid, names), "wrapper": wrapper(pid)})
        with open(OUT, "a") as f:
            for i, _, util, mem in gpus:
                f.write(json.dumps({"t": round(t, 1), "gpu": int(i), "util": int(util), "mem_mib": int(mem),
                                    "apps": apps.get(int(i), [])}) + "\n")
        time.sleep(max(0, EVERY - (time.time() - t)))


if __name__ == "__main__":
    main()
