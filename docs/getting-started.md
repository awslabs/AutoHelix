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

To design a run with your coding agent, explicitly install the portable skill
into your target project:

```bash
autohelix skill install --agent codex --scope project --path /path/to/my-project
```

Choose `claude`, `codex`, or `opencode`, then ask the agent to use
`autohelix-setup` to prepare and verify your experiment. The skill includes
performance, writing, and research patterns. Preparation does not start the
optimization loop. See [CLI → skill](cli.md#autohelix-skill) for user installation,
export, and update behavior.

## Agent Setup

AutoHelix supports multiple agent backends. You need at least one installed.

### Claude Code (default)

Install [Claude Code](https://docs.anthropic.com/en/docs/claude-code), then verify:

```bash
claude --version
```

AutoHelix looks for `claude` in PATH. To use a custom command:

```bash
export AUTOHELIX_CLAUDE_CMD=/path/to/my/claude-wrapper
```

### Codex

```yaml
agent:
  type: codex
  command: /path/to/codex
  model: gpt-5.5
  reasoning_effort: high
```

### OpenCode

```yaml
agent:
  type: opencode
  command: /path/to/opencode
```

## Your First Run

### With your coding agent

Start in a git project with the skill installed as described above. The skill
helps design the experiment from your existing code, checks, and evidence.
The installation's `--agent` selects the agent that discovers the skill;
`agent.type` in the generated configuration selects the loop backend.

For preparation only, ask:

> Set up AutoHelix to improve [goal]. [Optional instructions.]

Describe the outcome you want, such as making data processing faster while
preserving its results. The agent can propose suitable tests and benchmarks;
you do not need to specify the evaluation in the initial request.

The agent should inspect the project, propose scope and evaluation, write
`autohelix.yaml`, and validate it without executing evaluators. It can also run
known cheap checks within the request. Missing evaluators should be proposed
before implementation unless already authorized. Review the generated goal,
scope, constraints, observations, optional gates/reviewer, and budget choices.
Measurements alone do not reject regressions; acceptance gates are optional.
Unset optional budgets provide no cap from those fields, while omitted
iterations default to five attempts.
The final reply should show the complete generated YAML, followed by a numbered
walkthrough of its sections: why each choice was made, what it controls, and how
you can adjust it. Comments can explain defaults or open choices where useful.
Verification results and launch instructions follow the walkthrough; file links
and a durable explanation can supplement it.

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
mean an open-ended experiment cannot be resumed.

You can also combine preparation and execution in one request when you want the
agent to set up and manage the experiment from scratch.

Time thresholds are checked between iterations; per-agent deadlines and check
timeouts have their own boundaries. Dollar accounting depends on backend
reporting and currently excludes reviewer and supervisor calls. Missing cost
is unknown, not zero. See [budget details](config.md) before relying on a cap.
The console, `autohelix view`, the run summary and run page (`report.md`,
`report.html`), and saved records already support inspecting results.

### Manual setup

The following steps prepare the same kind of experiment without the skill.

### 1. Initialize

```bash
cd my-project
autohelix init
```

This creates:

- `autohelix.yaml` — config template at project root
- `.autohelix/` — runtime directory (gitignored)

### 2. Configure

Edit `autohelix.yaml` — set your goal and uncomment relevant sections:

```yaml
goal: |
  Make the sort function faster while keeping all tests passing.

constraints:
  - pytest tests/

scope:
  editable: [sort.py]

metrics:
  - command: python benchmark.py
    values:
      speed: higher
```

### 3. Run

AutoHelix requires a clean committed working tree. Review and commit the
intended setup changes first; preserve unrelated work:

```bash
git add autohelix.yaml .gitignore
git commit -m "Configure AutoHelix experiment"
```

The configuration must reference implemented checks and measurements that use
the intended environment. Then launch:

```bash
autohelix run -n 5
```

You'll see a live display of agent activity, followed by constraint/metric results for each iteration.

Use `autohelix watch` in a second terminal for detailed live output.

### 4. Reset

`autohelix clear` only archives run state (history, notes, logs) — your **code stays as
is** (every iteration is still committed):

```bash
autohelix clear
```

For a full reset to before iteration 1:

```bash
autohelix clear                      # 1. archive history, notes, logs (code unchanged)
git reset --hard <initial-commit>    # 2. roll code back to before the agent's iterations
rm -rf /path/to/artifacts/<task>/*   # 3. only if you configured an external artifacts path
```

Find `<initial-commit>` with `git log --oneline` (the one before "AutoHelix iteration 1").

## Built-in Examples

The examples require a source checkout. List everything available there with
`python examples/setup_example.py --list`.

### Sorting (performance optimization)

```bash
python examples/setup_example.py sorting
cd sorting
autohelix run -n 3
```

Optimizes bubble sort. Uses numeric metrics (`python benchmark.py`) to track progress.

### Writing (reviewer-driven quality)

```bash
python examples/setup_example.py writing
cd writing
autohelix run -n 3
```

Improves an API reference doc guided by an LLM reviewer — no numeric metrics, just qualitative feedback between iterations.

### AlgoTune Tasks

A large set of algorithmic optimization tasks are available:

```bash
python examples/algotune/setup.py kmeans
cd kmeans
autohelix run -n 5
```

Some tasks require additional dependencies (`scipy`, `networkx`, etc.).
