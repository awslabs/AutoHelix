# Concepts

## The Iteration Loop

AutoHelix runs a loop where each iteration is isolated, validated, and either merged or discarded:

```
for each iteration:
    1. create_worktree()      — isolated git branch
    2. prepare_worktree()     — seed notes/ + read-only reference material
    3. build_prompt()         — goal + history + metrics + scope
    4. run_agent()            — agent makes changes
    5. save_notes()           — always, even on failure
    6. revert_out_of_scope()  — enforce editable/frozen
    7. run_constraints()      — hard gates (tests, linting)
    8. run_observables()      — measurements (if constraints pass)
    9. check_metric_gates()   — optional regression guards
   10. run_reviewer()         — optional qualitative feedback
   11. merge or discard       — accept valid changes, reject failures
```

## Git Worktrees

Each iteration runs in a separate [git worktree](https://git-scm.com/docs/git-worktree). This means:

- The agent works on an isolated copy of the code
- Failed iterations never touch the main branch
- Multiple iterations can't interfere with each other
- PYTHONPATH is remapped so Python imports resolve to the worktree (not the main repo)

Worktrees live in `.autohelix/worktrees/` and are cleaned up after each iteration.

## Notes

The agent's persistent memory across iterations. Stored in `.autohelix/notes/`.

`notes/` is the **sole agent-owned region** and the only thing that round-trips
between the main project and each iteration worktree. Everything else the agent
sees in `.autohelix/` (observations, hints, the latest review) is read-only
reference — copied *into* the worktree but never copied back. That asymmetry is
the whole contract: the agent freely structures `notes/` however the prompt
directs (per-iteration files, a journal, subdirectories), and only `notes/`
persists.

Key properties:

- **Survive rejection** — notes are saved *before* constraints run, so even rejected iterations contribute learnings
- **Copied into worktrees** — the agent sees notes from all previous iterations
- **Agent-managed** — the agent decides what to write and how to organize notes
- **Archived on clear** — `autohelix clear` moves notes to the archive, giving the agent a clean slate

Notes are the primary mechanism for the agent to build on previous attempts. They're more effective than full session context because the agent curates what's worth remembering.

## Scope Enforcement

AutoHelix can restrict which files the agent modifies:

- **Editable (allowlist)**: only matching files are committed
- **Frozen (denylist)**: matching files are reverted, everything else is committed

Enforcement happens *before* constraints run, so constraints test the actual state that would be merged. This prevents situations where constraints pass in the worktree but fail after merge because out-of-scope changes were included.

## Agent Backends

AutoHelix supports pluggable agent backends:

| Backend | CLI Tool | Key Features |
|---------|----------|-------------|
| **Claude Code** | `claude` | Stream JSON, model selection, auto memory control |
| **Codex** | `codex` | JSON output, reasoning effort, custom settings |
| **OpenCode** | `opencode` | JSON format, model variants |
| **Mock** | (built-in) | Deterministic changes for testing |

All backends implement the same interface: receive a prompt, work in a worktree, emit events for the live display.

## Reviewer

An optional second agent that provides qualitative feedback between iterations. It runs after constraints and metric gates pass but *before* the merge — so it only ever reviews changes that are about to land.

It is a full agent working in the same worktree, with tool access: it inspects the diff and code itself rather than being handed one. Its entire instruction is the `reviewer.prompt` you configure, plus where to write the result — it does **not** automatically receive the goal, metrics, or a prepared diff, so include whatever context you want it to consider in the prompt. Example:

```yaml
reviewer:
  prompt: |
    Review the change just made for clarity, correctness, and performance.
    What single change would most improve the result next iteration?
  # model: claude-opus-4-8   # override the model (defaults to the main agent's)
  # timeout_seconds: 300     # override the timeout
  # auto_memory: false       # independent reviews (default)
```

- The reviewer is **advisory, not a gate.** Its feedback never rejects an iteration — constraints and metric gates alone decide what merges. Even if the reviewer errors, the iteration still merges (with a placeholder review recorded).
- Each review is archived to `.autohelix/reviews/iter-N.md`. There is no standing "latest review" file — the latest is derived from the highest-numbered `iter-N.md`.
- The **latest** review is materialized into the next iteration's worktree as `.autohelix/review.md` so the main agent can read it; the `reviews/` archive itself is never copied into a worktree.
- Because the archive isn't copied in, the reviewer (which runs in the same worktree) **structurally cannot read prior reviews** — each review is independent. (`auto_memory: false`, the default, only disables Claude Code's own cross-session memory; the isolation here comes from the directory layout.)

## Prompt Template

