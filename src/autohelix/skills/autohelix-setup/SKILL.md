---
name: autohelix-setup
description: "Design, configure, or manage an AutoHelix experiment for an existing project, including requests such as 'use AutoHelix to improve X', or explain its existing runs. Use for setup-only preparation, reading and explaining runs, requested launch, or supervision of a bounded run."
---

# Set up an AutoHelix run

Turn the user's goal into an agreed, validated, committed experiment
configuration, ready to run. Propose it in the conversation before changing
project files; discuss revisions, then save and commit the agreed setup.
Use the installed `autohelix` CLI; a source checkout is not required.
Setup ends with the configuration and verification handoff. Launch or
supervise only when the user requests it.

AutoHelix gives each attempt a private copy of the code, runs configured checks
and measurements on what it froze, and commits accepted changes to the run. Each iteration starts a fresh agent
session, with persistent notes and measurements from earlier attempts.

## Questions about existing runs

When the user asks about the project's runs (bare `autohelix`'s "Ask … about
these runs"), read [reading runs](references/reading-runs.md) and answer from
the run records: no setup question first. Change nothing unless asked. When
they want a new or changed experiment, continue with the setup below.

## Understand the experiment

Unless the user has already said what they want to accomplish, your first message
is exactly this line and nothing else (no preamble, no mention of this skill):

What do you want to accomplish?

Don't inspect anything before the answer.

