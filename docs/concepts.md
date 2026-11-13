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
5. **When should the loop stop?** Set an iteration, time, or cost budget that
   matches the intended scope, and a `stop:` condition if "done" is checkable.

### Tips

- Keep gates, benchmarks and evaluation criteria outside the editable scope.
- To see exactly what the agent received, read the prompt kept in each
  iteration's folder, `.autohelix/runs/<id>/iterations/NNN/prompt.md` (the agent
  copy, which holds it as `.autohelix-iteration/prompt.md`, is gone once the
  iteration ends), or the built-in template
  (`src/autohelix/templates/default_prompt.md`). See [Prompt](#prompt).
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

Every phase of an iteration (agent, checks, each review, stop) runs in **its own fresh
copy** of the code: a private folder holding one fixed tree, with its own small
`.git`. That `.git` borrows your repository's objects and has copies of its
branches, tags, remote branches, and this run's ref and its group's (a snapshot taken
when the copy is made; no stash, no remote). This means:

- Whatever the agent does with git (commits, branches, config, hooks) stays in its copy.
  AutoHelix reads the copy from outside, through your repository, and never runs git
  inside it after a call has used it.
- What gets checked is exactly what gets published: the agent's copy is frozen to a
  tree, checks run on a fresh copy of that tree, and acceptance commits that tree.
- **Only files git would add reach checks.** Ignored files the agent built (build
  output, virtualenvs, caches, models) are deleted with the agent's copy. Gates
  must build or install what they need; long-lived outputs such as trained models
  belong at a fixed absolute path outside the project.
- **Checks are read-only.** Any change a gate command, observe command, review or `ask`
  makes to the candidate's files rejects the iteration. New files are scratch, shared between
  commands in the checks phase (so build-then-test works) and deleted afterwards. If
  generated code should be accepted, generating it is the agent's job.
- In a checks copy, plain `git diff` shows changes to tracked files; new files show as
  untracked. The complete change is in `.autohelix-iteration/candidate.diff`; that is
  the only harness material a checks copy gets (no notes, history or prompt). A review's
  copy gets the same, plus `previous.md` with `previous: true`.
- Accepted commits advance the run's hidden `refs/autohelix/<id>` first.
- A process-held per-run lock lets only one process write a run at a time.
- PYTHONPATH is remapped so Python imports resolve to the copy (not the main repo), and
  `autohelix` on a command's `PATH` is a launcher for the harness's own installation
  (see [Configuration → Command environment](config.md#command-environment)).
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
records the folder it used, so `view` and `delete` find it from any shell. The
folders below that root are private (mode 0700, owned by you); a shared root must be
sticky like `/tmp`. Copies left by a process that was killed (or a machine that
crashed) are removed at the start of the next `run` or `resume` in the project,
and other projects' leftovers once their checkout has been gone for a day (each
project's folder records which checkout it serves, and the machine); with no run
active, the whole `copies/` folder is safe to delete. A
baseline command that modifies the code it checks (for example a formatter without
`--check`) stops the run with a setup error before any agent runs.

### What copies protect against

Copies, read-only checks and the `autohelix` launcher make the loop correct
for a cooperative agent, and for candidate code that misbehaves by accident:

- what is checked is exactly what is accepted, and a check can't change it;
- a gate, entry or judge that edits the files it checks is caught, even if a
  wrapper script restores them;
- stored output (gates', observe entries' and stop commands', judge and review
  transcripts included), `$AUTOHELIX_PREVIOUS` and `$AUTOHELIX_OBSERVATIONS` are
  written or copied by the harness after the command ended, so what the next
  call reads can't be rewritten in place;
- reviews read their own copy of exactly the candidate, not files earlier
  commands left behind;
- `autohelix ask` and `autohelix review` run the harness's own installation,
  not modules from the candidate's `PYTHONPATH`;
- a command's background processes are killed when it ends.

They are not a security boundary. The agent and candidate code run as you:
they can still reach the project, its `.git`, other runs and your home folder
by absolute path, and write run state there (`.autohelix/runs/<id>/`, including
the history and observations of earlier iterations) until commands run in a
sandbox. A child process that starts a session of its own escapes the
process-group kill. Main-checkout inspection is best effort: it logs possible
escapes without reverting them. For stronger isolation use the
[Docker sandbox](docker.md).

## Notes

The agent's persistent memory across iterations. Stored in `.autohelix/runs/<id>/notes/` (a link to the latest saved version, `.internal/notes/<n>/`; each iteration's `iterations/NNN/notes` links to the version saved with it) and presented to the agent as `.autohelix-iteration/notes/`.

`notes/` is the **sole agent-owned region** and the only thing that round-trips
between the main project and each iteration's agent copy. Everything else the agent
sees in `.autohelix-iteration/` (history, observations, hints, `rejected.patch`, other runs of its group) is read-only
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

## Agent Backends

AutoHelix supports pluggable agent backends:

| Backend | CLI Tool | Key Features |
|---------|----------|-------------|
| **Claude Code** | `claude` | Stream JSON, model selection, auto memory control |
| **Codex** | `codex` | JSON output, reasoning effort, custom settings |
| **OpenCode** | `opencode` | JSON format, model variants |
| **Mock** | (built-in) | Deterministic changes for testing |

All backends implement the same interface: receive a prompt, work in a
directory, emit events for the live display. Claude Code and Codex can also
answer `autohelix ask` and `autohelix review`; OpenCode can't, as it has no
read-only mode.

## What the agent sees

Each iteration's prompt (see [Prompt](#prompt)) always holds the goal, the last
five iterations (one line each: metrics or the rejection reason, then the
agent's one-line summary), the gates, observe entries and stop commands, the
scope, where to read notes and hints, and where to write notes. Then, about the
latest iteration:

- **After an accepted iteration:** its gate results and where its observations
  are (`.autohelix-iteration/iterations/NNN/observe/<name>/`, failed entries
  marked, and an entry that ran but didn't report a declared metric named with
  how to report it). The copy holds its code.
- **After a rejection:** the rejected attempt's gate results (passed, failed,
  or not reached; a failing gate with the folder holding its whole output), the
  failure output, the observations it produced before being rejected, and
  `.autohelix-iteration/rejected.patch` (for a change over 2 MiB, the `git diff`
  command that shows it instead). The copy is still the last accepted code, and
  the prompt names the folder of the last accepted iteration too.
- **After discarded changes:** the paths that were outside the scope, frozen
  ones said apart from the ones that aren't editable.

Observe commands are different: `$AUTOHELIX_PREVIOUS` (and so
`review --previous`) always comes from the last *accepted* iteration in which
the entry succeeded, so a tracker's status moves only with accepted code.

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
    # agent: {backend: codex, model: gpt-5.5}   # a review by another model family
```

- Each review runs in its **own fresh copy of exactly the candidate**, with the
  complete change as `.autohelix-iteration/candidate.diff`: not the checks copy,
  where gate and observe commands leave files and run candidate code. Symlinks
  pointing outside the copy are removed and listed in the reviewer's prompt.
- The prompt is the reviewer's entire request: it is not handed the goal,
  metrics, notes or other reviews, so include whatever context it should
  consider.
- Several reviews are several entries, each with its own name, prompt and
  agent. List them last: a metric gate failure skips the entries after it, so
  their cost isn't spent on an iteration already rejected.
- A review is **feedback**, not a gate. A failed review (no valid answer, a
  timeout) is recorded and shown to the next agent, and the run continues. A
  reviewer that changes the files it reviews rejects the iteration.
- Read-only has three layers: the backend's read-only mode, a fingerprint of
  the copy before and after the call (a change is a sticky
  `integrity_violation` that rejects the iteration), and its own fresh copy.

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

budget: {iterations: 30, cost: 20}
```

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
(`src/autohelix/templates/default_prompt.md`), so new features reach every
project. Most configs only need `goal:`. To replace the whole prompt, point
`prompt_template:` in the config at a template file (a path relative to the
config file, so it is versioned with it); copying the built-in template is the
easiest start. `.autohelix/prompt.md` is no longer read.

The variables a template gets are a stable, documented contract:

| Variable | Content |
|----------|---------|
| `goal` | The config's goal, whole |
| `title` | Its first line, cut to about 80 characters |
| `iteration` | This iteration's number (the baseline is 0) |
| `worktree` | The agent's working directory (its copy) |
| `history` | The last iterations, one line each; empty if none |
| `gates` | Every gate in list order: `{text, kind}`, `kind` being `command` or `metric`; a metric gate's text says what it compares ("speed: at most 5% worse than the last accepted iteration"). Elsewhere (console header, `view --json`, the `gates` of history records) a metric gate goes by its short text, `speed regress 5%` or `speed improve 0%` |
| `observe` | Every observe entry: `{name, kind, metric_labels}`, `kind` being `command` (with `command`) or `review` (with `prompt`, on one line); `metric_labels` like `speed (↑)` |
| `stop` | The stop commands |
| `editable`, `frozen` | The scope patterns |
| `notes_path` | Where this iteration's notes go (in the copy) |
| `hints` | `.autohelix-iteration/hints.md` if there are hints, else empty |
| `rejected_patch` | `.autohelix-iteration/rejected.patch` if the copy has one, else empty |
| `rejected_diff` | When the latest rejected change is over 2 MiB (no patch file), the `git diff <tip> <tree>` command that shows it, else empty |
| `discarded` | The paths whose changes the latest iteration lost to the scope |
| `discarded_frozen` | Those of them that are frozen (the others aren't editable) |
| `last` | The latest iteration (the baseline before iteration 1), or none: `{iteration, accepted, reason, summary, failure_output, gates: [{text, kind, status}], observe: [{name, ok, error, missing, path}]}`; `status` is `passed`, `failed` or `pending`, `missing` the declared metrics an entry ran without reporting, and `path` the entry's folder in the copy |
| `time_limit` | `agent.timeout` as a compact duration (`15m`), or empty |
| `group`, `group_runs` | The run's group, and one line per other run of it (where its state is in the copy, iterations, latest accepted metrics, running or finished, its ref); empty outside a group |

A custom template is a copy: it won't pick up future changes to the built-in
one. Remove `prompt_template:` to return to it. A run warns when a custom
template uses variables not in this table (such as the old `constraints` or
`history_summary`; they render empty), and when it still names `.autohelix/`
paths in the agent's copy (they are now `.autohelix-iteration/`).
Agent-written text in the variables (summaries, discarded paths, errors,
failure output) has its control characters removed; summaries are cut to 200
characters.

### Use case: a soft hold-out eval

Editing the template is also how you can keep an evaluation *out of the agent's view*, useful for measuring generalization without the agent optimizing against the eval itself. Two prompt-only levers:

- **Don't list the eval command.** Drop the `observe` (or `gates`) loop from your `prompt_template:` file. The command still runs and its metric still gates and ranks the run; it just isn't named in the prompt. Its output is still in the agent's copy under `.autohelix-iteration/iterations/NNN/`: an observe entry's `observe/<name>/` (a review's transcript included), and a *failing* gate's whole `gates/<k>/` folder (`command.txt`, so the gate's command verbatim, stdout, stderr, and an `ask:` gate's judge transcript; passing gates' output is not copied). A gate's command is in every record too (`record.json`, `history.jsonl`), so leaving a gate out of the prompt doesn't hide its command. So keep what it prints minimal, and prefer an observe entry or a gate that says little when it fails.
- **Fence the agent into its copy.** Add an explicit instruction to only read and write inside `{{ worktree }}`, and keep the eval script *and* any answer key outside the project tree (reference the script by absolute path from your config). The agent then has nothing in scope to reverse-engineer.

This is a **best-effort nudge, not a security boundary.** In practice a cooperative agent respects the fence: in testing, Claude declined to read an out-of-copy eval even when its path was visible in a log it had legitimately read. But nothing *enforces* it: copies live outside the project, but the agent runs as you, so one that is determined (or explicitly told to cheat) can still reach the project and anything else on disk by absolute path. Treat it as reducing reward-hacking pressure, not eliminating the possibility.

## Runs and run state

`.autohelix/` is gitignored locally; AutoHelix leaves `.gitignore` unchanged.
State is scoped to `.autohelix/runs/<id>/`, one folder per iteration; what the
harness keeps for itself is in `.internal/` folders:

```
.autohelix/
├── runs/<id>/
│   ├── run.json                # identity (base commit, target branch, config), archived flag
│   ├── history.jsonl           # one record per iteration: the index and the source of truth
│   ├── hints.md
│   ├── report.md  report.html  # the run summary; the one-file run page
│   ├── notes -> .internal/notes/<n>   # agent memory; round-trips (link to the latest version)
│   ├── iterations/
│   │   ├── 000/                # the baseline
│   │   └── 001/ ...            # one folder per iteration (below)
│   ├── logs/                   # run logs per invocation, report.log, hook audit
│   └── .internal/              # status.json, usage/, notes versions notes/<n>/,
│                               # superseded attempts attempts/, report.json, the copies path
└── .internal/                  # the current run's id, lock files, helper scripts (bin/)
```

Every iteration's folder has the same parts; those that don't apply are absent:

```
iterations/NNN/
├── record.json                 # its history.jsonl record, pretty-printed (written last)
├── prompt.md  agent.log        # what the agent was given and did (not at the baseline)
├── notes -> ../../.internal/notes/<n>   # the notes as saved with it
├── gates/<k>/                  # gate k of gates: that ran: command.txt, stdout.txt, stderr.txt, ask.log
├── observe/<name>/             # each observe entry: stdout.txt, stderr.txt, captures, review.log
├── stop/<k>/                   # stop command k, after an accept: as gates/<k>/
└── rejected.patch              # a rejected attempt's change (also kept on its ref)
```

Numbers are three digits, so `ls` and globs sort in order (past 999 the width
grows and glob order breaks at that boundary); `ls iterations | tail -1` is the
latest. `record.json` is derived from `history.jsonl`, which stays the source
of truth (`jq` on it for numbers across iterations; see
[Stored output](config.md#stored-output) for texts). A killed iteration, or a
retried baseline, reuses its number: the earlier attempt's folder is moved to
`.internal/attempts/NNN-<k>/` first. `delete` removes all of it.

The temporary candidate copies themselves (`iter-N-<phase>/`) live outside the
project; see [Candidate Copies](#candidate-copies).

The agent's copy holds no `.autohelix/` (nothing in a copy looks like project
state). What the harness provides is in `.autohelix-iteration/`, which is never
part of a candidate (it is left out when the copy is frozen), laid out like the
run's folder:

```
<copy>/
├── <code at the run's tip>
├── .git/                       private; refs: branches, tags, this run's (and its group's)
└── .autohelix-iteration/
    ├── prompt.md  hints.md     this iteration's prompt; hints
    ├── notes/                  this run's notes: the only thing saved back
    ├── history.jsonl           this run's iterations
    ├── iterations/NNN/         the last 5 iterations and the last accepted one: record.json,
    │                           failing gates' gates/<k>/, observe/, stop/
    ├── rejected.patch          after a rejection: the rejected attempt's change
    ├── commit_summary.txt      written by the agent: the commit's first line
    └── runs/<other-id>/        other runs of the group: run.json, history.jsonl, notes/
```

A copy's `iterations/` leaves out the agent logs, prompts, `notes` links,
passing gates' output and rejected patches; `history.jsonl` covers older
iterations. Only `notes/` round-trips: when the copy is frozen it is saved as a
new version, `runs/<id>/.internal/notes/<n>/`, and the `runs/<id>/notes` link
switches to it in one step, so a note the agent deletes stays deleted and other
runs never read a half-saved mix; the iteration's `notes` link keeps pointing at
that version. Everything else is reference material. A checks copy gets only
`candidate.diff`; a review's copy `candidate.diff` (and `previous.md` with
`previous: true`); a stop copy none of it.

A run is a line of work: `autohelix run` starts one, `autohelix resume` continues
one (by default the config's most recently active), `archive` sets one aside and
`delete` removes it. A run has no other state: whether it is running comes from its
lock, how its last segment ended from `status.json`, and whether it has landed from
git. One invocation of `run` or `resume`, called a **segment**,
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

If you reset the target behind accepted run code, resuming that run with auto
landing would reapply that code: `autohelix archive` it (or `delete` it) so a bare
`resume` passes it over.
Only first valid baseline capture requires a clean checkout, and every readiness
check (clean checkout, editable patterns, preflight) runs before a first start
allocates a run. The baseline, stop commands and summary run in copies of the
accepted code. Agents can still reach the project, its `.git`, other runs and
`~/.gitconfig` through absolute paths: main-checkout inspection is best effort and
logs possible escapes, without reverting user edits. Copies are correctness tools
for cooperative agents, not a filesystem security boundary.

Archive retains evidence, unlanded commits and rejected trees, and leaves code
intact. Delete removes the run directory, its ref and its
`refs/autohelix-rejected/<id>/` refs, while lock files remain outside that
directory. Runs abandoned by earlier versions read as archived. Legacy top-level
history must be moved or deleted before run; there is no automatic migration.

## Cost Tracking

Available token usage and cost are captured from agent backends:

- **Per iteration**: shown after each accepted/rejected result
- **Cumulative**: shown in the final summary
- **Persisted**: stored in `.autohelix/runs/<id>/history.jsonl` for later analysis

Each iteration's cost includes its optimization agent and the model calls its
commands report (`ask`, `review`). Run totals and `budget.cost` also include the
baseline's. History's `calls` list records each agent and command call with
its role, usage and log path; baseline calls and failed calls count too.
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

To follow a run live, use [`autohelix view`](cli.md#autohelix-view) in a terminal, or `autohelix view --web` for this page in a browser. When a run stops (finished, interrupted, or failed), AutoHelix saves the same page as one self-contained file, `.autohelix/runs/<id>/report.html`, which opens in a browser without a server. It shows the status, the metric chart, the iteration table with rejection reasons, each iteration's gates, notes and observe entries (status and metrics; a review's text), the changes from the baseline to the final code, and the run summary when there is one (see [Configuration → Report](config.md#report)). It leaves out agent activity, per-iteration diffs, and stdout, stderr and captured files; use `autohelix view --web` for those. `autohelix report` also rewrites it.
