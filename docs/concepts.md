# Concepts

## The Iteration Loop

AutoHelix runs a loop where each iteration is isolated, validated, and either accepted or discarded:

```
for each iteration:
    1. create_worktree()      — detached checkout at the run tip
    2. prepare_worktree()     — seed notes/ + read-only reference material
    3. build_prompt()         — goal + history + metrics + scope
    4. run_agent()            — agent makes changes
    5. normalize()           — restore expected git state, keep file edits
    6. revert_out_of_scope()  — enforce editable/frozen
    7. run_constraints()      — hard gates (tests, linting)
    8. run_observables()      — measurements (if constraints pass)
    9. check_metric_gates()   — optional regression guards
   10. run_reviewer()         — optional qualitative feedback
   11. accept or discard      — update run ref and durable history atomically
   12. save_notes()/cleanup()  — always, even on failure
```

## Designing Your Own Loop

A useful loop answers five questions:

1. **What should improve?** Define the goal and how progress will be observed.
2. **What may the agent change?** Define the editable surface.
3. **What must remain true?** Encode non-negotiable requirements as constraints.
4. **What should carry forward?** Decide which metrics, artifacts, reviews,
   results, and lessons later iterations need.
5. **When should the loop stop?** Set an iteration, time, or cost budget that
   matches the intended scope.

### Tips

