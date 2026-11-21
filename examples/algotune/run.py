#!/usr/bin/env python3
"""Run AlgoTune tasks in containers, one task per 8-core set (Linux host with Docker).

    python3 run.py data --tasks svm kmeans --split dev --seed 1000 --out ~/algotune/data
    python3 run.py run --tasks-file pilot.txt --arm autohelix --arg 5 \\
        --data ~/algotune/data --out ~/algotune/runs

`run` keeps up to --slots containers going, each on its own set of physical cores (their
hyperthread siblings stay idle), with no GPU, 32 GB of memory and the dev instances of its
task. The containers use the Docker network --network, which must not reach the instance
metadata endpoint (checked before starting):

    docker network create --subnet 172.30.0.0/16 algotune
    sudo iptables -I DOCKER-USER -s 172.30.0.0/16 -d 169.254.169.254 -j DROP

The agent reaches Bedrock with a short-term Bedrock API key minted here from the host's
credentials (pip install aws-bedrock-token-generator). A key lives at most as long as the
instance-role credentials that signed it (hours), so each container gets a private folder,
mounted read-only at /run/bedrock, whose `token` gets a freshly minted key every
KEY_REFRESH_S; the container's claude-bedrock (its $AUTOHELIX_CLAUDE_CODE_CMD) reads it at
every start, and AutoHelix starts Claude Code anew each iteration. Run this as uid 1000 (the
containers' `agent`, which reads the key file).

The `goal` arm is one `claude -p` session, which keeps the key it started with: Claude Code
reads AWS_BEARER_TOKEN_BEDROCK once and doesn't refresh it (awsAuthRefresh and
awsCredentialExport handle SigV4 credentials, apiKeyHelper Anthropic API keys), so a goal
session fails once its key expires (in the pilot, about 6 hours after it was minted), and
a goal run should be budgeted to end before that.

A task whose output folder has `done` is skipped; delete the folder to redo it.
"""

import argparse
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

AGENT_IMAGE, EVAL_IMAGE = "algotune-agent", "algotune-eval"
AGENT_UID = 1000  # the containers' `agent`
KEY_REFRESH_S = 30 * 60


def core_sets(slots: int, size: int) -> list[str]:
    """Sets of physical cores: cpu N's hyperthread sibling is N + physical count on these hosts."""
    physical = os.cpu_count() // 2
    if slots * size > physical:
        raise SystemExit(f"{slots} slots of {size} cores need more than {physical} physical cores")
    return [f"{i * size}-{i * size + size - 1}" for i in range(slots)]


def tasks_from(args) -> list[str]:
    names = list(args.tasks or [])
    if args.tasks_file:
        names += [line.split("#")[0].strip() for line in Path(args.tasks_file).read_text().splitlines()]
    return [n for n in names if n]


def pool(items: list[str], slots: list[str], job) -> None:
    free: queue.Queue = queue.Queue()
    for slot in slots:
        free.put(slot)

    def work(item: str) -> None:
        slot = free.get()
        try:
            job(item, slot)
        finally:
            free.put(slot)

    threads = [threading.Thread(target=work, args=(item,)) for item in items]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def data(args) -> int:
    out = Path(args.out).expanduser().resolve() / args.split
    out.mkdir(parents=True, exist_ok=True)

    def job(task: str, cpus: str) -> None:
        if (out / task / f"{args.split}.jsonl").is_file():
            print(f"{task}: already there", flush=True)
            return
        done = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--cpuset-cpus", cpus,
             "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{out}:/out",
             "--entrypoint", "python", EVAL_IMAGE, "-I", "/opt/algotune-eval/make_data.py",
             "--task", task, "--split", args.split, "--seed", str(args.seed),
             "--size", str(args.size), "--out", "/out"],
            capture_output=True, text=True)
        status = "ok" if done.returncode == 0 else f"FAILED:\n{done.stderr[-2000:]}"
        print(f"{task}: {status}", flush=True)

    pool(tasks_from(args), slots(args), job)
    return 0


def slots(args) -> list[str]:
    return args.cpus.split(",") if args.cpus else core_sets(args.slots, args.cores)


def bedrock_key(region: str) -> str:
    try:
        from aws_bedrock_token_generator import provide_token
    except ImportError:
        raise SystemExit("pip install --user aws-bedrock-token-generator")
    return provide_token(region=region)


