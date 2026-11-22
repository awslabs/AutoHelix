# Autoresearch: a small GPT trained from scratch in five minutes

[Karpathy's autoresearch](https://github.com/karpathy/autoresearch) as an AutoHelix loop:
the agent edits `train.py`, which trains a small GPT (50M parameters by default) on one
GPU in about five minutes, and the score is the model's bits per byte on held-out text
(`val_bpb`, lower is better). Upstream lets the training script report its own loss and
keep time itself; here both are outside the agent's reach:

- `train.py` ends by exporting the model (`ar.export.save(model)`), and a trusted scorer,
  `ar/score.py`, computes the cross-entropy from the exported graph's logits on the
  validation shard, in a separate process. It rejects archives that could run code when
  loaded, graphs with anything but aten operators and FlashAttention-3, and models that
  aren't causal or mix batch rows.
- Training runs in a sandbox (`ar/sandbox.sh`) and is killed at a fixed wall-clock limit.
- The agent sees the validation scores. The final result is scored again on a test shard
  only the operator has.

Upstream is fetched at a pinned commit (`228791f`, sha256-checked) and patched
(`patches/train.patch`: the seed from `AR_SEED`, the export instead of upstream's
evaluation); nothing of it is vendored here.

There are two ways to use it: one AutoHelix run on your own GPU box (setup, run, score),
and the comparison of three loops in `arms/` (see [Comparing loops](#comparing-loops)).

## What is enforced

| Restriction | How | Not covered |
|---|---|---|
| Only `train.py` changes | `scope: {editable: [train.py]}` | |
| No network | a new network namespace (loopback only) | |
| One GPU | a `/dev` with GPU K's node only (plus `nvidiactl`, `nvidia-uvm`), so `CUDA_VISIBLE_DEVICES` can't widen it; no InfiniBand/EFA devices | GPU memory (no cap) |
| Training shards and tokenizer read-only, no validation shard | per-file read-only binds of every shard but `shard_06542` at `~/.cache/autoresearch/data`, where `prepare.py` looks | |
| Writes only to a scratch folder | a new root: `/usr`, `/etc`, the venv, its Python and the FA3 cache read-only; scratch (also `/tmp` and `HOME`) writable; the old root detached | `/dev/shm` (private, in memory) |
| No view of the rest of the machine | nothing else mounted; a fresh `/proc` with the sandbox's processes only | `/sys` (read-only) and `/etc` |
| Mounts stay | every capability dropped (`setpriv`, `no_new_privs`); a nested namespace can't unmount locked mounts | |
| Wall clock | `timeout -s KILL` on the sandbox: its init dies, and the kernel kills every process in it | CPU and RAM (no cpuset or memory cap) |
| Scorer has no network | `unshare --net` | |
| `train.py` is a training script | `ar/static_check.py`: under 64 KB, allowed imports only, no large literals | |

All of it is unprivileged (user namespaces; no root, no Docker). `tests/check_sandbox.sh`
checks each row on your machine. The agent itself runs unsandboxed as you, so it could
use other GPUs or run the scorer in its session; that is for a look at the run's logs,
and the test shard measures overfitting to validation. The comparison in `arms/` closes
this gap with a container per run.

## Set up

On an H100 or H200 box (train.py's FlashAttention-3 build is Hopper-only) with `git`,
`patch` and [uv](https://github.com/astral-sh/uv), from the AutoHelix checkout:

```bash
python examples/autoresearch/setup.py ~/ar-project --local ~/ar-local --heldout ~/ar-heldout
```

This fetches upstream, builds its locked environment (`~/ar-local/venv`), caches FA3,
downloads the data with upstream's `prepare.py` (10 training shards, the validation shard,
the tokenizer), lays out `~/ar-project` as a git repository, and puts a test shard at a
random index in `~/ar-heldout` (private). Keep `--local` on local disk: on a network file
system, data reads and Python's imports stall training. The project's `ar/paths.sh` names
these folders; nothing else holds a path of the box.

Check it, on two free GPUs J and K (indices as `nvidia-smi` numbers them):

```bash
cd ~/ar-project
AR_GPU=J bash /path/to/examples/autoresearch/tests/check_sandbox.sh . ~/ar-heldout  # each restriction
AR_GPU=J,K bash ar/smoke.sh              # train.py runs and exports (about 70 s, on J)
AR_GPU=J,K bash ar/train_and_score.sh    # two trainings at once and their score (about 7 min)
```

## Run

With AutoHelix and Claude Code (`claude`, or `AUTOHELIX_CLAUDE_CODE_CMD`) installed on the
box, in `tmux`:

```bash
AR_GPU=J,K autohelix run -n 60 --time 24h
```

`AR_GPU` goes to every command and to the agent's session (`ar/smoke.sh` uses its first
GPU), so four runs can share an 8-GPU box. Each iteration is the agent's session
(`agent.timeout: 30m`), the static check, then the two trainings and their scores (about
7 minutes). With one GPU (`AR_GPU=K`) it trains once; raise `improve:` to 0.15%.

## Scoring the test shard

After the run, score its final `train.py` on the test shard. Exports aren't kept, so this
trains it again; use a few seeds for an error bar, and do the same for the baseline (the
run's first commit):

```bash
git worktree add /tmp/ar-tip autohelix/<run-id>      # the run's tip
cd /tmp/ar-tip
for seed in 1 2 3; do
  AR_GPU=K AR_SEED=$seed AR_SCORE_DATA=~/ar-heldout/test.parquet bash ar/train_and_score.sh | grep val_bpb=
done
```

The metric is still printed as `val_bpb`; with `AR_SCORE_DATA` it is the test shard's.

## Time limit and noise

`TIME_LIMIT` in `ar/train_and_score.sh` (380 s) is the only clock that counts: from launch
to exit, imports, `torch.compile`, training and the export included. `prepare.TIME_BUDGET`
(300 s of steps) is upstream's timer inside editable code. 380 s is the patched baseline's
wall clock on an H200 (338–346 s, alone or 8 at once) plus about 10%: an agent may move its
own timer, so the margin is training time it can claim.

The baseline's val_bpb varies by about 0.16% (σ) across seeds, while the same seed repeats
within about 0.03%. Seeds are fixed (42 and 43 unless `AR_SEED` is set), so a change that
leaves the random stream alone (learning rates, schedules) is judged sharply, and one that
redraws it (shapes, initialization) gets the full seed noise. Two trainings per attempt
halve that variance; `improve: 0.1%` then passes a no-effect, stream-redrawing change about
27% of the time and a real 0.3% gain about 89% of the time. Rescoring with fresh seeds and
the test shard measures what is real. Seeds 42 and 43 happen to be about 0.2% lucky for the
baseline, so a stream-changing change has to beat that too.

## Comparing loops

`arms/` runs the same task under three loops on one 8-GPU box: AutoHelix (this
`autohelix/autoresearch/config.yaml`), Karpathy's `program.md` loop (`arms/karpathy/`, upstream's text adapted
to the experiment tool) and Claude Code's `/goal` (`arms/goal/`, this goal text). Each gets
the same model (Claude Opus 5.5 on Bedrock), scorer and budget of 60 experiments of two
trainings in 24 h, two runs each. The session loops also stop at $150 of API cost
(`--max-budget-usd`); AutoHelix gets no `--cost`.

A pilot without isolation was audited first: agents there used GPUs outside their pair
and one run read another's live state through `ps`. So now each run has a Docker container
of its own (`arms/container.sh`, image `arms/container/`), and the GPUs are reached only
through fixed entry points:

- **The container** sees only its project, the environment (read-only), data the agent's
  user can't open, its own processes, `HOME` and `/tmp`, and only its two GPUs.
- **The wrappers:** the GPU device nodes are open only to root and a training user. The
  agent's user reaches them through `sudo ar-gpu smoke | train_and_score |
  run_experiment` (`arms/container/ar-gpu`), which take nothing from the agent but
  `train.py` and run the base commit's scripts. `ar/smoke.sh` and `ar/train_and_score.sh`
  call them in a container. `train_and_score` runs only for AutoHelix's harness, and
  `run_experiment` (the other loops' tool) counts and logs every call where the agent
  can't write, refusing past 60.
- **The audit:** `arms/check_container.sh` checks a running container; `arms/gpu_poll.py`
  records every GPU process and the entry point it came from; `arms/collect.py` tabulates
  the runs (cost, val_bpb, GPU time, entry-point calls) and flags what looks wrong.

### Reproducing it

The host needs Docker, the NVIDIA container toolkit (`nvidia-container-cli`), Amazon Linux
2023 (the image's base, so the host-built venvs run in it) and an instance role with
Bedrock access (`AWS_REGION`, default `us-west-2`). The arms' scripts find everything
relative to the folder `arms/` is in, setup's `--local` folder:

```
~/ar-local/
├── projects/autoresearch/   # setup.py's project: every run starts from its first commit
├── arms/                    # a copy of examples/autoresearch/arms; the runs' state goes here
├── claude-home/.local/bin/claude   # Claude Code's native build
├── autohelix-venv/          # a venv with AutoHelix, on a uv Python in python/ (setup's)
└── logs/
```

```bash
python examples/autoresearch/setup.py ~/ar-local/projects/autoresearch --local ~/ar-local --heldout ~/ar-heldout
cp -r examples/autoresearch/arms ~/ar-local/arms
UV_PYTHON_INSTALL_DIR=~/ar-local/python uv venv --managed-python --python 3.12 ~/ar-local/autohelix-venv
uv pip install --python ~/ar-local/autohelix-venv/bin/python .   # AutoHelix, from the checkout (the runs below: 9a4f0b4)
HOME=~/ar-local/claude-home bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
cd ~/ar-local && mkdir -p logs
docker build --build-arg AGENT_UID=$(id -u) -t ar-arm arms/container
bash arms/reset_projects.sh                     # six fresh projects at one base commit
nohup python3 arms/gpu_poll.py > logs/gpu_poll.log 2>&1 &
bash arms/autohelix/launch.sh autohelix 0,1     # autohelix run -n 60 --time 24h
bash arms/autohelix/launch.sh autohelix-2 6,7
nohup bash arms/queue.sh karpathy 2,3 karpathy karpathy-2 > logs/queue-karpathy.log 2>&1 &
nohup bash arms/queue.sh goal 4,5 goal goal-2 > logs/queue-goal.log 2>&1 &
python3 arms/collect.py                         # any time
```

`arms/check_container.sh NAME OTHER...` checks a running container from the host (what its
agent's user can't do). The run's log keeps its refused `train_and_score` call, which
`collect.py` flags, so check a test run's container before the real runs, for example `bash arms/reset_projects.sh ar-test`, `bash
arms/karpathy/launch.sh test 0,1`, `bash arms/check_container.sh test autohelix karpathy
goal`, then `docker rm -f ar-test`, `rm -rf projects/ar-test` and `docker run --rm
--entrypoint rm -v "$PWD/arms/runs:/runs" ar-arm -rf /runs/test` (its logs are root's).

Each run's logs are in `arms/runs/NAME/`; an AutoHelix run's state is in its project's
`.autohelix/`. To score the runs once they end (`arms/rescore.py`: the best `train.py`
in a fresh copy of the base commit, 6 seed pairs on validation, then 3 on the test shard),
with the baseline as an AutoHelix run's iteration 0:

```bash
nohup bash arms/score_chain.sh 0,1 ~/ar-heldout/test.parquet autohelix:0 autohelix autohelix-2 \
    karpathy karpathy-2 goal goal-2 > logs/score_chain.log 2>&1 &
```

### Results

These runs used AutoHelix at commit 9a4f0b4, whose config was `autohelix.yaml` at the
project root (the settings of `autohelix/autoresearch/config.yaml` now; the goal wording
changed since, see the end of this section). Both AutoHelix
runs ended 4.83% and 4.81% below the baseline on the test shard (3 seed pairs; the
baseline is 0.99358), `/goal` 4.28% and 4.25%, and `program.md` 2.30% and
2.26%. The API cost (without AutoHelix's end-of-run report) was $27–36 per AutoHelix run,
$36–37 per `/goal` run and $20–45 per `program.md` run. Test and validation scores agree
within 0.0006 for every run, and the audit found no breaches. The [repository
README](../../README.md#results) plots the improvement against cost
(`assets/autoresearch_cost-light.png`). These runs' prompts said "In your session the GPUs
are reachable only through" smoke (and the experiment tool), without "Don't run full
trainings in your session".

### Cost and tokens

The loops cost about the same, but spend it differently (API tokens per run, from the
session logs; Opus 5.5 prices per million: cache read $0.20, cache write $5, output $20):

| Run | Model calls | Context per call (median) | Input | of it cache reads | Cache writes | Output | Reads / writes / output |
|---|---|---|---|---|---|---|---|
| AutoHelix | 919 | 36k | 33.9M | 31.8M | 2.1M | 0.51M | $6 / $10 / $10 |
| AutoHelix | 1,357 | 39k | 52.7M | 50.0M | 2.7M | 0.60M | $10 / $14 / $12 |
| `/goal` | 102 | 111k | 11.8M | 5.1M | 6.7M | 0.12M | $1 / $34 / $2 |
| `/goal` | 114 | 108k | 13.4M | 7.0M | 6.3M | 0.12M | $1 / $32 / $2 |
| `program.md` | 203 | 126k | 27.4M | 19.8M | 7.7M | 0.14M | $4 / $38 / $3 |
| `program.md` | 73 | 69k | 5.2M | 1.5M | 3.7M | 0.05M | $0.3 / $18 / $1 |

- **AutoHelix:** each iteration is a fresh session, so contexts stay small (peaks of 43–49k
  per session, 70k at most). Its calls come seconds apart, so most input is cache reads.
  It reads 3–4× more input and writes 4–5× more output than `/goal`: it spends
  its budget on reading the code, notes and history, and on planning.
- **The session loops:** one conversation grows to 100–250k tokens. An experiment (6–7
  minutes) outlasts the 5-minute prompt cache, so each wake-up rewrites the whole context
  at cache-write price: 85–94% of their cost. The two `program.md` runs, with the same
  setup, cost $20 and $45. The $45 one ran experiments in the background, woke about three
  times per experiment and reprinted its results table each time.

### Known limitations

- **Time wording differs between loops.** `program.md` says "fixed time budget of 5
  minutes", so the Karpathy runs train to 338–348 s; `/goal` and AutoHelix read the 380 s
  limit and train to 365–378 s.
- **Smoke frequency is uncapped.** Each `ar-gpu smoke` call is capped (killed at 180 s),
  but not how many there are, and `train.py` may ignore smoke's 30 s `TIME_BUDGET`. One
  AutoHelix run used 780 GPU-minutes of smoke. `collect.py` reports each run's smoke
  GPU-minutes.
- **Re-rolls.** The same `train.py` scores slightly differently each time (GPU
  nondeterminism, about 0.02%), so an attempt that just missed the gate can be resubmitted
  unchanged for another chance. Two of one AutoHelix run's acceptances came that way. The
  fresh-seed rescoring measures the real score.
- **Credentials.** The agent can use whatever credentials its model client uses (on EC2,
  possibly the machine's whole instance role). Give the containers credentials that allow
  only the model API.

## Files

```
autoresearch/
├── setup.py                 # upstream at 228791f, the environment, data, the project
├── patches/train.patch      # AR_SEED; ar.export.save(model) instead of evaluate_bpb
├── template/                # becomes the project (with upstream's train.py and prepare.py)
│   ├── autohelix/autoresearch/config.yaml   # the experiment (`autohelix run`)
│   └── ar/
│       ├── export.py        # save(model): the export contract
│       ├── score.py         # the trusted scorer (validate, load, probes, bits per byte)
│       ├── sandbox.sh       # the training sandbox
│       ├── train_and_score.sh  # the observe command: sandboxed training, then the scorer
│       ├── smoke.sh         # the agent's quick check
│       └── static_check.py  # the gate on train.py's source
├── tests/
│   ├── test_ar_score.py     # the scorer's tests (GPU, AR_UPSTREAM)
│   └── check_sandbox.sh     # runs sandbox_check.py in the sandbox: each restriction
└── arms/                    # the comparison of loops
    ├── container.sh, container/   # a run's container: image, entrypoint, ar-gpu wrapper
    ├── autohelix/, karpathy/, goal/   # each loop's launcher (and prompt)
    ├── reset_projects.sh, queue.sh    # fresh projects; runs one after another
    ├── check_container.sh, gpu_poll.py, collect.py   # the audit
    └── rescore.py, score_chain.sh     # scoring again, on fresh seeds and the test shard
```

`tests/test_ar_score.py` needs a CUDA GPU, upstream's environment and `AR_UPSTREAM` (the
folder with upstream's `train.py` and `prepare.py`, e.g. `~/ar-local/upstream`); elsewhere
it isn't collected. The ClimbMix data's license has not been checked for redistribution;
setup downloads it from Hugging Face as upstream does.
