# ML Experiments Guide

Tips for running AutoHelix on **training/evaluation loops** (post-training a model,
optimizing a benchmark score) rather than pure code optimization. These are the
patterns that matter when each iteration trains a model and scores it against a
benchmark. The `posttrain` example is a full working reference (it keeps one
model folder, so it doesn't roll models back; see
[One folder per iteration](#one-folder-per-iteration)).

## Layout

- **Write artifacts to an absolute path outside the project.** Models,
  checkpoints, and eval outputs must go to an *absolute* path outside the repo,
  one folder per iteration (e.g. `/data/x/$AUTOHELIX_RUN_ID/$AUTOHELIX_ITERATION/model`,
  [below](#one-folder-per-iteration)). Each phase runs in a fresh copy of the
  code ([Phases](phases.md)). A file the agent writes inside its copy that git ignores is deleted with
  the copy, and checks never see it; any other new file in scope is part of the
  change: checked and, if accepted, committed (a checkpoint saved in the working
  folder ends up in your git history). Ignore training by-products
  (`checkpoints/`, `wandb/`, `*.safetensors`, ...) in `.gitignore`. A rejected
  iteration rolls back *code*, not your external artifacts: give each iteration
  its own folder ([One folder per iteration](#one-folder-per-iteration)).
- **Evaluate from that path.** The eval script should read the model from the
  same absolute artifacts path, independent of the current working directory.
- **Report the score in a parseable form.** AutoHelix reads the metric from your
  eval command's stdout (an `observe:` entry with `metrics: {score: higher}`). It
  reads only a `##autohelix[score=<number>]` line (a plain `score: 0.9` line
  doesn't count). Without one the metric is missing: the baseline stops with a setup
  error, and later a `metric:` gate on it fails. Use the entry's `capture:` to
  also save training logs, eval JSON, etc. for the next agent and for later
  inspection.
- **Gate on the score with care.** A `metric: score` gate with `regress: N%`
  compares with the last accepted iteration. Eval scores are noisy: set N above
  the run-to-run noise (or make the eval more stable), especially with
  `improve:`, which otherwise keeps lucky measurements.
- **Freeze the eval harness.** Put the scorer, eval driver, run scripts, and prompt
  templates in `scope.frozen`. The agent should write *training* code only — never
  the thing that grades it (otherwise it can "fix" the eval to inflate the score).

## One folder per iteration

Key the artifacts folder by run and iteration, so no experiment overwrites the
model an accepted iteration produced:

```
/data/x/$AUTOHELIX_RUN_ID/$AUTOHELIX_ITERATION/model
```

Both the agent and the commands need these paths, so keep them in one script
in the [experiment folder](config.md#experiment-folder), `autohelix/train/paths.sh`
(see the [Training a model](patterns.md#training-a-model) pattern):

<!-- recipe: paths -->
```bash
# autohelix/train/paths.sh: sourced by the agent and by gate and observe commands
out=/data/x/$AUTOHELIX_RUN_ID/$AUTOHELIX_ITERATION/model
history=.autohelix-iteration/history.jsonl
if [ -f "$history" ]; then  # only the agent's copy has the run's history
  last=$(jq -s 'group_by(.iteration) | map(last | select(.accepted)) | last | .iteration' "$history")
  start=/data/x/$AUTOHELIX_RUN_ID/$last/model
fi
```

- **The agent** runs `source .autohelix-iteration/experiment/paths.sh`, then
  `rm -rf "$out"` (a killed iteration or a retried baseline reuses its number),
  and trains from `$start` into `$out`. `$start` is the last accepted
  iteration's folder (in the history a number can appear on more than one line;
  the last line for it counts); when that is the baseline, it doesn't exist:
  start from the base model. Put these steps in the goal, so the agent knows
  them.
- **Gate and observe commands** run `source "$AUTOHELIX_EXPERIMENT/paths.sh"`
  and evaluate `$out`: `AUTOHELIX_RUN_ID` and `AUTOHELIX_ITERATION` are in
  their [environment](phases.md#command-environment), with the values the
  agent had. At the baseline (`AUTOHELIX_ITERATION` is `0`) no agent has
  trained, so evaluate the base model there:

  ```bash
  # autohelix/train/eval.sh
  source "$AUTOHELIX_EXPERIMENT/paths.sh"
  if [ "$AUTOHELIX_ITERATION" = 0 ]; then out=/data/base-model; fi
  python evaluate.py --model "$out"   # prints ##autohelix[accuracy=...]
  ```

With a top-level `autohelix.yaml` (no experiment folder), put the same lines in
the goal and in the commands.

What it gives you: rollback. A rejected iteration's model stays in its own
folder, the next agent starts again from the last accepted one, and every
accepted iteration's model stays on disk.

What it doesn't:

- **Cleanup.** Nothing removes these folders, rejected iterations' included;
  `autohelix delete` and `archive` leave them. Delete them yourself.
- **Accepting a retrain alone.** An iteration that only retrains (no change to
  the code) is a no-op: accepted if its checks pass, and the next agent starts
  from its folder, but it makes no commit, so a `metric:` gate keeps comparing
  with the value measured on the last commit
  ([the reference](config.md#gates)).
- **Imported runs.** A run started with `run --import` has a new run id, so its
  first agent finds only its own baseline and starts from the base model. To
  continue the imported run's models, tell the agent to start from that run's
  folder (its history is in `.autohelix-iteration/runs/<id>/history.jsonl`).

A loop whose evaluation rebuilds the model from the commit needs none of this:
the [ml-recipe example](https://github.com/awslabs/AutoHelix/tree/main/examples/ml-recipe)
trains from its `train_config.yaml` and evaluates in one observe entry, so
the commit is the model.

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
- **Set `agent.timeout` for long runs.** It is a hard kill at the deadline, and
  it also injects the remaining time into the agent's context so it can wrap up
  and save notes first. Hints are supported for Claude Code, Codex CLI >= 0.141.0
  (an older Codex warns and runs without them), and OpenCode.

- **Long runs keep the machine awake, within limits.** `run` and `resume` hold a
  sleep block (macOS `caffeinate`, Linux `systemd-inhibit`) until the segment
  ends, so an idle workstation doesn't suspend mid-training; where the tool is
  missing or refused (containers, no systemd, polkit over ssh) there is none. A
  closed lid still sleeps, and credentials or a VPN can still expire: on a remote
  GPU box, start `autohelix run` itself (not the training inside it) in `tmux` so
  it outlives your ssh session.

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
