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
checks each row on your machine. What's left (the agent itself runs unsandboxed as you, so
it could use other GPUs or run the scorer in its session) is for a post-hoc audit of the
run's logs, and the test shard measures overfitting to validation.

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
system, data reads and Python's imports stall training.

Check it, on two free GPUs J and K (indices as `nvidia-smi` numbers them):

```bash
cd ~/ar-project
AR_GPU=J bash /path/to/examples/autoresearch/tests/check_sandbox.sh .   # each restriction
AR_GPU=J,K bash ar/smoke.sh              # train.py runs and exports (about 70 s, on J)
AR_GPU=J,K bash ar/train_and_score.sh    # two trainings at once and their score (about 7 min)
```

## Run

With AutoHelix and Claude Code (`claude`, or `AUTOHELIX_CLAUDE_CODE_CMD`) installed on the
box:

```bash
AR_GPU=J,K autohelix run -n 60 --time 24h
```

`AR_GPU` goes to every command and to the agent's session (`ar/smoke.sh` uses its first
GPU), so four runs can share an 8-GPU box. Start `autohelix` in `tmux` on the box. Each
iteration is the agent's session (`agent.timeout: 30m`), the static check,
then the two trainings and their scores (about 7 minutes). With one GPU (`AR_GPU=K`) it
trains once; raise `improve:` to about 0.3% (see [Noise](#noise)).

## Scoring the test shard

After the run, score the run's final `train.py` on the test shard. Exports aren't kept, so
this trains it again; use a few seeds for an error bar, and do the same for the baseline:

```bash
git worktree add /tmp/ar-tip autohelix/<run-id>      # the run's tip (the first commit: the baseline)
cd /tmp/ar-tip
for seed in 1 2 3; do
  AR_GPU=K AR_SEED=$seed AR_SCORE_DATA=~/ar-heldout/test.parquet bash ar/train_and_score.sh | grep val_bpb=
done
```

The metric is still printed as `val_bpb`; with `AR_SCORE_DATA` it is the test shard's.

## Time limit

`TIME_LIMIT` in `ar/train_and_score.sh` is the only clock that counts: from launch to
exit, imports, `torch.compile`, training and the export included. `prepare.TIME_BUDGET`
(300 s of steps, the first 10 not counted) is upstream's timer inside editable code.

It is 380 s: the patched baseline's wall clock through `ar/train_and_score.sh` on an H200
was 338–341 s alone or two at once and 343–346 s with all 8 GPUs training (about 40 s of imports,
setup, compile and the first 10 steps, 300 s of counted steps, 2 s of export), plus about
10%. An agent
may move its own timer, so the margin is training time it can claim; a tighter limit
leaves less of it but risks killing a run slowed by its neighbours.

## Noise

The patched baseline through `ar/train_and_score.sh`, scored on the validation shard (H200,
`hotel-3`, November 18, 2026):

| Runs | Seeds (`AR_SEED`) | val_bpb | Wall clock (s) |
|---|---|---|---|
| 8 at once, GPUs 0–7 | 1–8 | 0.992598, 0.993331, 0.994890, 0.993906, 0.993873, 0.995953, 0.993854, 0.992745 | 342.9–345.7 |
| alone, GPU 0 | 1, 2 | 0.992566, 0.993904 | 338.2, 338.6 |
| alone, GPU 5 | 42 | 0.993247 | 341.0 |
| 2 at once (`AR_GPU=0,1`) | 42, 43 | 0.993258, 0.989724 (mean 0.991491) | 339.2, 339.7 |

- **Across seeds:** 8 at once, mean 0.993894 and σ 0.0011 (0.11%); over the 10 seeds,
  mean 0.993413 and σ 0.0016 (0.16%), seed 43 being the low outlier.
- **The same seed repeats closely:** 0.00003, 0.00057 and 0.00001 apart (seeds 1, 2, 42)
  in different runs, step counts and neighbours. Training 8 at once adds about 5 s of
  startup and no visible noise.
- **So it depends on the change.** Seeds are fixed (42 and 43 unless `AR_SEED` is set),
  so a change that leaves the random stream alone (learning rates, schedules) is compared
  almost like with like: noise around 0.03% per training. A change that redraws it
  (model shapes, initialization) gets the full seed noise: the difference of two
  attempts has σ 0.23% with one training each and 0.16% with the mean of two.
- **Two trainings per attempt** (`AR_GPU=J,K`), all at once, so the wall clock stays the
  same. `improve: 0.2%` passes a no-effect change that redraws the stream about 11% of
  the time and a real 0.3% gain about 73% of the time; same-stream changes are judged
  far more sharply. With one training, use `improve: 0.3%`. Revisit after a pilot run.
- The baseline's reference with seeds 42 and 43 (0.991491) is lucky by about 0.2%, seed 43
  being a good draw. Until an accepted change redraws the stream, a stream-changing change
  has to beat that too.

## Files

```
autoresearch/
├── setup.py                 # upstream at 228791f, the environment, data, the project
├── patches/train.patch      # AR_SEED; ar.export.save(model) instead of evaluate_bpb
├── template/                # becomes the project (with upstream's train.py and prepare.py)
│   ├── autohelix.yaml
│   └── ar/
│       ├── export.py        # save(model): the export contract
│       ├── score.py         # the trusted scorer (validate, load, probes, bits per byte)
│       ├── sandbox.sh       # the training sandbox
│       ├── train_and_score.sh  # the observe command: sandboxed training, then the scorer
│       ├── smoke.sh         # the agent's quick check
│       └── static_check.py  # the gate on train.py's source
└── tests/
    ├── test_ar_score.py     # the scorer's tests (GPU, AR_UPSTREAM)
    └── check_sandbox.sh     # runs sandbox_check.py in the sandbox: each restriction
```

`setup.py` also writes `ar/paths.sh`, the absolute paths of the environment and data.
The ClimbMix data's license has not been checked for redistribution; setup downloads it
from Hugging Face as upstream does.