The prompt sent to the agent each iteration is built from a [Jinja2](https://jinja.palletsprojects.com/) template. `autohelix init` writes the default template to `.autohelix/prompt.md` — reading it is the quickest way to see exactly what the agent receives each iteration (goal, constraints, metrics, where to read notes, the "write notes when done" instruction). Edit that file to customize it; if it's absent, AutoHelix falls back to the built-in default.

Available template variables:

| Variable | Content |
|----------|---------|
| `{{goal}}` | Goal from config |
| `{{iteration}}` | Current iteration number (baseline is iter-0) |
| `{{worktree}}` | Absolute path to this iteration's worktree (write only inside it) |
| `{{history_summary}}` | Summary of recent iterations (empty on the first) |
| `{{constraints}}` | List of constraint command strings |
| `{{observables}}` | List of `{command, metric_labels}` — the metric commands and their labelled directions (e.g. `speed (↑)`) |
| `{{editable}}` / `{{frozen}}` | Scope patterns (whichever is configured) |
| `{{iteration_time}}` | Per-iteration time budget as a compact duration (e.g. `15m`); empty if unset |
| `{{has_reviewer}}` | True when a reviewer is configured (gates the review.md hint) |
| `{{peers}}` | Names of parallel peer workers, if running under `autohelix parallel` |
| `{{has_hints}}` | True when `autohelix hint` has left a non-empty `hints.md` |

Because customizing the template freezes a copy at `.autohelix/prompt.md`, it won't pick up future changes to the built-in default — delete it to return to the default.

### Use case: a soft hold-out eval

Editing the template is also how you can keep an evaluation *out of the agent's view* — useful for measuring generalization without the agent optimizing against the eval itself. Two prompt-only levers:

- **Don't list the eval command.** Drop the `{{observables}}` (or `{{constraints}}`) loop from your `prompt.md`. The command still runs and its metric still gates/ranks the run — it just isn't named in the prompt, so the agent isn't handed the exact thing to target.
- **Fence the agent into its worktree.** Add an explicit instruction to only read and write inside `{{worktree}}`, and keep the eval script *and* any answer key outside the project tree (reference the script by absolute path from your config). The agent then has nothing in-scope to reverse-engineer.

This is a **best-effort nudge, not a security boundary.** In practice a cooperative agent respects the fence — in testing, Claude declined to read an out-of-worktree eval even when its path was visible in a log it had legitimately read. But nothing *enforces* it: iteration worktrees are nested under the project, so an agent that is determined (or explicitly told to cheat) can walk up to the parent directory and read whatever is there. Treat it as reducing reward-hacking pressure, not eliminating the possibility.

## Runtime Directory

`.autohelix/` contains all runtime state (gitignored):

Only `notes/` round-trips between the main project and iteration worktrees;
`observations/`, `hints.md`, and the latest review are copied *into* worktrees
as read-only reference; `reviews/` and `history.jsonl` never enter a worktree.

```
.autohelix/
├── notes/                # Agent's persistent memory (agent-owned; round-trips)
├── observations/         # Per-iteration captured artifacts (read-only reference)
│   ├── iter-0/           # Baseline outputs
│   ├── iter-1/           # Per-iteration stdout + captured files
│   └── ...
├── reviews/              # Reviewer feedback archive: iter-N.md (no standing "latest")
├── peer_notes/           # Parallel mode: symlinks to peers' shared notes
├── hints.md              # `autohelix hint` messages (read-only reference)
├── output/
│   ├── report.md         # Agent-written run analysis
│   └── dashboard.html    # Visual dashboard
├── logs/                 # Prompts and raw agent output per iteration
├── history.jsonl         # Accept/reject, metrics, usage per iteration
├── prompt.md             # Customizable prompt template
├── worktrees/            # Temporary git worktrees (one per iteration)
├── runtime/
│   ├── bin/              # Runtime scripts (time_left.sh, etc.)
│   └── config_path       # Path to config used by last run
└── archive/              # Past runs (from autohelix clear)
```

## Cost Tracking

Token usage and cost are captured from all agent backends:

- **Per iteration**: shown after each accepted/rejected result
- **Cumulative**: shown in the final summary
- **Persisted**: stored in `.autohelix/history.jsonl` for later analysis

What's available depends on the backend:

- **Claude Code**: input/output tokens, cost (USD), duration
- **Codex**: input/output tokens
- **OpenCode**: input/output tokens, cost (per step, accumulated)

Missing fields are simply omitted from the display.

## Dashboard

AutoHelix regenerates a self-contained HTML dashboard at `.autohelix/output/dashboard.html` after every iteration — no command or flag required. Open it in a browser (it refreshes on disk as the run progresses):

- Metric trajectories per iteration, with baseline (iter-0) as the reference and direction (↑/↓) taken from your config
- Accepted vs. rejected iterations, with the rejection reason
- Cost and token usage over the run

Because it's a single static file with no server, you can open it mid-run to watch progress or share it afterward. For a written narrative of the run instead, use [`autohelix report`](cli.md#autohelix-report).
