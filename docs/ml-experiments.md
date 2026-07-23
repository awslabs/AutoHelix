# ML Experiments Guide

Tips for running AutoHelix on **training/evaluation loops** (post-training a model,
optimizing a benchmark score) rather than pure code optimization. These are the
patterns that matter when each iteration trains a model and scores it against a
benchmark. The `posttrain` example is a full working reference.

## Layout

- **Write artifacts to a fixed absolute path.** Models, checkpoints, and eval
  outputs must go to a fixed *absolute* path — outside the repo is simplest (e.g.
  `/data/artifacts/<task>/final_model`), but an in-repo path works too **if it's
  gitignored**. A worktree-relative path (e.g. `outputs/`) is destroyed when the
  iteration's worktree is cleaned up; a non-gitignored in-repo path trips the
  "Main repo has uncommitted changes" rejection. Note that a rejected iteration
  rolls back *code* but not your external artifacts — so back up the current best
  before a risky experiment.
- **Evaluate from that fixed path.** The eval script should read the model from the
  same absolute artifacts path, independent of the current working directory.
- **Report the score in a parseable form.** AutoHelix reads the metric from your
  eval command's stdout. It recognizes `##autohelix[score=<number>]` (unambiguous,
  recommended) or a plain `score: <number>` / `score=<number>` line matching the
  metric name. Without a recognizable value, the iteration may record nothing (or
  the wrong number). Use the metric command's `capture:` to also save training logs,
  eval JSON, etc. for later inspection.
- **Freeze the eval harness.** Put the scorer, eval driver, run scripts, and prompt
  templates in `scope.frozen`. The agent should write *training* code only — never
  the thing that grades it (otherwise it can "fix" the eval to inflate the score).

## Environment

These are recommendations, not requirements — they save iteration time and avoid
mid-run surprises.

- **Pre-install all dependencies; tell the agent not to install anything.** Build
  the env once, list what's available in the goal prompt. Saves iteration time and
  avoids version drift mid-run.
- **Pre-cache datasets and the base model.** Download them before the run so the
  agent doesn't spend iteration time fetching. For gated datasets, set up the access
  token in the env's cache ahead of time.
- **Run through a setup wrapper.** A wrapper script (`run_native.sh` in the example)
  that activates the env (and, where relevant, pins the assigned GPU and sets the HF
  cache) saves the agent from a bare `python` that misses the prepared setup.

## Run discipline

- **Train in the foreground — no `nohup`, `&`, `setsid`, or detaching.** Backgrounding
  returns control to the agent immediately, so it can end its turn while training is
  still running — then the eval scores a half-written model (and the orphaned run
  keeps burning the GPU). Foreground keeps the agent's turn open until training
  finishes.
- **Consider `budget.iteration_time` for long runs.** Instead of a hard mid-step
  kill, it injects remaining-time into the agent's context so it can wrap up and
  save notes before the deadline. Supported for Claude Code, Codex CLI >= 0.141.0,
  and OpenCode.

## Copy-paste goal-prompt rules

A starting point — drop these into a cell's `autohelix.yaml` `goal:` and adapt
freely. (See `posttrain` for these in context.)

```
## Rules
- Operate autonomously; never ask the user for feedback.
- Run training in the FOREGROUND and WAIT for it to finish. Do NOT use nohup, &,
  setsid, or otherwise background/detach — when you exit, the iteration ends and a
  detached run keeps going, so the eval scores a half-written model.
- Do NOT install packages. The environment is prebuilt; use what is provided.
- Save your final model ONLY to the fixed artifacts path: <ABSOLUTE_PATH>/final_model
  (and checkpoints under <ABSOLUTE_PATH>/checkpoints). Do not save elsewhere.
- Do not modify the eval harness or any frozen file.
- Do not train on the benchmark's test data (contamination). Train only on the
  train split or other non-contaminating external data.
- After each experiment, write detailed notes (what you tried, the recipe, the score) so
  the next iteration builds on it.
- To check remaining time, run: bash "$AUTOHELIX_TIME_LEFT_SCRIPT". Wrap up and save
  before the deadline.
```
