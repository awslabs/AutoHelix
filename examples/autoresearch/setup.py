#!/usr/bin/env python3
"""Set up the autoresearch example: upstream at a pinned commit, its environment and
data on local disk, and the AutoHelix project.

    python examples/autoresearch/setup.py PROJECT [--local DIR] [--heldout DIR]

1. Fetches karpathy/autoresearch at UPSTREAM_COMMIT into DIR/upstream and checks the
   sha256 of the files used (UPSTREAM_SHA256).
2. Builds the environment from upstream's uv.lock: DIR/venv, with uv's own Python in
   DIR/python (`uv` on PATH, or --uv).
3. Caches FlashAttention-3 at FA3_REVISION in DIR/hf (the Hugging Face cache training and
   scoring use, offline).
4. Runs upstream's prepare.py: 10 training shards, the validation shard (shard_06542) and
   the tokenizer, in DIR/autoresearch/{data,tokenizer}.
5. Lays out PROJECT (a new folder): template/, upstream's train.py (with
   patches/train.patch) and prepare.py, and ar/paths.sh naming the folders above; then
   `git init` and a first commit.
With --heldout DIR, also downloads one more shard at a random index (not printed) to
DIR/test.parquet, DIR private (0700): the test shard the operator scores after a run.

Every step is skipped when its result is already there, so it can be re-run. DIR must be
on local disk: on a network file system (Lustre) data reads and late imports stall
training. Needs git, patch, uv, an NVIDIA Hopper GPU (H100/H200: train.py's FA3 build)
and network access during setup only.
"""

import argparse
import hashlib
import os
import secrets
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
UPSTREAM_REPO = "https://github.com/karpathy/autoresearch"
UPSTREAM_COMMIT = "228791fb499afffb54b46200aca536f79142f117"
UPSTREAM_SHA256 = {
    "train.py": "2954175f4ac42ad65164aef40910ef953789abcd05a5cc886ac9ba5a00814414",
    "prepare.py": "4f2ba9cbb8ba8c4a3d35be405a913e2f3be3af9aea103ed52ef7b2a662058150",
    "pyproject.toml": "675c150a9e0769f0e39a43eb7d836934266fa348d6e2403521f1ff99f9b9f1af",
    "uv.lock": "03174c5cce6387418c5b6cc9bbe8f71ad0ae1e1d6fedeaecae5cdcf7321da0a3",
}
FA3_REPO = "varunneal/flash-attention-3"
FA3_REVISION = "de87b9b5af06dd9984df595bef90b2eba44b181a"  # as ar/score.py pins it
SHARD_URL = "https://huggingface.co/datasets/karpathy/climbmix-400b-shuffle/resolve/main/shard_{:05d}.parquet"
TRAIN_SHARDS, VAL_SHARD = 10, 6542


def run(*args, **kwargs):
    print("+", " ".join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], check=True, **kwargs)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch_upstream(folder):
    if not (folder / ".git").is_dir():
        folder.mkdir(parents=True, exist_ok=True)
        run("git", "init", "-q", folder)
        run("git", "-C", folder, "fetch", "-q", "--depth", "1", UPSTREAM_REPO, UPSTREAM_COMMIT)
        run("git", "-C", folder, "checkout", "-q", "FETCH_HEAD")
    for name, expected in UPSTREAM_SHA256.items():
        if sha256(folder / name) != expected:
            sys.exit(f"setup: {folder / name} doesn't match upstream {UPSTREAM_COMMIT[:7]} (sha256)")
    print(f"upstream: {UPSTREAM_COMMIT[:7]} in {folder}, sha256 checked")


def build_env(local, uv):
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(local / "venv"),
               UV_PYTHON_INSTALL_DIR=str(local / "python"), UV_LINK_MODE="copy")
    run(uv, "sync", "--frozen", "--no-install-project", "--managed-python", cwd=local / "upstream", env=env)
    python = (local / "venv" / "bin" / "python").resolve()
    if local / "python" not in python.parents:
        sys.exit(f"setup: the venv's Python is {python}, outside {local / 'python'} (the sandbox binds only that)")


