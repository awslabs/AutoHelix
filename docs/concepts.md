# Concepts

## The Iteration Loop

AutoHelix runs a loop where each iteration is isolated, validated, and either accepted or discarded:

```
for each iteration:
    1. agent copy             — fresh copy of the accepted tip A, plus notes/ and reference material
    2. build_prompt()         — goal + history + metrics + scope
    3. run_agent()            — agent makes changes in its copy
    4. freeze                 — snapshot the copy to a tree T; out-of-scope changes are dropped;
                                notes are saved and the copy is deleted
    5. checks copy            — fresh copy of T (HEAD at A)
    6. run_constraints()      — hard gates (tests, linting)
    7. run_observables()      — measurements (if constraints pass); T's files are
                                verified after every command
    8. check_metric_gates()   — optional regression guards
    9. run_reviewer()         — optional qualitative feedback, in its own copy of T
   10. accept or discard      — commit exactly T on A, update run ref and durable history atomically
   11. stop commands          — in a copy of the accepted tip
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

## Candidate Copies

Every phase of an iteration (agent, checks, reviewer, stop) runs in **its own fresh
copy** of the code: a private folder holding one fixed tree, with its own small
`.git`. That `.git` borrows your repository's objects and has copies of its
branches, tags, remote branches and run refs (a snapshot taken when the copy is
made; no stash, no remote). This means:

- Whatever the agent does with git (commits, branches, config, hooks) stays in its copy.
  AutoHelix reads the copy from outside, through your repository, and never runs git
  inside it after a call has used it.
- What gets checked is exactly what gets published: the agent's copy is frozen to a
  tree, checks run on a fresh copy of that tree, and acceptance commits that tree.
- **Only files git would add reach checks.** Ignored files the agent built (build
  output, virtualenvs, caches, models) are deleted with the agent's copy. Constraints
  must build or install what they need; long-lived outputs such as trained models
  belong at a fixed absolute path outside the project.
- **Checks are read-only.** Any change a constraint, metric, reviewer or `ask` makes to
  the candidate's files rejects the iteration. New files are scratch, shared between
  commands in the checks phase (so build-then-test works) and deleted afterwards. If
  generated code should be accepted, generating it is the agent's job.
- In a checks copy, plain `git diff` shows changes to tracked files; new files show as
  untracked. The complete change is in `.autohelix/candidate.diff`.
- Accepted commits advance the run's hidden `refs/autohelix/<id>` first.
- A process-held per-run lock prevents concurrent writing lifecycle operations.
- PYTHONPATH is remapped so Python imports resolve to the copy (not the main repo).
- **A copy holds only committed files.** Commands get
  their tools from the environment AutoHelix started in: run it with your virtualenv
  activated so `python` and `pytest` resolve to it, and reference untracked data by
  absolute path. Copies sit outside the project, so lookups in parent directories
  (Node's `node_modules`, a `.venv` beside the project) don't find your project's.
- **Submodule changes are not supported.** Submodules appear as empty directories in a
  copy; changing one, or writing inside one, rejects the iteration.

Copies live in
`${XDG_CACHE_HOME:-~/.cache}/autohelix/copies/<project>-<hash>/<id>/` and are deleted
after each phase; the run's folder goes when a segment ends. To put them elsewhere (for
example on a larger disk), set `AUTOHELIX_COPIES_ROOT` to an absolute path. Each run
records the folder it used, so `view` and `abandon` find it from any shell. The
folders below that root are private (mode 0700, owned by you); a shared root must be
sticky like `/tmp`. Copies left by a run that crashed are removed when it resumes or
is abandoned; with no run active, the whole `copies/` folder is safe to delete. A
baseline command that modifies the code it checks (for example a formatter without
`--check`) stops the run with a setup error before any agent runs.

## Notes

The agent's persistent memory across iterations. Stored in `.autohelix/runs/<id>/notes/` and presented to the agent as `.autohelix/notes/`.

`notes/` is the **sole agent-owned region** and the only thing that round-trips
between the main project and each iteration's agent copy. Everything else the agent
sees in `.autohelix/` (observations, hints, the latest review) is read-only
reference — copied *into* the copy but never copied back. That asymmetry is
the whole contract: the agent freely structures `notes/` however the prompt
directs (per-iteration files, a journal, subdirectories), and only `notes/`
persists.

Key properties:

- **Survive rejection** — notes are saved during final cleanup, so even rejected iterations contribute learnings
- **Copied into agent copies** — the agent sees notes from all previous iterations
- **Agent-managed** — the agent decides what to write and how to organize notes
- **Retained on abandon** — `autohelix clear` aliases abandonment; `run --new` gives a new run separate state

Notes are the primary mechanism for the agent to build on previous attempts. They're more effective than full session context because the agent curates what's worth remembering.

## Scope Enforcement

AutoHelix can restrict which files the agent modifies:

- **Editable (allowlist)**: only matching files are kept
- **Frozen (denylist)**: changes to matching files are dropped, everything else is kept

Enforcement happens when the agent's copy is frozen, *before* constraints run, so constraints test exactly the tree that would be accepted. Out-of-scope changes are dropped from the candidate tree only; nothing is written back into the agent's copy.

## Agent Backends

AutoHelix supports pluggable agent backends:

| Backend | CLI Tool | Key Features |
|---------|----------|-------------|
| **Claude Code** | `claude` | Stream JSON, model selection, auto memory control |
| **Codex** | `codex` | JSON output, reasoning effort, custom settings |
| **OpenCode** | `opencode` | JSON format, model variants |
| **Mock** | (built-in) | Deterministic changes for testing |

All backends implement the same interface: receive a prompt, work in a directory, emit events for the live display.

## Reviewer

An optional second agent that provides qualitative feedback between iterations. It runs after constraints and metric gates pass but *before* acceptance into the run. Landing on the user's branch may happen later, particularly in manual mode.

It is a full agent working in its own fresh copy of the candidate, with tool access: it inspects the code itself, and `.autohelix/candidate.diff` holds the complete change. Its entire instruction is the `reviewer.prompt` you configure, plus where to write the result — it does **not** automatically receive the goal or metrics, so include whatever context you want it to consider in the prompt. Example:

```yaml
reviewer:
  prompt: |
    Review the change just made for clarity, correctness, and performance.
    What single change would most improve the result next iteration?
  # model: claude-opus-4-8   # override the model (defaults to the main agent's)
  # timeout_seconds: 300     # override the timeout
  # auto_memory: false       # independent reviews (default)
