# Post-training example: GSM8K

A real post-training loop: the agent **writes the training code** (data prep, SFT,
rejection sampling, …) and fine-tunes a small base model (`Qwen/Qwen3-4B-Base`) to do well
on [GSM8K](https://github.com/openai/grade-school-math) (grade-school math word problems).
Each iteration trains a model and scores it with a frozen eval; a metric gate rejects
any run that scores more than 10% below the last accepted one.

The setting is inspired by [PostTrainBench](https://posttrainbench.com/) ("can AI agents
improve the performance of base LLMs?"): a base model, one GPU, a time budget, the agent
writes the training code and curates data, and a frozen benchmark it cannot touch does the
grading.

The same approach scales up to much larger models on multi-node clusters; see the
[ML Experiments Guide](../../docs/ml-experiments.md).

## How it's wired

Unlike the code-optimization examples, **there is no single editable file** — the agent
authors the training scripts itself (`train_sft.py`, `gen_rft.py`, …) across iterations.
Scope is `frozen:`-only, protecting just the eval harness, which ships with the example:

```
posttrain/
├── autohelix/posttrain/  # frozen — like all of autohelix/
│   └── config.yaml       # config: goal, gates (model check, metric gate), eval observe entry
├── config.sh             # paths (ARTIFACTS / UV_ENV / HF_HOME) — EDIT before running
├── .gitignore            # training by-products (checkpoints, wandb/, *.safetensors, …)
├── run_native.sh         # frozen — env + GPU pin entrypoint; run everything through this
├── run_eval.sh           # frozen — official scorer; prints ##autohelix[score=...]
├── evaluate.py           # frozen — inspect_ai GSM8K eval (chat template: the base model's)
├── check_final_model.sh  # frozen — gate: a loadable model exists at the fixed path
├── gpu_device.txt        # frozen — which GPU index to pin
└── (train_*.py, etc.)    # AGENT-WRITTEN each run — not in the repo
```

- **Agent-written:** the training code, iterated across runs.
- **Frozen (never edited):** everything listed above — the agent tunes the recipe, never
  the scorer.
- **Artifacts live outside the project:** checkpoints and `final_model/` go to the fixed
  `$ARTIFACTS` path from `config.sh`; the eval reads exclusively from there. This is
  required: checks run in a fresh copy of the candidate code. A model the agent wrote
  inside its own copy is either ignored by git, and deleted with that copy, or not, and
  committed with the iteration (`.gitignore` ignores the usual training by-products).
  Either way it isn't where the eval looks. If `$ARTIFACTS` is left at its in-repo
  default, `check_final_model.sh` fails with an error saying so.
- **The eval's chat template is the base model's,** read from the Hugging Face cache
  each time (`evaluate.py --chat-template-model`), so the prompt format stays fixed
  whatever the agent's model ships. It isn't kept in this folder: a copy holds only
  committed files, and the template isn't ours to commit.

## SETUP — required before this can run

> [!IMPORTANT]
> This directory is **not** ready for AutoHelix yet (no git repo), and the training
> scripts are agent-written (not included). Do not run `autohelix run` here until the
> steps below are done.

🚧 **TODO — to make this runnable:**

1. **Edit `config.sh`** — set `ARTIFACTS` (a fixed absolute path OUTSIDE this repo for
   checkpoints + `final_model/`), `UV_ENV` (the prebuilt env, also OUTSIDE this repo:
   it has no default, and `run_native.sh` refuses to run without it), and `HF_HOME`.
   Each is marked `TODO(setup)` in the file.

2. **Build the environment once** (do not let the agent install packages). The reference
   setup uses [uv](https://github.com/astral-sh/uv) to build the env that `run_native.sh`
   activates:

   ```bash
   # sketch — adapt to your box; must match the UV_ENV path in config.sh
   uv venv /data/posttrain/uv_env
   uv pip install --python /data/posttrain/uv_env \
     vllm torch transformers trl peft accelerate datasets flash_attn inspect_ai
   # the eval also needs the GSM8K task definitions:
   uv pip install --python /data/posttrain/uv_env inspect_evals
   ```

3. **Pre-cache** the base model (`Qwen/Qwen3-4B-Base`) and the GSM8K dataset into the HF
   cache so nothing is downloaded mid-iteration (the eval reads the base model's chat
   template from there and never downloads it).

4. **Set the GPU** in `gpu_device.txt` (defaults to `0`), and confirm `BASE_MODEL` in
   `run_eval.sh` matches the goal in `autohelix/posttrain/config.yaml`.

Sanity-check the harness before handing it to the agent:

```bash
bash run_eval.sh        # should score the BASE model (the baseline) and print a score
```

Then commit a clean tree and start the loop:

```bash
git init && git add -A && git commit -m "Initial baseline"   # autohelix requires a git repo
autohelix run -n 20 --time 10h   # 20 iterations; none starts after 10 hours
```