Inspect the project's instructions, git state, relevant code, existing
evaluators, and any AutoHelix configs (`autohelix.yaml`, `autohelix/*.yaml`).
Preserve existing work and configuration.
Use the Python and `autohelix` CLI the session's first prompt names, else the
`autohelix` on `PATH` (a plain `python` may lack the package: pipx, `uv tool`,
a venv that isn't active). Check `autohelix --help` and relevant subcommand
help against the installed version. The harness and the target project may use
different environments. The user docs are at the path the session's first
prompt gives; read them when needed.

Recommend, don't interview. From the user's answer and the project (its
tests, benchmarks, instructions), choose scope, gates, observe entries, backend,
and the budget of the launch command. Ask before proposing the config only when the project
can't tell you something that changes the loop materially, such as there being
no way yet to check or measure the goal. Don't ask about budgets, timeouts,
models or reviews: pick defaults, say what you picked. Carry forward any
preferences the user states.

Budgets are not config: they are flags of `autohelix run` and `resume` (`-n N`
iterations, `--cost USD`, `--time DURATION`, for each `run` or `resume` command), so the config never
has a `budget:` key (it is an error). The iteration count follows the goal: `-n 3`
for an open-ended goal ("make X faster", "improve Y"), `-n 1` for a single task
that gates (commands or `ask:`) can check (`-n 1` is also what no flag means). A
`review:` entry never rejects: its feedback goes to the next iteration, which a
`-n 1` run never has, so a goal checked only by a review needs more iterations.
Landing and the run summary are flags too, never config keys (`land:` and
`report:` are errors): a run lands automatically (fast-forwards the branch it
started on when the command ends) unless the launch command says `--land
manual`. Recommend `--land manual` for complex work: several iterations,
changes across many files, design or refactoring work, anything the user would
review as a pull request (the gates can't be the whole review), and when the
user wants to review the code before it reaches their branch, will keep
committing on that branch while the run goes, or is comparing approaches in
separate runs; `--group` runs land manually already. Automatic landing suits
simple tasks the gates fully check (a fix with a reproducer, a measured
speedup with tests). Add `--no-report` only when the user asks to skip the summary (it isn't stored:
hand it over on every `resume` command too). Defaults
otherwise: no cost or time limit, and no timeouts (no `timeout:` on gates or observe entries, no `agent.timeout`):
the built-in defaults apply. No metric gates (`- metric: … regress:/improve:`)
either: the agent sees the numbers and decides. Set any of these only when the
user asks. The
iteration prompt already tells the agent to leave its edits uncommitted; don't
repeat that in `goal:`.

For help choosing gates, measurements, feedback, and budgets, read
[loop design](references/loop-design.md). For concrete patterns, read the
relevant [examples](references/examples.md). A qualitative goal can use captured
evidence and review feedback without inventing a numeric score.

## Propose and agree on the configuration

Read [the configuration reference](references/config.md) when drafting the
configuration.

Include the intended path in the proposal, using the path you were given
(the session's first prompt may name it). Without a path, recommend
`autohelix.yaml` for the project's one loop, or `autohelix/<name>.yaml` when
it has or will have several. Configs live only there. Don't propose
`autohelix.yaml` when `autohelix/` already has configs: it would take over a
bare `autohelix run`. A name uses lowercase letters, digits
or hyphens, at most 32 characters, starts with a letter or digit, and isn't
`autohelix`. If the target holds the commented template, propose completing it
there. Naming, and prompt templates and helper scripts in `autohelix/` (committed with
the config), are under Config files in the [configuration
reference](references/config.md). Preserve other configs and the run state in
`.autohelix/`; propose converting a config in the first format (`constraints:`,
`metrics:`, `reviewer:`, ...), which no longer loads.

Define a goal (its first line is the run's title), editable scope, gates,
observe entries (commands, optional reviews), and optional metric gates or a
stop condition. Keep tests,
benchmark definitions, evaluation criteria, and `autohelix/` outside the
editable scope unless changing them is part of the user's task.

When a new or materially changed evaluator, check, or benchmark is needed,
include its design in the proposal: what it measures or checks, representative
cases and limits, likely runtime/resource/model cost, and its acceptance or
feedback role. Label proposed commands as unimplemented rather than putting
invented paths into YAML as though ready to run.

Use project-relative paths for code and check commands so they resolve inside
iteration copies. Check commands must use the intended environment and load
the candidate code, rather than accidentally importing an editable install from
the original checkout. An absolute path to an agent or Python executable can
select the intended environment; candidate code, evaluation inputs, and ordinary
capture outputs should still resolve in the copy. Make external output
locations deliberate: rejecting code does not restore external files.

Show the complete proposed YAML in a fenced block, including the full goal and
every configured field. For an existing config, show the proposed replacement
and summarize the changes. For newly generated configs, use this top-level
order, omitting unused fields: `goal`, `scope`, `gates`, `observe`, `stop`,
`agent`, `prompt_template`. This is a presentation
convention, not a validation requirement. Preserve an existing config's order
unless reorganizing it is requested. Keep the order of entries within `gates`,
`observe`, and `stop`: it affects execution.

Follow the YAML with a numbered, section-by-section
walkthrough in configuration order: what each setting controls, why it was
chosen, and how changing it would affect the run. Cover editable and frozen
scope, each gate, observations and evidence paths, metric directions and metric
gates or their absence, the backend, and effective defaults, including omitted
fields that affect the run; then the suggested launch command and its budget.
Distinguish instructions to the agent from enforced checks, and measurements
from acceptance decisions. Explain what has been inspected and what remains
unverified. Include any proposed helper scripts, prompt templates, or evaluators
and their intended paths. For explanation examples, see
[the configuration reference](references/config.md).

Keep project files unchanged during discovery, proposal, and revisions: don't
write configs, implement helpers or evaluators, or make commits yet.
For design-only requests, finish with the proposal. Otherwise ask whether to
save and commit or revise; honor requests to save without committing.
Discuss revisions and show the updated proposal until agreed. Agreement
authorizes the proposed setup files and their commit, unless the user requests
otherwise. If prior authorization already covers the concrete configuration
and these actions, proceed without asking again.

## Save, verify, and commit the agreed setup

Write the agreed config yourself at its intended path. Implement only the agreed helpers,
templates, and evaluators. Resolve routine validation errors within the agreed
design; return to the proposal if a fix would materially change the experiment.

Always validate the YAML and inspect its structure, declared metric names and
gate references, scope/input paths, command and dependency setup, working
directory/import paths, and expected output/capture locations without executing
the configured commands. Inspect how declared metrics would be emitted (only
`##autohelix[name=value]` lines on stdout count); output files may not exist
until evaluation. Check the config from the project root with `autohelix run
--check` (`autohelix run perf --check` for `autohelix/perf.yaml`; use the
launcher the session's first prompt names if `autohelix` isn't on PATH). It
makes every check `autohelix run` makes before writing anything (the config,
the prompt template, a clean checkout, the agent command, scope patterns),
starts nothing and exits 0 when the run would start. Before the commit it may
only report the uncommitted setup files; check again after committing.

Resolve validation errors and explain material warnings. This static review is
separate from executing checks, measurements, or evidence-gathering commands.

Automatically execute those commands only when they are known to be cheap in
time, resources, and model cost and fit the requested scope. For expensive work
or uncertain cost/duration, describe the proposed verification and likely cost
if known, then ask before executing unless that execution is already explicitly
authorized by the user's task or budget. A bounded timeout does not establish
cheapness. Apply this distinction to tests, training, benchmarks, import probes,
and reviews or `ask` gates alike.

A baseline or review call is not required to finish setup. When a command is
run, report its result and any output/metric/capture failures. Otherwise record
that execution was not performed and which behavior remains unverified.

After validation, inspect git state and commit only the agreed setup files,
using explicit paths. Preserve unrelated work, including staged changes.
Commit the config so it's versioned, and any scripts its commands use:
iteration copies are made from the commit. Honor a request to leave files
uncommitted. If the repository or commit is unavailable, report what remains
to make the setup ready to run.

## Hand off or start the run

Keep the final handoff brief: saved config and helper paths, the setup commit
(or why no commit was made), the `autohelix run NAME --check` result after
the commit, other validation and execution results, and what remains
unverified. End with the complete launch command, budget and landing
included, and one line on why: e.g. `autohelix run perf -n 3 --land manual`
("a first look at an open-ended goal; review the code before it lands, then
`autohelix land <id>`"), or `autohelix run fix -n 1` ("one task the gates
fully check: it lands when the command ends"). Always write the flags out,
`-n 1` included. Add `--cost` or `--time` only for a reason (expensive
evaluations, a deadline); `autohelix resume <id> -n N` gives more later. The complete YAML and rationale
were shown in the proposal; repeat them only if requested or needed to explain
a revision.

Don't start a run unless the user asks for one. The project must be a git
checkout with a commit, and the first baseline needs no uncommitted changes to
tracked files; identify unrelated changes that still prevent launch without
committing or discarding them. `autohelix run` creates `.autohelix/` itself.
The launch command is `autohelix run` for `autohelix.yaml`, or
`autohelix run <name>` for `autohelix/<name>.yaml`, followed by its budget flags
(and `--land manual` when chosen).
Keep the iteration prompt's instruction to leave candidate edits uncommitted:
AutoHelix validates and commits accepted changes. A setup commit before launch
is different from an iteration agent or supervisor making extra commits while
the loop runs.

For a requested launch, use given/existing bounds and resolve material budget
ambiguity as described in the loop-design reference.

Use `autohelix view ID --once` or `--json` (a snapshot; they exit) to inspect a run, and
`autohelix view --once` / `--json` for all of the project's runs; a bare `autohelix view`
serves a web page and `view --terminal` follows a run live, for people (neither exits).
Use `autohelix hint --run ID` to steer the next iteration. History, observations (reviews included), notes, the run summary
(`report.md`), and the run page (`report.html`) are stored under `.autohelix/runs/<id>/`.

## Manage a requested run

For requested supervision, read [managed runs](references/managed-runs.md).
Launch within the agreed scope and effort limits, monitor the actual process
and saved outcomes, and send useful evidence-based hints for later iterations.
Continue through termination or explain a concrete blocker; launching a
background process alone does not finish a management request. Hand off the
actual accepted changes, results, failures, and remaining limits.
