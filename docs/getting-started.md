# Getting Started

## Installation

Install the CLI and setup skill from PyPI:

```bash
pip install autohelix
```

Or install from source for the repository's example projects:

```bash
git clone https://github.com/awslabs/AutoHelix.git
cd AutoHelix
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

To design a run with your coding agent, run `autohelix` in your project, in a
terminal (see [With your coding agent](#with-your-coding-agent)), or explicitly
install the portable skill into your target project:

```bash
autohelix skill install --agent codex --scope project --path /path/to/my-project
```

Choose `claude`, `codex`, or `opencode`, then ask the agent to use
`autohelix-setup` to prepare and verify your experiment. The skill includes
performance, writing, and research patterns. Preparation does not start the
optimization loop. See [CLI → skill](cli.md#autohelix-skill) for user installation,
export, and update behavior.

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

In your project, run:

```bash
autohelix
```

`autohelix` shows the project's runs (none yet), then asks "Set up an experiment
with Claude Code? Enter/q" (or names the agent you have: Codex or OpenCode; it
asks which when there are several). Enter starts your coding agent with the
setup skill bundled with AutoHelix, and the agent starts by asking what you
want to achieve. The skill helps design the experiment from your existing code,
checks, and evidence. The agent saves the config as `autohelix.yaml` (the
project's first config; for later ones it proposes a short name for the goal
and saves them as `autohelix/<name>.yaml`), checks it with `autohelix run --check`, commits it, and
gives the `autohelix run` command to start. The agent you choose becomes
`agent.cli`, the loop's agent, unless you decide otherwise.

Later, `autohelix` lists the project's runs with the command that applies to
each (`resume`, `land`, `view`), and offers to ask the agent about them (it
summarizes what was accepted and rejected, then answers questions) or to set up
another experiment. See [CLI → autohelix](cli.md#autohelix).

With the skill installed as described above, you can also start from an
ordinary agent session. The installation's `--agent` selects the agent that
discovers the skill; `agent.cli` in the generated configuration selects the
loop's agent. For preparation only, ask:

> Set up AutoHelix to improve [goal]. [Optional instructions.]

Describe the outcome you want, such as making data processing faster while
preserving its results. The agent can propose suitable tests and benchmarks;
you do not need to specify the evaluation in the initial request.

The agent inspects the project, then shows the complete proposed YAML and its
intended path (`autohelix.yaml`, or `autohelix/<name>.yaml`) before changing
project files. A numbered walkthrough follows the YAML in configuration order:
what each section controls, why its settings were chosen, and how to adjust
them. It covers the goal, scope, gates, observations and evidence, backend,
and effective defaults for omitted fields. Missing evaluators are
proposed with their design and likely cost before implementation. Discuss
revisions, then agree to save and commit
when the proposal fits your goal. You can also ask for a proposal only, or for
files to be saved without committing.

The skill uses a consistent top-level order for generated configs: `goal`,
`scope`, `gates`, `observe`, `stop`, `agent`, `prompt_template`, omitting
unused fields. This is a presentation convention; existing configs can use any
top-level order.

After agreement, the agent writes the setup files, validates the config without
executing evaluators, and commits only the agreed setup changes. It can also run
known cheap checks within the request. Its final reply gives the saved paths,
commit, verification results, what remains unverified, and the complete
command line to start it, with a suggested budget and why (`autohelix run perf
-n 10 --cost 20`). Budgets are command-line flags, not config: `-n`
iterations, `--cost` dollars, `--time` wall clock, for each `run` or `resume`
(1 iteration when none is given; see [CLI → Budgets](cli.md#budgets)).
Measurements alone do not reject regressions; metric gates are optional.

After reviewing the configuration, ask for a managed execution:

> Run and manage AutoHelix using the existing config. [Optional changes or budget limits.]

For example, add limits of three iterations, four minutes per agent call, and
fifteen minutes for the loop. Choose bounds appropriate to your work.
Within the request, the agent uses the existing configuration, applies agreed
changes, verifies setup, resolves the clean
committed state required to launch, starts the loop, and monitors it through
termination. It can send evidence-based `autohelix hint` messages for subsequent
iterations. The handoff should explain accepted changes, actual measurements,
failures, evidence paths, and accounting limits. Ending this execution does not
mean an open-ended experiment cannot be continued with `autohelix resume`.

You can also combine preparation and execution in one request when you want the
agent to set up and manage the experiment from scratch.

`--time` and `--cost` are checked between iterations; per-agent deadlines and
check timeouts have their own boundaries. Dollar accounting depends on backend
reporting and includes the optimization agent and the `ask` and `review` calls
of gates and observe entries, including the baseline's. The run summary's cost
counts in the run's total but runs after `--cost` is checked; supervisor calls
are separate. Missing cost
is unknown, not zero. See [CLI → Budgets](cli.md#budgets) before relying on a cap.
For a long run, `run` and `resume` keep the machine from sleeping until the
segment ends (macOS `caffeinate`, Linux `systemd-inhibit`; nothing where the tool
is missing or refused). A closed laptop lid still sleeps, and expiring
credentials or a dropped VPN still stop the agent: keep the lid open (or use a
server) and check how long your login lasts.
The console, `autohelix view`, the run summary and run page (`report.md`,
`report.html`), and saved records already support inspecting results.

### Manual setup

The following steps prepare the same kind of experiment without the skill.

### 1. Write the config

Write `autohelix.yaml` at the project root (or `autohelix/perf.yaml`, one of
[several configs](#5-several-configs)). Start from the config below, an
[example](https://github.com/awslabs/AutoHelix/tree/main/examples), or the commented template shipped in the package:

```bash
cd my-project
python -c "from importlib.resources import files; print(files('autohelix.templates').joinpath('config_template.yaml').read_text())" > autohelix.yaml
```

(use the Python AutoHelix is installed in). There is nothing else to
initialize: the first run creates `.autohelix/`, its runtime directory, kept
out of git.

### 2. Configure

Edit `autohelix.yaml`: set your goal and uncomment relevant sections:

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
```