```

- The reviewer verdict is **advisory**. Ordinary errors still allow acceptance (with a placeholder review). A reviewer that changes the candidate's files invalidates its verdict and rejects the iteration. Feedback under `.autohelix/` is allowed. Backend read-only restrictions remain best effort.
- Each review is archived to `.autohelix/runs/<id>/reviews/iter-N.md`. There is no standing "latest review" file — the latest is derived from the highest-numbered `iter-N.md`.
- The **latest** review is materialized into the next iteration's agent copy as `.autohelix/review.md` so the main agent can read it; the `reviews/` archive itself is never copied into a copy.
- The intended reviewer view omits the review archive and removes the materialized latest `review.md` before its call. Agents can still reach shared project state and git metadata; the directory layout does not isolate that access. `auto_memory: false`, the default, disables Claude Code's cross-session memory.

## Prompt Template

The prompt sent to the agent each iteration is built from a [Jinja2](https://jinja.palletsprojects.com/) template. `autohelix init` writes the default template to `.autohelix/prompt.md` — reading it is the quickest way to see exactly what the agent receives each iteration (goal, constraints, metrics, where to read notes, the "write notes when done" instruction). Edit that file to customize it; if it's absent, AutoHelix falls back to the built-in default.

Available template variables:

| Variable | Content |
|----------|---------|
| `{{goal}}` | Goal from config |
| `{{iteration}}` | Current iteration number (baseline is iter-0) |
| `{{worktree}}` | Absolute path to this iteration's agent copy (write only inside it) |
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

This is a **best-effort nudge, not a security boundary.** In practice a cooperative agent respects the fence — in testing, Claude declined to read an out-of-worktree eval even when its path was visible in a log it had legitimately read. But nothing *enforces* it: copies live outside the project, but the agent runs as you, so one that is determined (or explicitly told to cheat) can still reach the project and anything else on disk by absolute path. Treat it as reducing reward-hacking pressure, not eliminating the possibility.

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
│   ├── copies                  # base folder of this run's candidate copies
│   └── runtime/status.json        # advisory live status/copy/log
└── runtime/                   # shared helper scripts and persistent lock files
```

The temporary candidate copies themselves (`iter-N-<phase>/`) live outside the
project; see [Candidate Copies](#candidate-copies).

The agent still sees a small `.autohelix/` view inside its copy. Only `notes/`
round-trips; observations, hints, peer notes and latest `review.md` are reference
material. The review archive never enters a copy.

A run resumes by config path or exact id. One invocation, called a **segment**,
holds a process lock through validation, final landing and report. Agent commits
don't matter: the candidate is the agent copy's files. Acceptance commits exactly
the checked tree, with the previous accepted tip as its parent; it leaves the
user's branch and staged files alone. No-op accepts retain the same tip.

Resuming repairs an interrupted final record and recovers accepted commits whose
record was not finished. Other history corruption is refused. User commits on
the target can be picked up when they descend from the run tip. Auto landing
fast-forwards the target at segment end before report; manual mode retains code
on the run ref until `autohelix land`. Divergence, a branch being rebased or checked
out elsewhere, and dirty files that would be overwritten require user resolution.
If the target moves during a segment, AutoHelix warns (in every landing mode),
leaves it alone and asks for an explicit land after review. Unrelated dirty and staged files are preserved.

If you reset the target behind accepted run code, auto landing would reapply that
code. Use `autohelix abandon` to discard the old run before starting `run --new`.
Only first valid baseline capture requires a clean checkout, and every readiness
check (clean checkout, editable patterns, preflight) runs before a first start
allocates a run. The baseline, stop commands and summary run in copies of the
accepted code. Agents can still reach the project, its `.git`, other runs and
`~/.gitconfig` through absolute paths: main-checkout inspection is best effort and
logs possible escapes, without reverting user edits. Copies are correctness tools
for cooperative agents, not a filesystem security boundary.

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
