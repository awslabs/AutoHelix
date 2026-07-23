# Getting Started

## Installation

AutoHelix is installed from source:

```bash
git clone https://github.com/awslabs/AutoHelix.git
cd AutoHelix
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

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

List everything available with `python examples/setup_example.py --list`.

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