def cache_fa3(local):
    model = local / "hf" / "hub" / ("models--" + FA3_REPO.replace("/", "--"))
    if not (model / "snapshots" / FA3_REVISION).is_dir():
        code = f"from kernels import get_kernel; get_kernel({FA3_REPO!r}, revision={FA3_REVISION!r})"
        run(local / "venv" / "bin" / "python", "-c", code, env=dict(os.environ, HF_HOME=str(local / "hf")))
    # train.py asks for the default branch; offline, that resolves through refs/main
    (model / "refs").mkdir(exist_ok=True)
    (model / "refs" / "main").write_text(FA3_REVISION)
    print(f"FA3: {FA3_REPO}@{FA3_REVISION[:8]} in {local / 'hf'}")


def prepare_data(local):
    home = local / "prepare-home"  # prepare.py writes to ~/.cache/autoresearch
    (home / ".cache").mkdir(parents=True, exist_ok=True)
    (local / "autoresearch").mkdir(exist_ok=True)
    link = home / ".cache" / "autoresearch"
    if not link.is_symlink():
        link.symlink_to(local / "autoresearch")
    run(local / "venv" / "bin" / "python", local / "upstream" / "prepare.py", "--num-shards", TRAIN_SHARDS,
        cwd=local / "upstream", env=dict(os.environ, HOME=str(home)))


def lay_out(project, local):
    if project.exists() and any(project.iterdir()):
        sys.exit(f"setup: {project} exists and isn't empty")
    shutil.copytree(HERE / "template", project, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("train.py", "prepare.py"):
        shutil.copy(local / "upstream" / name, project / name)
    run("patch", "-s", "-p1", "-i", HERE / "patches" / "train.patch", cwd=project)
    (project / "ar" / "paths.sh").write_text(f"""\
# Written by setup.py: the environment and data, on local disk.
AR_VENV={local}/venv
AR_PYTHON={local}/venv/bin/python
AR_PYTHON_HOME={local}/python
AR_HF_HOME={local}/hf
AR_DATA={local}/autoresearch/data
AR_TOKENIZER={local}/autoresearch/tokenizer
""")
    (project / ".gitignore").write_text("__pycache__/\n")
    run("git", "init", "-q", project)
    run("git", "-C", project, "add", "-A")
    run("git", "-C", project, "commit", "-q", "-m", "autoresearch at upstream 228791f, patched")


def heldout_shard(folder):
    """One more shard, at a random index outside the training and validation shards."""
    target = folder / "test.parquet"
    if target.exists():
        print(f"heldout: {target} exists")
        return
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    index = TRAIN_SHARDS + secrets.randbelow(VAL_SHARD - TRAIN_SHARDS)
    partial = folder / "test.parquet.part"
    with urllib.request.urlopen(SHARD_URL.format(index), timeout=60) as response, open(partial, "wb") as f:
        shutil.copyfileobj(response, f)
    partial.rename(target)
    print(f"heldout: a test shard at a random index (not printed) in {target}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("project", type=Path, help="the project folder to create")
    parser.add_argument("--local", type=Path, default=Path.home() / "ar-local",
                        help="environment and data, on local disk (default: ~/ar-local)")
    parser.add_argument("--uv", default="uv", help="the uv executable")
    parser.add_argument("--heldout", type=Path, help="also download a secret test shard here")
    args = parser.parse_args()
    local, project = args.local.expanduser().resolve(), args.project.expanduser().resolve()
    fetch_upstream(local / "upstream")
    build_env(local, args.uv)
    cache_fa3(local)
    prepare_data(local)
    lay_out(project, local)
    if args.heldout:
        heldout_shard(args.heldout.expanduser().resolve())
    print(f"""
Project ready: {project}
Next, on the GPU box (J, K = two free GPUs, as nvidia-smi numbers them):
  cd {project}
  AR_GPU=J bash {HERE}/tests/check_sandbox.sh .   # the sandbox's restrictions
  AR_GPU=J,K bash ar/smoke.sh                     # train.py runs and exports (about 70 s)
  AR_GPU=J,K bash ar/train_and_score.sh           # two trainings and their score (about 7 min)
  AR_GPU=J,K autohelix run -n 60 --time 24h""")


if __name__ == "__main__":
    main()