- Keep checks and evaluation criteria outside the editable scope.
- After `autohelix init`, inspect `.autohelix/prompt.md` to see exactly what the
  agent will receive. You can edit it before running to customize the agent's
  instructions. See [Prompt Template](#prompt-template).
- AutoHelix already retains iteration evidence and agent memory. When findings
  should also be user-facing and revisable, keep them in an editable,
  version-controlled file. The [research example](../examples/research/) uses
  `REPORT.md` for this purpose.
- The editable content and evaluated result need not be the same. In the
  [post-training example](../examples/posttrain/), the agent writes training
  code while the evaluator scores the resulting model. AutoHelix cannot
  restore artifacts outside git when an iteration is rejected.

See the [examples](../examples/) for concrete loops that can be adapted or
combined.

## Git Worktrees

Each iteration runs in a separate [git worktree](https://git-scm.com/docs/git-worktree). This means:

- The agent works on an isolated copy of the code
- Accepted commits advance the run's hidden `refs/autohelix/<id>` first
- A process-held per-run lock prevents concurrent writing lifecycle operations
- PYTHONPATH is remapped so Python imports resolve to the worktree (not the main repo)

Worktrees live in `.autohelix/worktrees/<id>/` and are cleaned up after each iteration.

## Notes

The agent's persistent memory across iterations. Stored in `.autohelix/runs/<id>/notes/` and presented to the agent as `.autohelix/notes/`.

`notes/` is the **sole agent-owned region** and the only thing that round-trips
between the main project and each iteration worktree. Everything else the agent
sees in `.autohelix/` (observations, hints, the latest review) is read-only
reference — copied *into* the worktree but never copied back. That asymmetry is
the whole contract: the agent freely structures `notes/` however the prompt
directs (per-iteration files, a journal, subdirectories), and only `notes/`
persists.

Key properties:

- **Survive rejection** — notes are saved during final cleanup, so even rejected iterations contribute learnings
- **Copied into worktrees** — the agent sees notes from all previous iterations
- **Agent-managed** — the agent decides what to write and how to organize notes
- **Retained on abandon** — `autohelix clear` aliases abandonment; `run --new` gives a new run separate state

Notes are the primary mechanism for the agent to build on previous attempts. They're more effective than full session context because the agent curates what's worth remembering.

## Scope Enforcement

AutoHelix can restrict which files the agent modifies:

- **Editable (allowlist)**: only matching files are committed
- **Frozen (denylist)**: matching files are reverted, everything else is committed

Enforcement happens *before* constraints run, so constraints test the actual state that would be accepted. This prevents situations where constraints pass in the worktree but fail after acceptance because out-of-scope changes were included.

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

An optional second agent that provides qualitative feedback between iterations. It runs after constraints and metric gates pass but *before* acceptance into the run. Landing on the user's branch may happen later, particularly in manual mode.

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

- The reviewer verdict is **advisory**. Ordinary errors still allow acceptance (with a placeholder review). Reviewer candidate edits invalidate the checks and reject the iteration. Every reviewer/ask call compares candidate contents before and after, including new/deleted files, modes, symlinks and submodules; runtime feedback is allowed. Backend read-only restrictions remain best effort.
- Each review is archived to `.autohelix/runs/<id>/reviews/iter-N.md`. There is no standing "latest review" file — the latest is derived from the highest-numbered `iter-N.md`.
- The **latest** review is materialized into the next iteration's worktree as `.autohelix/review.md` so the main agent can read it; the `reviews/` archive itself is never copied into a worktree.
- The intended reviewer view omits the review archive and removes the materialized latest `review.md` before its call. Agents can still reach shared project state and git metadata; the directory layout does not isolate that access. `auto_memory: false`, the default, disables Claude Code's cross-session memory.

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

## Run state and lifecycle

`.autohelix/` is gitignored locally; AutoHelix leaves `.gitignore` unchanged.
`prompt.md` is project-wide, and `current` points to the selected run. State is
scoped to `.autohelix/runs/<id>/`:

```
.autohelix/
├── prompt.md
├── current
├── runs/<id>/
│   ├── run.json                # immutable identity + lifecycle metadata
│   ├── history.jsonl           # schema 1 result/core fields and model calls
│   ├── notes/                  # agent memory; round-trips
│   ├── observations/iter-N/    # stdout and captured artifacts
│   ├── reviews/iter-N.md       # reviewer feedback archive
│   ├── hints.md
│   ├── logs/iter-N/call-*/     # separate directory per actual agent/judge call
│   ├── output/report.md
│   ├── output/report.html
│   └── runtime/status.json        # advisory live status/worktree/log
├── worktrees/<id>/iter-N/      # temporary detached checkouts
└── runtime/                   # shared helper scripts and persistent lock files
```

The agent still sees a small `.autohelix/` view inside its worktree. Only `notes/`
round-trips; observations, hints, peer notes and latest `review.md` are reference
material. Persistent history/review archives never enter the worktree.

A run resumes by config path or exact id. One invocation, called a **segment**,
holds a process lock through validation, final landing and report. Agent-created
commits are reopened as file changes before scope and checks run. Acceptance
records only the validated files, with the previous accepted tip as their parent;
it leaves the user's branch and staged files alone. No-op accepts retain the same
tip. Judges use the same content policy with no candidate writes allowed.

Resuming repairs an interrupted final record and recovers accepted commits whose
record was not finished. Other history corruption is refused. User commits on
the target can be picked up when they descend from the run tip. Auto landing
fast-forwards the target at segment end before report; manual mode retains code
on the run ref until `autohelix land`. Divergence, a branch being rebased or checked
out elsewhere, and dirty files that would be overwritten require user resolution.
If the target moves during a segment, AutoHelix leaves it alone and asks for an
explicit land after review. Unrelated dirty and staged files are preserved.

If you reset the target behind accepted run code, auto landing would reapply that
code. Use `autohelix abandon` to discard the old run before starting `run --new`.
Dirty submodule contents must be committed within the submodule before acceptance;
a parent commit can record a clean submodule commit, not its uncommitted files.

Only first valid baseline capture requires a clean checkout. Resumed evaluation,
stop judges and summary use accepted run checkouts. Agents can still escape a
worktree through absolute paths or shared git metadata: main-checkout inspection
is best effort and logs possible escapes, without reverting user edits. Worktrees
are correctness tools for cooperative agents, not a filesystem security boundary.

Abandon retains evidence and unlanded commits, refuses future resume and leaves
code intact. Purge removes the run directory/ref, while lock files remain outside
that directory. Legacy top-level history must be moved or deleted before run;
there is no automatic migration.

## Cost Tracking

Available token usage and cost are captured from agent backends:

- **Per iteration**: shown after each accepted/rejected result
- **Cumulative**: shown in the final summary
- **Persisted**: stored in `.autohelix/runs/<id>/history.jsonl` for later analysis

Each iteration's cost includes its optimization agent and reviewer. Run totals
and `budget.cost` also include baseline reviews. History's `calls` list records each agent, reviewer and command-judge attempt with
its role, usage and log path; baseline judges/reviewer and failed calls count too.
Failed main-agent calls consume their iteration number and resume at the next one;
setup failures before a call do not. Baseline retry calls stay in iteration 0.
`calls` is the sole persisted usage representation; log paths are relative to the
run directory.
Report generation has its own separately recorded cost.

What's available depends on the backend:

- **Claude Code**: input/output tokens, cost (USD), duration
- **Codex**: input/output and available cached-input, cache-write, and reasoning
  tokens; calculated API-equivalent cost for recognized models; measured duration
- **OpenCode**: input/output tokens, cost (per step, accumulated)

Every backend uses the same `usage.cost_usd` field and **Cost** display.
History also records `cost_source`: `backend` for a CLI-supplied amount,
`calculated` for a Codex estimate. Calculated costs save their model, pricing
basis, and rates. Codex estimates use Standard OpenAI API prices; they do not
represent subscription or Bedrock bills. Backend-supplied amounts may also be
estimates.

Missing costs are unavailable, and totals with missing costs are incomplete.
`budget.cost` uses the recorded amounts, including Codex estimates, and warns
if some costs are unavailable. See [Cost Tracking](config.md#cost-tracking) for
the pricing assumptions and accounting limits.

## Run page

To follow a run live, use [`autohelix view`](cli.md#autohelix-view). When a run stops (finished, interrupted, or failed), AutoHelix saves the same page as one self-contained file, `.autohelix/runs/<id>/output/report.html`, which opens in a browser without a server. It shows the status, the metric chart, the iteration table with rejection reasons, each iteration's notes and review, the changes from the baseline to the final code, and the run summary when there is one (see [Configuration → Report](config.md#report)). It leaves out agent and reviewer activity, per-iteration diffs, and captured output; use `autohelix view` for those. `autohelix report` also rewrites it.
