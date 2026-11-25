# Getting Started

## Installation

AutoHelix needs Python 3.11 or newer and git 2.29 or newer (`git --version`).

Install the CLI and its agent skill from git (until the PyPI release):

```bash
pip install git+https://github.com/awslabs/AutoHelix.git
```

Or install from source for the repository's example projects:

```bash
git clone https://github.com/awslabs/AutoHelix.git
cd AutoHelix
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Commands run in the environment AutoHelix starts in. Don't editable-install your
own project there: commands would then test the checkout's code, not the
candidate's (see [Command environment](phases.md#command-environment)).

## Agent Setup

AutoHelix runs one of three agent CLIs (`agent.cli`). You need at least one installed.

### Claude Code (default)

Install [Claude Code](https://docs.anthropic.com/en/docs/claude-code), then verify:

```bash
claude --version
```

```yaml
agent:
  cli: claude-code
  model: opus             # passed through unchanged; unset = the CLI's default
```

AutoHelix looks for `claude` in PATH. To use another executable (a wrapper, a
Bedrock launcher), set it in the environment, never in the config:

```bash
export AUTOHELIX_CLAUDE_CODE_CMD=/path/to/my/claude-wrapper
```

### Codex

```yaml
agent:
  cli: codex
  model: gpt-5.5
  effort: high
```

`$AUTOHELIX_CODEX_CMD` overrides `codex` on PATH.

### OpenCode

```yaml
agent:
  cli: opencode
  model: anthropic/claude-sonnet-4-5
```

`$AUTOHELIX_OPENCODE_CMD` overrides `opencode` on PATH. Each CLI's model
default, what `effort` maps to and its model docs: [Config → Agent](config.md#agent).

## Your First Run

### With your coding agent

Install the skill once, where your agent finds it:

```bash
autohelix skill install --agent claude-code --scope user   # or codex, opencode
```

Then ask the agent "Use AutoHelix to improve [goal]", in any app, IDE or terminal
session; it needs `autohelix` on its `PATH`. After upgrading AutoHelix, repeat
the install with `--force` (the agent tells you when its copy is older than
`autohelix --version`). In a terminal, `autohelix` does the same without the
install: it shows the project's runs (none yet), then asks "Start Claude Code
with AutoHelix? Enter/q" (or names the agent you have: Codex or OpenCode; it
asks which when there are several), and Enter starts that agent with the skill.
See [CLI → autohelix skill](cli.md#autohelix-skill) and
[CLI → autohelix](cli.md#autohelix).

The agent looks at the project, asks what AutoHelix should work on, and
designs the experiment with you from your existing code, checks and evidence.
When you agree, it saves the config as `autohelix/<name>/config.yaml` (kept
private unless you want it shared:
[Where configs live](concepts.md#where-configs-live)), checks it with
`autohelix run <name> --check`, and gives the command to start, with a
suggested budget (`autohelix run perf -n 10 --cost 20`). The agent you work
with becomes `agent.cli`, the loop's agent, unless you decide otherwise.

To have it start the run too, ask: "Run and manage AutoHelix using the
existing config", with any budget. It starts the loop, follows it to the end,
can send `autohelix hint`s, and reports what was accepted and measured. Its own
calls aren't counted in the run's cost.

Later, ask the agent about the runs: it reads them, says what the numbers don't
show (why iterations were rejected, anything odd) and asks what next. In a
terminal, `autohelix` lists them first, with the command that applies to each
(`resume`, `land`, `view`). See [After the first run](#after-the-first-run).

### Manual setup

#### 1. Write the config

Write the config of an experiment folder, `autohelix/perf/config.yaml` (a
project can have [several](#several-configs)):

```yaml
goal: |
  Make the sort function faster while keeping all tests passing.

scope:
  editable: [sort.py]

gates:                      # must pass, or the iteration is rejected
  - pytest tests/

observe:                    # measured after the gates; the next agent reads the output
  - name: bench
    command: python benchmark.py     # prints ##autohelix[speed=1234]
    metrics: {speed: higher}
  - name: review            # a read-only reviewer's feedback
    review: Review the change for correctness. What should the next iteration fix?

agent:
  cli: claude-code          # or codex, opencode
```

`gates:` decide what is accepted; `observe:` entries only inform (a failing
benchmark doesn't reject anything, unless a `metric:` gate is on its number).
For other goals, start from a [pattern](patterns.md) or an
[example](https://github.com/awslabs/AutoHelix/tree/main/examples); every key is
in the [configuration reference](config.md). There is nothing else to
initialize: the first run creates `.autohelix/`, its runtime directory, kept
out of git.

#### 2. Run

AutoHelix needs a git repository with a commit, and the first baseline needs no
uncommitted changes to tracked files. A script kept in the experiment folder is
stored with the run, like the config (commands run it as
`"$AUTOHELIX_EXPERIMENT/bench.py"`:
[Experiment folder](config.md#experiment-folder)); a script elsewhere in the
project must be committed, since copies are made from the commit. The config
needn't be committed. Keep `autohelix/` private, as the agent-led setup does:

```bash
echo /autohelix/ >> "$(git rev-parse --git-path info/exclude)"
```

or commit it to share the experiment (the other setups:
[Where configs live](concepts.md#where-configs-live)). Then launch:

```bash
autohelix run perf -n 5  # a bare `autohelix run -n 5` runs the only experiment
```

## After the first run

### Follow it

`run` prints a header (goal, agent, gates, observe, scope, budget), then one row per
iteration as it ends, with a live row for the one under way:

```text
             time    cost   speed ↑
  · base                       46.9
  ✓ 1         31s   $0.15  17,481.5 373×
  ✗ 2      2m 23s   $0.48  pytest tests/ failed (exit 1)
  ⠹ 3      1m 12s  agent
