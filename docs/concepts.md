# Concepts

## The Iteration Loop

AutoHelix runs a loop where each iteration is isolated, validated, and either accepted or discarded:

```
for each iteration:
    1. agent copy       — fresh copy of the accepted tip A, plus notes/ and reference material
                          (and rejected.patch after a rejection)
    2. prompt           — goal, recent history, the last iteration's gates and observations,
                          gates, observe entries, stop commands, scope
    3. agent            — makes changes in its copy
    4. freeze           — snapshot the copy to a tree T; out-of-scope changes are dropped
                          (and reported); notes are saved and the copy is deleted
    5. checks copy      — fresh copy of T (HEAD at A); T's files are verified after every command
    6. gate commands    — in list order (tests, linting, ask: questions); the first failure rejects
    7. observe entries  — in list order (benchmarks, reviews), each output saved by name;
                          after each, the metric gates on its numbers; a failure rejects
    8. accept or reject — commit exactly T on A, update the run ref and history atomically;
                          or keep T as refs/autohelix-rejected/<id>/<n>
    9. stop commands    — after an accept, in a copy of the accepted tip
```

Iteration 0 is the **baseline**: the gates and observe entries run once on the
starting code, rejecting nothing, to record the reference numbers (see
[Configuration → Order within an iteration](config.md#order-within-an-iteration)).

## Designing Your Own Loop

A useful loop answers five questions:

1. **What should improve?** Define the goal and how progress will be observed.
2. **What may the agent change?** Define the editable surface.
3. **What must remain true?** Encode non-negotiable requirements as gates.
4. **What should carry forward?** Decide which metrics, artifacts, reviews,
   results, and lessons later iterations need: those are `observe:` entries.
5. **When should the loop stop?** Give an iteration, time, or cost budget that
   matches the intended scope on the command line (`autohelix run -n 30 --cost
   20`), and a `stop:` condition in the config if "done" is checkable.

### Tips

- Keep gates, benchmarks and evaluation criteria outside the editable scope.
- To see exactly what the agent received, read the prompt kept in each
  iteration's folder, `.autohelix/runs/<id>/iterations/NNN/prompt.md` (the agent
  copy, which holds it as `.autohelix-iteration/prompt.md`, is gone once the
  iteration ends), or a sample rendering of the built-in template in
  [Phases → The main agent](phases.md#the-main-agent). See [Prompt](#prompt).
- AutoHelix already retains iteration evidence and agent memory. When findings
  should also be user-facing and revisable, keep them in an editable,
  version-controlled file. The [research example](https://github.com/awslabs/AutoHelix/tree/main/examples/research) uses
  `REPORT.md` for this purpose.
- The editable content and evaluated result need not be the same. In the
  [post-training example](https://github.com/awslabs/AutoHelix/tree/main/examples/posttrain), the agent writes training
  code while the evaluator scores the resulting model. AutoHelix cannot
  restore artifacts outside git when an iteration is rejected.

See the [examples](https://github.com/awslabs/AutoHelix/tree/main/examples) for concrete loops that can be adapted or
combined.

## Candidate Copies

Every phase runs in its own fresh copy of the code, outside the project; what
each copy holds, and what is checked and committed: [Phases](phases.md).

## Notes

The agent's persistent memory across iterations. Stored in `.autohelix/runs/<id>/notes/` (a link to the latest saved version, `.internal/notes/<n>/`; each iteration's `iterations/NNN/notes` links to the version saved with it) and presented to the agent as `.autohelix-iteration/notes/`.

`notes/` is the **sole agent-owned region** and the only thing that round-trips
between the main project and each iteration's agent copy. Everything else the agent
sees in `.autohelix-iteration/` (history, observations, hints, `rejected.patch`, other runs' state) is read-only
reference — copied *into* the copy but never copied back. That asymmetry is
the whole contract: the agent freely structures `notes/` however the prompt
directs (per-iteration files, a journal, subdirectories), and only `notes/`
persists.

Key properties:

- **Survive rejection** — notes are saved during final cleanup, so even rejected iterations contribute learnings
- **Copied into agent copies** — the agent sees notes from all previous iterations
- **Agent-managed** — the agent decides what to write and how to organize notes
- **Versioned** — each save is a new version (`.internal/notes/<n>/`), so a note the agent deletes stays deleted, and the notes as of any iteration remain
- **Shared in a group** — runs started with the same `--group` see each other's notes (read-only copies, in `.autohelix-iteration/runs/<id>/notes/`)
- **Per run** — each `autohelix run` starts with empty notes; `resume` continues a run's notes; `archive` keeps them, `delete` removes them
- **Imported read-only** — a run started with `run --import <id>` sees that run's notes and history in `.autohelix-iteration/runs/<id>/`; its own notes still start empty, numbered from its iteration 1

Notes are the primary mechanism for the agent to build on previous attempts. They're more effective than full session context because the agent curates what's worth remembering.

## Scope Enforcement

AutoHelix can restrict which files the agent's changes are kept for:

- **Editable**: only matching paths are kept
- **Frozen**: changes to matching paths are dropped
- **Both**: editable minus frozen (`editable: [src/, tests/]`, `frozen: [tests/acceptance/]`)

Enforcement happens when the agent's copy is frozen, *before* any gate runs, so
gates test exactly the tree that would be accepted. Out-of-scope changes are
dropped from the candidate tree only; nothing is written back into the agent's
copy. The dropped paths are recorded with the iteration, shown in the viewer,
and named in the next agent's prompt, so it can tell why a change never lands.

With no scope, the agent may edit the tests and benchmarks that judge it; a run
with gate or observe commands but no scope warns at start.

## Agent CLIs

`agent.cli` picks the agent CLI AutoHelix runs:

| `cli:` | Executable | Key Features |
|---------|----------|-------------|
| `claude-code` | `claude` (`$AUTOHELIX_CLAUDE_CODE_CMD`) | Stream JSON, model selection, `effort` as `--effort`, auto memory control |
| `codex` | `codex` (`$AUTOHELIX_CODEX_CMD`) | JSON output, `effort` as `model_reasoning_effort` |
| `opencode` | `opencode` (`$AUTOHELIX_OPENCODE_CMD`) | JSON format, `effort` as `--variant` |

All of them implement the same interface: receive a prompt, work in a
directory, emit events for the live display. Claude Code and Codex can also
answer `autohelix ask` and `autohelix review`; OpenCode can't, as it has no
read-only mode.

## What the agent sees

What the agent's copy and prompt hold: [Phases → The agent](phases.md#the-agent).

## Rejected work

Every rejected attempt's frozen tree is kept as a hidden ref,
`refs/autohelix-rejected/<id>/<n>`, including attempts whose agent call failed
or timed out (only if freezing the copy itself fails is there nothing to keep).
A failed or timed-out attempt is rejected without checks: it is kept for the
record and the next agent, never accepted unchecked, and the loop goes on (the
next agent gets its `rejected.patch`). Three failed agent calls in a row stop
the run: "the agent failed 3 times in a row: <last error>". An accepted or
gate-rejected iteration resets the count.

An attempt whose changes were all discarded (its tree equals the commit it
started from) keeps no ref: there is nothing to keep or re-apply.

The next agent's copy gets the latest rejection's change against the tip as
`.autohelix-iteration/rejected.patch`, and the prompt says: "The previous
attempt was rejected (failure output above). Its changes are in
`.autohelix-iteration/rejected.patch`; `git apply` it if it was close, or start
over." ("(failure output above)" only when there is some.) A change larger than
2 MiB is not written as a file: the prompt gives the command that shows it
instead, `git diff <tip> <tree>` (the copy reaches the tree through its
borrowed objects), and the template gets it as `rejected_diff`.

- It always applies cleanly: a rejection doesn't move the run's ref (if the tip
  moved anyway, there is no patch).
- Whatever is re-applied is proposed and checked again, like any change.
- The patch is derived: overwritten on each rejection, absent after an accept.
- The viewer shows any rejected iteration's diff from its ref.
- Rejected trees stay in the repository until `autohelix delete` removes the
  run (`archive` keeps them), and show up in `git log --all`.

## Reviews

A review is an `observe:` entry with `review:` instead of `command:`,
shorthand for [`autohelix review`](cli.md#autohelix-review). A read-only agent
reviews the candidate; its text is stored like any entry's stdout, and the next
agent reads it with the other observations.

```yaml
observe:
  - name: review
    review: |
      Review the change for clarity, correctness, and performance.
      What single change would most improve the result next iteration?
    # agent: {cli: codex, model: gpt-5.5}   # a review by another model family
```

- Each review reads its own fresh, read-only copy of exactly the candidate (see
  [Phases → Judges](phases.md#judges-ask-and-review)).
- The prompt is the reviewer's entire request: it is not handed the goal,
  metrics, notes or other reviews, so include whatever context it should
  consider.
- Several reviews are several entries, each with its own name, prompt and
  agent. List them last: a metric gate failure skips the entries after it, so
  their cost isn't spent on an iteration already rejected.
- A review is **feedback**, not a gate. A failed review (no valid answer, a
  timeout) is recorded and shown to the next agent, and the run continues; so
  is a reviewer that changes the files it reviews (its call fails; the change
  was in its own copy).

## Progress tracking

A review with `previous: true` sees its own output from the last accepted
iteration, as a starting point: it is told to re-verify every item against the
code (against anchoring: once "done", an item tends to stay done). It sees its
own previous output only: no agent notes, no other reviews. That makes an
independent tracker: the status of every item is written by the checker rather
than the implementer, and moves only with accepted iterations.

```yaml
goal: |
  Implement every item in PRD.md
  Each iteration, take the highest-priority unfinished item, implement only
  that one, and add tests for it.

scope:
  editable: [src/, tests/]
  frozen: [PRD.md]          # the agent can't edit what is being tracked

gates:
  - pytest -q
  - ask: Does the change keep the public API?

observe:
  - name: review
    review: Review the diff for correctness and PRD intent. What is the most important next fix?
  - name: progress
    review: |
      For each PRD.md item, give done / partial / missing with file:line evidence.
      Report `missing`: the number of items that are not fully done.
    previous: true
    metrics: {missing: lower}

stop:
  - jq -e '.missing == 0' "$AUTOHELIX_METRICS"
```

Run it with `autohelix run -n 30 --cost 20`.

`metrics: {missing: lower}` adds `missing` to the review's answer schema; the
command prints it as `##autohelix[missing=3]`, so it is a number like any
other: in the terminal, the viewer's chart, and `$AUTOHELIX_METRICS` for
`stop:`. `$AUTOHELIX_OBSERVATIONS` also lets a stop command read the tracker's
report (with `grep`, or `autohelix ask` about it).

Review numbers are noisy and gameable: an LLM's count can drift between calls,
and the agent can write code or comments aimed at the reviewer. They are fine
for display and `stop:`; gating on them is not recommended.

## Prompt

The prompt sent to the agent each iteration is built from a
[Jinja2](https://jinja.palletsprojects.com/) template: the built-in one
(`src/autohelix/templates/default_prompt.md`, shown rendered in
[Phases → The main agent](phases.md#the-main-agent)), so new features reach every
project. Most configs only need `goal:`. To replace the whole prompt, point
`prompt_template:` in the config at a template file (a path relative to the
config file, so it lives beside it); copying the built-in template is the
easiest start. `.autohelix/prompt.md` is no longer read.

The variables a template gets are a stable, documented contract:

| Variable | Content |
|----------|---------|
| `goal` | The config's goal, whole |
| `title` | Its first line, cut to about 80 characters |
| `iteration` | This iteration's number (the baseline is 0) |
| `copy` | The agent's working directory (its copy) |
| `history` | The last iterations, one line each; empty if none |
| `gates` | Every gate in list order: `{text, kind}`, `kind` being `command` or `metric`; a metric gate's text says what it compares ("speed: at most 5% worse than the last accepted iteration"). Elsewhere (console header, `view ID --json`, the `gates` of history records) a metric gate goes by its short text, `speed regress 5%` or `speed improve 0%` |
| `observe` | Every observe entry: `{name, kind, metric_labels}`, `kind` being `command` (with `command`) or `review` (with `prompt`, on one line); `metric_labels` like `speed (↑)` |
| `stop` | The stop commands |
| `editable`, `frozen` | The scope patterns; `frozen` is `scope.frozen` plus the config and template files (always frozen) |
| `notes_path` | Where this iteration's notes go (in the copy) |
| `hints` | `.autohelix-iteration/hints.md` if there are hints, else empty |
| `hints_text` | The hints' text (the lines `autohelix hint` added, oldest first), else empty |
| `rejected_patch` | `.autohelix-iteration/rejected.patch` if the copy has one, else empty |
| `rejected_diff` | When the latest rejected change is over 2 MiB (no patch file), the `git diff <tip> <tree>` command that shows it, else empty |
| `discarded` | The paths whose changes the latest iteration lost to the scope |
| `discarded_frozen` | Those of them that are frozen (the others aren't editable) |
| `last` | The latest iteration (the baseline before iteration 1), or none: `{iteration, accepted, reason, summary, failure_output, gates: [{text, kind, status, path}], observe: [{name, ok, error, missing, path}]}`; `status` is `passed`, `failed` or `pending`, `missing` the declared metrics an entry ran without reporting, and `path` the item's folder in the copy if it has one there, else empty (a failed gate's `.autohelix-iteration/iterations/KKK/gates/<k>/`, an observe entry's `.autohelix-iteration/iterations/KKK/observe/<name>/`; passing gates' output isn't in the copy) |
| `last_accepted` | The latest accepted iteration (the baseline counts), or none: `{iteration, path}`, `path` being its folder in the copy (`.autohelix-iteration/iterations/KKK/`: its record and observations describe the code in the copy), or empty |
| `recent_iterations` | How many of the latest iterations the copy has folders of (5; the last accepted one is there too) |
| `time_limit` | `agent.timeout` as a compact duration (`15m`), or empty |
| `group`, `group_runs` | The run's group, and one line per other run of it (where its state is in the copy, iterations, latest accepted metrics, running or finished, its ref); empty outside a group. Members whose state failed to copy are left out |
| `imported` | The run this one continues from (`run --import`), or none (also when its state isn't in the copy, e.g. that run was deleted): `{id, short, path, description, same_code}`, `path` being its state in the copy (`.autohelix-iteration/runs/<id>/`, read-only), `description` its line as in `group_runs`, `same_code` whether its tip holds the same tree as the copy's code (false when unknown); its code is at `autohelix/<id>`. Not listed in `group_runs`, even when it is in the group |

A custom template is a copy: it won't pick up future changes to the built-in
one. Remove `prompt_template:` to return to it. A run warns when a custom
template uses variables not in this table (such as the old `constraints` or
`history_summary`; they render empty), and when it still names `.autohelix/`
paths in the agent's copy (they are now `.autohelix-iteration/`).
A template naming `worktree`, `copy`'s old name, is a config error.
Agent-written text in the variables (summaries, discarded paths, errors,
failure output) has its control characters removed; summaries are cut to 200
characters.

### Use case: a soft hold-out eval

Editing the template is also how you can keep an evaluation *out of the agent's view*, useful for measuring generalization without the agent optimizing against the eval itself. Two prompt-only levers:

- **Don't list the eval command.** Drop the `observe` (or `gates`) loop from your `prompt_template:` file. The command still runs and its metric still gates and ranks the run; it just isn't named in the prompt. Its output is still in the agent's copy under `.autohelix-iteration/iterations/NNN/`: an observe entry's `observe/<name>/` (a review's transcript included), and a *failing* gate's whole `gates/<k>/` folder (`command.txt`, so the gate's command verbatim, stdout, stderr, and an `ask:` gate's judge transcript; passing gates' output is not copied). A gate's command is in every record too (`record.json`, `history.jsonl`), so leaving a gate out of the prompt doesn't hide its command. So keep what it prints minimal, and prefer an observe entry or a gate that says little when it fails.
- **Fence the agent into its copy.** Add an explicit instruction to only read and write inside `{{ copy }}`, and keep the eval script *and* any answer key outside the project tree (reference the script by absolute path from your config). The agent then has nothing in scope to reverse-engineer.

This is a **best-effort nudge, not a security boundary.** In practice a cooperative agent respects the fence: in testing, Claude declined to read an out-of-copy eval even when its path was visible in a log it had legitimately read. But nothing *enforces* it: copies live outside the project, but the agent runs as you, so one that is determined (or explicitly told to cheat) can still reach the project and anything else on disk by absolute path. Treat it as reducing reward-hacking pressure, not eliminating the possibility.

## Runs and run state

A run's files are in `.autohelix/runs/<id>/` and its copies outside the
project; [Phases → Where things live](phases.md#where-things-live) has the
layout.

A run is a line of work: `autohelix run` starts one, `autohelix resume` continues
one (by default the config's most recently active), `autohelix stop` ends a
running one after its current iteration, `archive` sets one aside and `delete`
removes it. **A run keeps its config**: the one it stored when it started
([CLI → Config files](cli.md#config-files), which also says what is frozen;
[CLI → `autohelix resume`](cli.md#autohelix-resume) says when `resume` refuses
an edited one). `run --import <id>` starts a new run
that also sees another run's notes and history
([CLI → Continuing from another run](cli.md#continuing-from-another-run)). A run has no other state: whether it is running comes from its
lock, how its last segment ended from `status.json`, and whether it has landed from
git. One `run` or `resume` command, called a **segment**,
holds a process lock through validation, final landing and report. Agent commits
don't matter: the candidate is the agent copy's files. Acceptance commits exactly
the checked tree, with the previous accepted tip as its parent; it leaves the
user's branch and staged files alone. No-op accepts retain the same tip.

Resuming repairs an interrupted final record and recovers accepted commits whose
record was not finished. Other history corruption is refused. **`resume` only
continues the same run**: its stored config, its own tip, more budget; what it does when
the target branch has moved past the run's tip is in [CLI → `autohelix resume`](cli.md#autohelix-resume). Auto landing
fast-forwards the target at segment end before report; manual mode retains code
on the run ref until `autohelix land`. Divergence, a branch being rebased or checked
out elsewhere, and dirty files that would be overwritten require user resolution.
If the target moves during a segment, AutoHelix warns (in every landing mode),
leaves it alone and asks for an explicit land after review. Unrelated dirty and staged files are preserved.

If you reset the target behind accepted run code, resuming that run with auto
landing would reapply that code: `autohelix archive` it (or `delete` it) so a bare
`resume` passes it over.
Only first valid baseline capture requires a clean checkout, and every readiness
check (clean checkout, preflight) runs before a first start
allocates a run. The baseline, stop commands and summary run in copies of the
accepted code. Agents can still reach the project, its `.git`, other runs and
`~/.gitconfig` through absolute paths: main-checkout inspection is best effort and
logs possible escapes, without reverting user edits. Copies are correctness tools
for cooperative agents, not a filesystem security boundary.

Archive retains evidence, unlanded commits and rejected trees, and leaves code
intact. Delete removes the run directory, its ref and its
`refs/autohelix-rejected/<id>/` refs, while lock files remain outside that
directory. A run folder missing a file every release writes (its stored
`config.yaml`) was made by a development build: it is refused, with the advice
to delete it. Legacy top-level
history must be moved or deleted before run; there is no automatic migration.

## Cost Tracking

Available token usage and cost are captured from the agent CLIs and shown:

- Per iteration (after accepted/rejected). A `+` after the amount (`$0.10+`) means part of that iteration's cost (agent or a command's model call) is unavailable, so the amount is what is known.
- Cumulative in the final summary
- Persisted in `.autohelix/runs/<id>/history.jsonl`

What each CLI reports:

- **Claude Code**: input/output tokens, cost (USD), duration
- **Codex**: input/output and available cached-input, cache-write, and reasoning
  tokens; calculated API-equivalent cost for recognized models (below); measured
  duration
- **OpenCode**: input/output tokens, cost (per step, accumulated)

Iteration costs include the optimization agent and model calls made by the
commands the harness runs (`ask:` gates and `review:` entries, or
`autohelix ask` and `autohelix review` anywhere in `gates:`, `observe:` or
`stop:`). Run totals and `--cost` also include the baseline's commands.
Run totals also count every summary call, recorded in
`.internal/usage/summary.jsonl` (one line per call; one that never returned
stays unknown; see [CLI → `autohelix report`](cli.md#autohelix-report)).

History's `calls` list records each attempted agent call and command model call
with its role, usage and log path (relative to the run directory); it is the
only place usage is stored. Records from earlier versions may list `reviewer`
calls; they count the same way. Every agent call's usage (the main agent's, a
judge's, the run summary's) records `cli`, `model` (the model the CLI's output
named, `null` when it named none) and `executable` (the resolved path started):
which model ran, whatever `model:` asked for. `view`'s run screen and the web
viewer's run page show the latest main-agent call's model.

A command of your own can report its model calls the same way: append one JSON
line per call, e.g. `{"id": "...", "source": "my-eval", "cost_usd": 0.12}`, to the
file named by `$AUTOHELIX_USAGE_FILE` (set for every command the harness runs).
Lines with the same `id` are one call (the last line wins), so a call can be
recorded with `"cost_usd": null` when it starts and its cost when it ends.

Interrupted or failed main-agent attempts are recorded as rejected iterations,
including command calls and any available usage; they consume their iteration
number. Failures before an agent call consume no iteration. Failed baseline
attempts keep their costs in iteration 0 for a clean retry. A process killed
before a durable history write can still lose unreturned usage; recovered
accepted commits record that charge as unknown.

The numeric field is `usage.cost_usd` for every CLI. `usage.cost_source` is
`backend` when the CLI supplies the amount, or `calculated` when AutoHelix
calculates it. Backend-supplied amounts can themselves be estimates; they are
not necessarily invoice totals. Older history without this metadata still loads.

**Codex costs are estimated** from reported input, cached-input, cache-write,
and output token counts and a bundled snapshot of the model's published
[OpenAI API rates](https://developers.openai.com/api/docs/pricing).
For subscription and Bedrock users, these are API-equivalent amounts, not
subscription charges or provider bills. The normal display uses the same
**Cost** label for every CLI; provenance is saved in history:

- `cost_model` identifies the model priced.
- `cost_basis: openai_standard_api` identifies Standard pricing at ordinary
  context lengths, with 30-minute cache writes.
- `cost_rates` saves the rates per million tokens, cache-write lifetime, and
  verification date used for that iteration.
- `output_includes_reasoning` records whether separately reported reasoning
  tokens were already included in output. They are not counted twice.

The snapshot was verified on November 6, 2026. It supports listed GPT-6 models
and common GPT-5 Codex models; unknown names and expiring promotional prices are
not guessed. The model priced is the one Codex's JSON output names, else
`agent.model`; with neither, the cost is unavailable ("set agent.model").
AutoHelix doesn't read Codex's session files or guess from `extra_args`: a model
chosen there (`-m`, a profile) is priced as `agent.model` says, so set
`agent.model` to the model you run.

Estimates exclude speed-tier and regional premiums, per-request long-context
surcharges, separately billed tools, and usage outside the captured thread.
Cache-write lifetimes other than 30 minutes are not inferred. External
evaluator calls count when they record usage through `$AUTOHELIX_USAGE_FILE`.
Codex agent duration is measured locally even when usage is unavailable.

Missing, invalid, or interrupted usage remains **unavailable**, not zero, and
totals with unavailable parts are labeled **incomplete**. `--cost` counts the
saved amounts (Codex estimates included) and is checked between iterations, so
it is an approximate stopping threshold, not a guaranteed spending cap; what it
does when a cost is unavailable is in [CLI → Budgets](cli.md#budgets).

## Run page

To follow a run live, open this page with [`autohelix view <id>`](cli.md#autohelix-view), or use `autohelix view --terminal` in a terminal. When a run stops (finished, interrupted, or failed), AutoHelix saves the same page as one self-contained file, `.autohelix/runs/<id>/report.html`, which opens in a browser without a server. It shows the status, the metric chart, the iteration table with rejection reasons, each iteration's gates, notes and observe entries (status and metrics; a review's text), the changes from the baseline to the final code, and the run summary when there is one (see [CLI → `autohelix report`](cli.md#autohelix-report)). It leaves out agent activity, per-iteration diffs, and stdout, stderr and captured files; use `autohelix view <id>` (the live page) for those. `autohelix report` also rewrites it.