class KeyFolder:
    """A private folder (mount it at /run/bedrock) whose `token` is a Bedrock key minted
    now and again every KEY_REFRESH_S until close(); a failed refresh keeps the old key."""

    def __init__(self, region: str, label: str):
        self.region, self.label = region, label
        self.path = Path(tempfile.mkdtemp(prefix="algotune-bedrock-"))  # 0700, ours
        self.write()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.refresh, daemon=True)
        self.thread.start()

    def write(self) -> None:
        staged = self.path / ".token"
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(bedrock_key(self.region))
        os.replace(staged, self.path / "token")  # the folder is mounted: the new file shows

    def refresh(self) -> None:
        while not self.done.wait(KEY_REFRESH_S):
            try:
                self.write()
            except Exception as error:  # noqa: BLE001 - keep the old key, try again later
                print(f"{self.label}: Bedrock key refresh failed: {error}", flush=True)

    def close(self) -> None:
        self.done.set()
        self.thread.join()
        shutil.rmtree(self.path, ignore_errors=True)


def check_network(network: str) -> None:
    probe = ("import socket\ns = socket.socket()\ns.settimeout(3)\n"
             "try:\n    s.connect(('169.254.169.254', 80))\n    print('REACHABLE')\n"
             "except OSError:\n    print('blocked')\n")
    done = subprocess.run(["docker", "run", "--rm", "--network", network, "--entrypoint", "python",
                           AGENT_IMAGE, "-c", probe], capture_output=True, text=True)
    if done.stdout.strip() != "blocked":
        raise SystemExit(f"containers on {network} reach the instance metadata endpoint "
                         f"({done.stdout.strip() or done.stderr.strip()[-300:]}); see this "
                         "script's docstring")


def run(args) -> int:
    out = Path(args.out).expanduser().resolve() / args.arm
    data_dir = Path(args.data).expanduser().resolve() / "dev"
    if os.getuid() != AGENT_UID:
        raise SystemExit(f"run as uid {AGENT_UID}: the containers' agent reads the key file")
    check_network(args.network)
    names = tasks_from(args)
    missing = [t for t in names if not (data_dir / t / "dev.jsonl").is_file()]
    if missing:
        raise SystemExit(f"no dev data for {missing}: run `run.py data --split dev` first")

    def job(task: str, cpus: str) -> None:
        work = out / task if args.arm != "best" else out / task / f"attempt-{args.attempt}"
        if (work / "done").exists():
            print(f"{task}: done already", flush=True)
            return
        work.mkdir(parents=True, exist_ok=True)
        started = time.time()
        key = KeyFolder(args.region, task)
        try:
            with open(work / "container.log", "a") as log:
                done = subprocess.run(
                    ["docker", "run", "--rm", "--name", f"algotune-{args.arm}-{task}-{os.getpid()}",
                     "--network", args.network, "--cpuset-cpus", cpus, "--memory", args.memory,
                     "-e", f"ANTHROPIC_MODEL={args.model}", "-e", f"AWS_REGION={args.region}",
                     "-v", f"{key.path}:/run/bedrock:ro",
                     "-v", f"{data_dir / task}:/data-in/{task}:ro", "-v", f"{work}:/work",
                     AGENT_IMAGE, task, args.arm, str(args.arg)],
                    stdout=log, stderr=subprocess.STDOUT)
        finally:
            key.close()
        minutes = (time.time() - started) / 60
        (work / "done").write_text(f"exit {done.returncode}, {minutes:.1f} min, cores {cpus}\n")
        print(f"{task}: exit {done.returncode} after {minutes:.1f} min", flush=True)

    pool(names, slots(args), job)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("data", "run"):
        sub = commands.add_parser(name)
        sub.add_argument("--tasks", nargs="*")
        sub.add_argument("--tasks-file")
        sub.add_argument("--slots", type=int, default=6)
        sub.add_argument("--cores", type=int, default=8)
        sub.add_argument("--cpus", help="explicit core sets instead, e.g. 0-7,16-23")
    commands.choices["data"].add_argument("--split", choices=("dev", "test"), required=True)
    commands.choices["data"].add_argument("--seed", type=int, required=True)
    commands.choices["data"].add_argument("--size", type=int, default=100)
    commands.choices["data"].add_argument("--out", required=True)
    run_parser = commands.choices["run"]
    run_parser.add_argument("--arm", choices=("autohelix", "goal", "best"), required=True)
    run_parser.add_argument("--arg", required=True,
                            help="autohelix: iterations; goal: max budget in USD; best: unused")
    run_parser.add_argument("--attempt", type=int, default=1, help="best: attempt number")
    run_parser.add_argument("--data", required=True)
    run_parser.add_argument("--out", required=True)
    run_parser.add_argument("--model", default="us.anthropic.claude-opus-5-5")
    run_parser.add_argument("--region", default="us-east-1")
    run_parser.add_argument("--memory", default="32g")
    run_parser.add_argument("--network", default="algotune")
    args = parser.parse_args()
    return data(args) if args.command == "data" else run(args)


if __name__ == "__main__":
    sys.exit(main())