```

✓ is accepted, with each metric's change from the baseline; ✗ is rejected, with
the reason. When the command ends, the end block says why, how each metric
moved, what landed on your branch and what to type next (see
[CLI → What it prints](cli.md#what-it-prints)).

Use `autohelix view` in a second terminal to follow the run on a live page in
your browser (every run of the project, each with its own page), or
`autohelix view --terminal` for the same screen `run` prints (header,
iterations so far, the live row) with the agent's live output below it.
`autohelix stop <id>` ends it after the current iteration (`--now`, or Ctrl-C
in its terminal, at once); scripts can block on `autohelix wait`, which prints
each iteration and the run's end as JSON.

### Continue, set aside or start over

Each `autohelix run` starts a **new** run, with fresh notes. To give the same
run more iterations, resume it; each `run` or `resume` command has its own
budget:

```bash
autohelix resume -n 5                # five more iterations of this config's latest run
autohelix resume k3f9 --cost 5       # a specific run (its id, or its last part), up to $5 more
```

Runs you're done with stay listed in `autohelix view`; archive them to hide
them, or delete them for good. Your **code stays as is** either way:

```bash
autohelix archive k3f9               # hidden, skipped by a bare resume; history kept
autohelix delete k3f9                # history, notes and logs removed
```

For a full reset to before iteration 1, roll the code back first, while the
run's record still names its starting commit (`base_commit` in its `run.json`),
then delete the run:

```bash
git reset --hard "$(jq -r .base_commit .autohelix/runs/*-k3f9/run.json)"  # 1. code as when the run started
autohelix delete --force k3f9        # 2. remove the run (--force: even with unlanded commits)
rm -rf /path/to/artifacts/<task>/*   # 3. only if you configured an external artifacts path
```

Run step 1 on the run's branch. `git reset --hard` also discards uncommitted
changes and any commits you made on that branch since the run started.

### Several configs

A project can have several loops: one experiment folder each, in
`autohelix/` at the project root. Commit the configs, or keep the folder private
([Where configs live](concepts.md#where-configs-live)). An experiment's own
scripts can live in its folder: the run stores them, and commands run them as
`$AUTOHELIX_EXPERIMENT/<file>`
([Experiment folder](config.md#experiment-folder)); commit any other scripts
their commands use.

```
autohelix/
  perf/
    config.yaml          # autohelix run perf
    prompt.md            # prompt_template: prompt.md, in perf/config.yaml
    bench.py             # python "$AUTOHELIX_EXPERIMENT/bench.py"
  docs/
    config.yaml          # autohelix run docs
  lib/                   # no config.yaml: not an experiment
```

Runs are named after the folder (`20261112-1400-perf-0mws`). A bare
`autohelix run` uses the only experiment in `autohelix/`; with several, it
stops with the list (`autohelix run <name>` picks one). See
[CLI → Config files](cli.md#config-files) for the rules.

### Several runs side by side

To try a goal several ways at once (another model or prompt), write one config
per variant, commit them, and start one run per config in its own terminal,
landing manually so that none lands before you compare:

```bash
autohelix run a --land manual     # autohelix/a/config.yaml
autohelix run b --land manual     # autohelix/b/config.yaml
```

Compare them with `autohelix view`, then land the winner and archive the
rest:

```bash
autohelix land k3f9
autohelix archive m2x7 --why "slower than k3f9"
```

Runs that should build on each other's work instead share a group
(`--group perf`): each iteration's agent sees the other runs of its group, their
notes, history and accepted code. To keep going from a run later (after more
commits, or with an edited config), start a new one with `--import k3f9`.
See [CLI → Groups](cli.md#groups).

## Built-in Examples

The examples require a source checkout. List everything available there with
`python examples/setup_example.py --list`.

### Sorting (performance optimization)

```bash
python examples/setup_example.py sorting
cd sorting
autohelix run -n 3
```

Optimizes bubble sort. An observe entry (`python benchmark.py`) tracks the `speed` metric.

### Writing (review-driven quality)

```bash
python examples/setup_example.py writing
cd writing
autohelix run -n 3
```

Improves a short blog post guided by a `review:` observe entry: no numeric metrics, just qualitative feedback between iterations.

### AlgoTune Tasks

AlgoTune's 154 algorithmic optimization tasks, scored as AlgoTune does (reference time /
solver time, only when every output passes the task's check). The evaluation runs in a
container, where the instances and the evaluator are out of the agent's reach:
[examples/algotune](../examples/algotune/README.md) has the images, the runner and a
comparison of AutoHelix with independent attempts and Claude Code `/goal` on 30 tasks.