`gates:` decide what is accepted; `observe:` entries only inform (a failing
benchmark doesn't reject anything, unless a `metric:` gate is on its number).
See the [configuration reference](config.md).

### 3. Run

AutoHelix needs a git repository with a commit, and the first baseline needs no
uncommitted changes to tracked files. Commit the config, and any scripts its
commands use (iteration copies are made from the commit), before the first
`run`: with auto landing, a commit on the branch after the run makes `resume`
refuse (a run continues its own code), leaving `autohelix run --import <id>` or
`resume <id> --land manual`. Preserve unrelated work:

```bash
git add autohelix.yaml
git commit -m "Configure AutoHelix experiment"
```

The configuration must reference implemented checks and measurements that use
the intended environment. Then launch:

```bash
autohelix run -n 5       # autohelix run perf -n 5 for autohelix/perf.yaml
```

It prints a header (goal, agent, gates, observe, scope, budget), then one row per
iteration as it ends, with a live row for the one under way:

```text
             time    cost   speed ↑
  · base                       46.9
  ✓ 1         31s   $0.15  17,481.5 373×
  ✗ 2      2m 23s   $0.48  pytest -q test_sort.py failed (exit 1)
  ⠹ 3      1m 12s  agent
```

✓ is accepted, with each metric's change from the baseline; ✗ is rejected, with the reason. When
the segment ends, the end block says why, how each metric moved, what landed on your
branch and what to type next (see [CLI → What it prints](cli.md#what-it-prints)).

Use `autohelix view` in a second terminal to follow the run: the same screen `run` prints
(header, iterations so far, the live row), and the agent's live output below it (`autohelix view --web` for the same in a browser).
`autohelix stop <id>` ends it after the current iteration (`--now`, or Ctrl-C in its
terminal, at once); scripts can block on `autohelix wait`, which prints each iteration
and the run's end as JSON.

### 4. Continue, set aside or start over

Each `autohelix run` starts a **new** run, with fresh notes. To give the same
run more iterations, resume it; budgets count each segment:

```bash
autohelix resume -n 5                # five more iterations of this config's latest run
autohelix resume k3f9 --cost 5       # a specific run (its id, or its last part), up to $5 more
```

Runs you're done with stay listed in `autohelix dashboard`; archive them to hide
them, or delete them for good. Your **code stays as is** either way:

```bash
autohelix archive k3f9               # hidden, skipped by a bare resume; history kept
autohelix delete k3f9                # history, notes and logs removed
```

For a full reset to before iteration 1:

```bash
autohelix delete --force k3f9        # 1. remove the run (--force: even with unlanded commits)
git reset --hard <initial-commit>    # 2. roll code back to before the agent's iterations
rm -rf /path/to/artifacts/<task>/*   # 3. only if you configured an external artifacts path
```

Find `<initial-commit>` with `git log --oneline` (the one before "AutoHelix iteration 1").

### 5. Several configs

A project can have several loops: one config each, in an `autohelix/` folder
at the project root, versioned with the project. Commit the configs and any
scripts their commands use: iteration copies are made from the commit.

```
autohelix/
  perf.yaml              # autohelix run perf
  docs.yaml              # autohelix run docs
  prompts/perf.md        # prompt_template: prompts/perf.md, in perf.yaml
```

Runs are named after the file (`20261112-1400-perf-0mws`). A bare
`autohelix run` uses `autohelix.yaml`, else the only config in `autohelix/`;
with several, it stops with the list (`autohelix run <name>` picks one). See
[CLI → Config files](cli.md#config-files) for the rules.

### 6. Several runs side by side

To try a goal several ways at once (another model or prompt), write one config
per variant, commit them, and start one run per config in its own terminal,
landing manually so that none lands before you compare:

```bash
autohelix run a --land manual     # autohelix/a.yaml
autohelix run b --land manual     # autohelix/b.yaml
```

Compare them with `autohelix dashboard`, then land the winner and archive the
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

A large set of algorithmic optimization tasks are available:

```bash
python examples/algotune/setup.py kmeans
cd kmeans
autohelix run -n 5
```

Some tasks require additional dependencies (`scipy`, `networkx`, etc.).
