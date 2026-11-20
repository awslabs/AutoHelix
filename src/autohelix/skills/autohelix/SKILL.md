---
name: autohelix
description: "Work with AutoHelix in a project: set up an experiment (requests such as 'use AutoHelix to improve X'), explain its runs (what was accepted or rejected and why, progress, cost, landing), or launch and supervise a bounded run when asked."
---

# AutoHelix

AutoHelix gives each attempt a private copy of the code, runs configured checks
and measurements on what it froze, and commits accepted changes to the run.
Each iteration starts a fresh agent session, with persistent notes and
measurements from earlier attempts.

This skill covers three jobs: explaining existing runs, setting up an
experiment (an agreed, checked config, ready to run), and launching or
supervising a run, only when the user asks for one.

Use the installed AutoHelix; a source checkout is not required. Its CLI and
Python are the ones the session's first prompt names, else the `autohelix` on
`PATH` and the interpreter on that launcher's first line
(`head -1 "$(command -v autohelix)"`): a plain `python` may lack the package
(pipx, `uv tool`, a venv that isn't active), and the harness and the target
project may use different environments. Check `autohelix --help` and the
relevant subcommand help against the installed version. Its user docs are in
the folder `autohelix docs` prints (`README.md` is their index); read them when
needed. Every config key is in `config.md` there, every flag in `cli.md`.

Don't start, resume, stop, land, archive or delete a run, or send a hint,
unless the user asks.

## Open the session

Unless the user has already said what they want, open with one short message,
with no preamble and no mention of this skill. First take a quick look and run
nothing that changes anything: `autohelix/README.md` first when it exists (the
coordinator's notes: see [Coordinator notes](#coordinator-notes)), the README
and project instructions, how the
code is tested and measured (test folders, benchmarks, evaluators, CI config),
task or spec files (`TODO`, `TASKS.md`, a PRD), and any AutoHelix configs
(`autohelix/*/config.yaml`).

- **With current runs**: the user has just seen them in a table (state,
  iterations, landing, cost), from bare `autohelix`; don't repeat it. Read the
  live runs and the most recent other one ([reading runs](references/reading-runs.md))
  and give in a few lines what the table doesn't show: why recent iterations
  were rejected, anything odd. Then ask what next, with two or three examples
  drawn from the state: a question about a run, the command that applies (land,
  resume), a new experiment.
- **Without runs**: a sentence on what you found that matters for a loop (what
  can check or measure a change), then "What should AutoHelix work on?" with
  two or three one-line goals drawn from the project. Configs no run has used
  are worth one of them ("check and finish `autohelix/perf/config.yaml`"). In an empty
  or unfamiliar project, ask with general examples.

For example:

> 7bne finished: 10 of 12 iterations accepted; two were rejected because
> `cli.md` named flags that don't exist. What next? For example: show what the
> accepted iterations changed, land 7bne, or set up a new experiment.

> This is a Python CLI with pytest tests and a benchmark in `bench/`. What
> should AutoHelix work on? For example: make `parse()` faster, fix the failing
> date tests, or implement the open items in `TODO.md`.

Then follow the answer: questions about runs below, a new or changed
experiment under Set up an experiment.

## Questions about existing runs

Read [reading runs](references/reading-runs.md) and answer from the run
records. Change nothing unless asked.

## Set up an experiment

Inspect what the goal needs: the relevant code, existing evaluators, git
state. Preserve existing work and configuration.

### Choose the design

Recommend, don't interview. From the user's goal and the project, choose the
scope, gates, observe entries and launch command; ask before proposing only
when the project can't tell you something that changes the loop materially,
such as there being no way yet to check or measure the goal. For help, read
[loop design](references/loop-design.md); for a starting config by kind of
goal (a checked task, optimization, training a model, working through a spec,
writing and research), read `patterns.md` in the docs.

- **Scope**: the files the goal needs. Keep tests, benchmarks, evaluation
  criteria and `autohelix/` outside `scope.editable` unless changing them is
  the task.
- **Gates** for what must hold (commands, or an `ask:` gate for a yes/no
  judgment), **observe entries** for what to measure or review. A `review:`
  entry never rejects: its feedback goes to the next iteration. A qualitative
  goal can use captured evidence and review feedback without inventing a
  numeric score.
- **A metric adapter** is the normal case for a measured goal: few tools
  print `##autohelix[name=value]` lines, so propose a small script in the
  experiment folder (`autohelix/perf/bench.py`, run as
  `python "$AUTOHELIX_EXPERIMENT/bench.py"`) that runs the tool and prints
  the line, with its code in the proposal. Running it once is the cheap check
  that the metric prints.
- **A new evaluator** (check, benchmark, validator) is part of the proposal:
  what it checks or measures, representative cases and limits, likely
  runtime and model cost, and whether it gates or informs. Label it as not yet
  written; never put an invented command in the YAML as though it exists.

Pick these yourself and say what you picked; don't ask about them:

- **Iterations**: `-n 1` for a single task the gates check, with an `ask:`
  gate for what the tests can't judge (a one-iteration run has no later
  review and lands by itself); `-n 3` for an open-ended goal ("make X faster", "improve Y"), a first look that `resume`
  can extend; with a `stop:` condition, enough for the whole job (`-n 10`). A
  goal checked only by a review needs more than one iteration.
- **Landing**: automatic (the run fast-forwards the branch it started on when
  the command ends) for simple tasks the gates fully check: a fix with a
  reproducer, a measured speedup with tests. `--land manual` for complex work:
  changes across many files, design or refactoring, anything the user would
  review as a pull request; also when the user wants to read the code first,
  will keep committing on the branch while the run goes, or is comparing
  approaches in separate runs. `--group` runs land manually already.
- **Cost and time**: no `--cost` or `--time` unless the user asks. When every
  iteration makes extra model calls (reviews, `ask:` gates) or evaluation is
  expensive, say so in the handoff and offer the flag ("add `--cost 20` to cap
  it").
- **Timeouts**: none (no `timeout:` on entries, no `agent.timeout`): the
  built-in defaults apply. When each iteration trains a model for a long time,
  offer `agent.timeout` and `--time` (`patterns.md`, Training a model).
- **Metric gates**: none: the agent sees the numbers and decides. Suggest one
  in the walkthrough when a measurement is stable enough to protect.
- **Agent**: `agent: {cli: X}`, where X is the CLI this session runs in (the
  first prompt names it), unless the user names another. Add `model:` or
  `effort:` only when the user names one.
- **Summary**: written by default; `--no-report` only when the user asks (it
  isn't stored: it goes on every `resume` command too).

Budgets, landing and the summary are command-line flags, never config keys
(`budget:`, `land:` and `report:` are errors). The iteration prompt already
tells the agent to leave its edits uncommitted; don't repeat that in `goal:`.

### Propose the config

Name the config's path:

- the path you were given (the session's first prompt may name it); a config
  that still holds the commented template is completed where it is;
- else `autohelix/<name>/config.yaml`, a new experiment folder. A name is
  lowercase letters, digits or hyphens, at most 32 characters, and starts with
  a letter or digit. Runs are named after the folder, and the launch command
  names it: `autohelix run <name>`.
- Only `autohelix/<name>/config.yaml` is a config: a folder in `autohelix/`
  without one (`autohelix/lib/`) isn't an experiment.

An experiment folder holds its config and what only it uses: its prompt
template, evaluators, metric adapters, sketches. The run stores the folder
when it starts (dotfiles and `__pycache__/` left out; at most 200 files and
10 MB):

```
autohelix/
  console/
    config.yaml
    render.py                  # its evaluator
    metrics.py -> ../lib/metrics.py
  lib/                         # helpers several experiments share (not an experiment)
    metrics.py
  README.md                    # the coordinator's notes
```

Helpers several experiments share go in `autohelix/lib/`, and each experiment
links what it uses with a relative link (`ln -s ../lib/metrics.py
autohelix/console/`), so it survives cloning `autohelix/` elsewhere. Links are
followed only inside `autohelix/`, and not into dot-names there
(`autohelix/.git/`, `.env`); other links are refused. A helper the project
commits stays in the project (`scripts/`) and commands name it by its path
(`python scripts/bench.py`): committed files are in every copy. All of
`autohelix/` is frozen: agents never edit experiment configs or material.

A document the goal relies on (a design, a spec): to freeze it with the run
(editing it then makes `resume` refuse), keep it in the experiment folder or
`autohelix/` and link it; an ignored one elsewhere (under `notes/`) is copied
into the folder. To keep it live, name it by its path in the goal.

Preserve other configs and the run state in `.autohelix/`. A config in the
first format (`constraints:`, `metrics:`, `reviewer:`, ...) no longer loads:
propose converting it.

Use project-relative paths for code and commands, so they resolve inside
iteration copies. Commands must use the intended environment and load the
candidate code, not an editable install of the original checkout; an absolute
path to an agent or Python executable can select the environment, but candidate
code, inputs and captured outputs must resolve in the copy. Make external output
locations deliberate: rejecting code doesn't restore files outside the project.
A script kept in the experiment folder is stored with the run;
commands run it from their copy of the folder:
`python "$AUTOHELIX_EXPERIMENT/render.py"`.

Show the complete YAML in a fenced block, the full goal included (its first
line is the run's title). New configs use this top-level order, omitting unused
keys: `goal`, `scope`, `gates`, `observe`, `stop`, `agent`, `prompt_template`.
Keep an existing config's order; for one, show the replacement and summarize the
changes. The order of entries within `gates`, `observe` and `stop` is the order
they run in.

Then a numbered walkthrough in config order, as long as the choices need: one
line for a setting with an obvious reason, more where there was a real choice.
Say what each part makes the run do, separating instructions to the agent
(`goal`) from what is enforced (gates, scope) and measurements from acceptance;
the defaults that matter (no metric gate, the default model); what you inspected
and what is unverified; any helper or evaluator to write, with its path; and
last the launch command and why. For example:

> 1. **Goal**: reduce runtime, keep behaviour. Guides the agent; the gate
>    enforces correctness.
> 2. **Scope**: only `src/solver.py`; the tests and benchmark stay fixed, so
>    the agent can't change what measures it.
> 3. **Gates**: `pytest tests/` must pass.
> 4. **Observe**: `bench` records `runtime_ms` (lower is better) and keeps
>    `results/benchmark.json` for the next agent. No metric gate: a slower
>    candidate that passes the tests is accepted. Once you know how noisy the
>    benchmark is, `- metric: runtime_ms` / `regress: 5%` would reject
>    regressions.
> 5. **Agent**: Claude Code, its default model.
> 6. **Launch**: `autohelix run perf -n 3`: a first look at whether the number
>    moves; it lands automatically, since tests and the benchmark check a
>    speedup.
>
> I read the tests and `benchmark.py`; I haven't run them.

Change no project files while proposing and revising: no config, helpers,
evaluators or commits yet. For a design-only request, stop at the proposal.
Otherwise ask whether to save it, or what to change; show the updated
proposal after each revision. Agreement authorizes writing the proposed files,
and the commits below, unless the user says otherwise.

### Save and check

Write the agreed config at its path, and only the agreed helpers, templates and
evaluators. Fix routine validation errors within the agreed design; go back to
the proposal if a fix would change the experiment.

Check it without running its commands: its structure, metric names and the
gates that use them, scope and input paths, environments and import paths,
where outputs and captures will be. Metrics come only from
`##autohelix[name=value]` lines on stdout; check that something prints them.
Then `autohelix run <name> --check` from the project root (`autohelix run perf
--check` for `autohelix/perf/config.yaml`): every check `autohelix run` makes
before writing anything, starting nothing; it exits 0 when the run would
start. Resolve its errors and explain material warnings.

Run the configured commands only when they are known to be cheap in time,
resources and model cost. Otherwise describe the verification and its likely
cost, and ask, unless the user's request already authorizes it. A timeout
doesn't make a command cheap. This applies to tests, training, benchmarks,
import probes, reviews and `ask` gates alike. A baseline or review call isn't
needed to finish setup; report what ran and its result, and what didn't run and
so is unverified.

An experiment folder is private by default. When `autohelix/` has no tracked
files and isn't ignored yet, add `/autohelix/` to the local exclude file
(`git rev-parse --git-path info/exclude`: `.git` is a file in a linked
worktree; for a project in a subfolder the pattern is `/<subfolder>/autohelix/`,
as patterns start at the repository's top) and say
so in one line: "kept private: commit it to share it, or `git init` in
autohelix/ for history". Commit only when the user wants the experiment shared
(`git add -f autohelix/<name>/` plus the `lib/` files it links: an excluded
file needs `-f`), or into `autohelix/`'s own repository when it has one. Pick
one of the two per project: with `autohelix/.git` present, `git add -f
autohelix/...` in the project stages nothing.

Iteration copies are made from the commit: every script a command runs is
committed in the project, or in the experiment folder and run from
`$AUTOHELIX_EXPERIMENT`. Commit by explicit path, leaving unrelated work,
staged changes included, as it is. If there is no repository or commit yet,
say what remains. Run `--check` again after a commit.

### Hand off

Keep it brief: the saved files, where they are kept (private, or the setup
commit), the `--check` result, what ran and what is unverified. End with the
complete launch command, every flag written out (`-n 1` included), and one line
on why. For example:

- `autohelix run perf -n 3`: "a first look at whether the number moves; it
  lands when the command ends. `autohelix resume <id> -n 10` continues."
- `autohelix run tasks -n 10 --land manual`: "room for the whole list; the stop
  condition ends it sooner. Read the code, then `autohelix land <id>`. Each
  iteration includes a review: add `--cost 20` to cap the spend."

The YAML and its walkthrough were in the proposal; repeat them only if asked.

### Change an experiment

Edit its config or folder; a run that is going keeps the copy it stored. A
changed config or folder makes `resume` refuse, so continue with
`autohelix run <name> --import <id>`.

## Coordinator notes

`autohelix/README.md` is the coordinator's notes: current state, decisions and
why, work done outside the loop, what's next. Keep it short; longer material
goes in its own file in `autohelix/` (or an experiment folder), linked from it.

- Read it first in every session.
- At the end of a session that changed something (a config, a run started
  or landed, a decision), update it: what changed, what was decided, what's
  next. Create it when it doesn't exist yet (a new `autohelix/` is excluded first,
  as under Save and check). Commit `autohelix/` only when it
  has its own repository and the user agreed to the session's changes; never
  commit into the project from here unless asked.
- Only while `autohelix/` isn't tracked by the project: editing a tracked one
  during a run dirties the checkout, and committing it puts the branch past an
  auto-landing run's tip. With a committed `autohelix/`, don't write it (or
  suggest excluding that one file).

## Launch and supervise a run

Only when the user asks. The request says how far to go, and the config is
the agreement on what the loop does, not permission for unlimited work:

- "Use AutoHelix to make the solver faster": prepare a config and hand off the
  launch command.
- "... and start a pilot": also launch it, within the bounds given.
- "... run three iterations and explain the results": launch, supervise to the
  end, and report.

A run needs a git checkout with a commit, and its first baseline needs no
uncommitted changes to tracked files: name any that block it without
committing or discarding them. Use the bounds given; resolve
material budget ambiguity as [loop design](references/loop-design.md) says. For
supervision, read [managed runs](references/managed-runs.md): monitor the
actual process and saved outcomes, send evidence-based hints, continue through
termination or explain a concrete blocker (starting a background process
doesn't finish the job), and hand off the accepted changes, results, failures
and remaining limits. Never commit while the loop runs: AutoHelix commits
accepted changes.
