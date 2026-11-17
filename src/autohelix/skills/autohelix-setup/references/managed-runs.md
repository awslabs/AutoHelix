# Managing a bounded AutoHelix run

Use this workflow when the user asks you to prepare, run, and supervise an
experiment, or to supervise an existing run. A setup-only request finishes with
the configuration and verification handoff. Respect any request to launch
without ongoing supervision.

## Launch and observe

Use the agreed configuration, agent/model, and effort limits. Follow the
entrypoint's evaluator-implementation and execution rules. Before launching,
inspect git state and include only the intended setup changes in any authorized
commit; preserve unrelated work. Resolve routine startup problems within scope,
and explain or ask about repairs that would change the agreed experiment.
Once the loop runs, leave candidate edits and commits to the harness workflow:
iteration agents leave edits uncommitted, and AutoHelix validates and commits
accepted changes. Do not add supervisor commits or alter the base between
iterations. Preserve that rule when adapting a custom iteration prompt.

Run the installed CLI in the target project and retain a process/session handle
and its output so you can observe completion. Use the runtime's background or
session tools when available. If the environment cannot support continued
monitoring, explain that limit and give a concrete handoff; do not claim that
supervision completed.

Take the run id from the `started` event of `autohelix wait` (its `run` field; or the run's header
line, `AutoHelix · run k3f9 · …`) and always pass it: `view ID --json`, `report ID`, `hint --run ID`, `stop ID`.
Then read `.autohelix/runs/<id>/history.jsonl`, the current backend log
`.autohelix/runs/<id>/iterations/NNN/agent.log` (NNN: the iteration, three digits), observations under
`.autohelix/runs/<id>/iterations/NNN/observe/<name>/` (one folder per observe entry, reviews
included), gate and stop command output under `iterations/NNN/gates/<k>/` and `stop/<k>/` (k: the
entry's 1-based position in `gates:` / `stop:`), and persistent `.autohelix/runs/<id>/notes/iter-N.md`
([reading runs](reading-runs.md) has the fields and how to explain them).
`autohelix view ID --json` (or `--once`) prints the run's state and iterations once
(without ID, every run's row: state, progress, metrics, no iterations); a bare `autohelix view` (a web page) and `view --terminal` (a live
terminal view) do not exit, for people; `view ID --json` has the run's state
and phase, and `.autohelix/runs/<id>/report.html` is saved when it stops. If the view does not present the selected
backend's activity use its logs and saved records.

Wait for events with `autohelix wait` instead of polling files or the process.
Take a cursor before launching (`autohelix wait -p PROJECT --timeout 1s` prints
a `timeout` event with a `cursor` and exits 2), then repeat `autohelix wait -p
PROJECT --after CURSOR --timeout 10m`, each time with the latest event's
`cursor`. Each call blocks until the next event and prints it as one JSON line:
`started` (a segment), `iteration` (`accepted`, `reason`, `metrics`, `summary`,
`cost_usd`), `ended` (`state`, `stop_reason`, `error`) or `crashed` (its process
is gone); a `timeout` event (exit 2) is a heartbeat, so check the launched
process then. The cursor makes the loop miss and repeat nothing however long you
inspect between calls. With run ids, an `idle` event says none of them is
running; any run you launch (`run` or `resume`) is idle until its segment starts
(its first second or so), so wait for its `started` event without ids. A crashed
run on another host looks running: only the timeout ends that wait. Exit 1 (nothing on stdout) is an error.

To end a run early, when the user asks or evidence shows further iterations
would be wasted (a broken evaluator or environment; say why), use `autohelix
stop -p PROJECT ID --why 'one line'`: it ends after its current iteration and
lands and reports as at any segment end. `--now` also interrupts the current
iteration (recorded as rejected; no summary), on the run's host only. `wait`
then reports its `ended` event.

Distinguish an agent's exploratory results from the harness's recorded
acceptance measurements. A rejected candidate is an experiment outcome; a
failed agent or evaluator may indicate an infrastructure problem. Explain the
evidence before deciding what to do next. Keep the agreed scope, evaluators,
acceptance rules, and the agreed budget (the launch command's `-n`/`--until`,
`--cost`, `--time`) fixed while running unless the user authorizes a change. The supervisor observes and steers; iteration agents make candidate
changes.

## Give useful hints

When new evidence suggests a useful next step, use the installed hint command:

```sh
autohelix hint -p PROJECT --run ID 'H1: Evidence and suggested next step.'
```

Hints are copied into the next iteration's copy (`.autohelix-iteration/hints.md`);
they do not interrupt the current turn. Send a hint before the next iteration's
copy is prepared. Always name the run with `--run` (without it the hint goes to the running run,
else the most recently active one, which is a guess when you started several). Use a recognizable ID
and briefly record its evidence and rationale outside the editable candidate.
Avoid repeating advice the iteration agents already follow.

If steering matters to the user's request, confirm later reading and use from
the agent log or notes. The hint command succeeding only confirms it was
queued. Ask for acknowledgment in the harness-requested `iter-N.md` notes when
that would make the intervention useful to inspect.

## Finish the execution and hand off

Observe the run's `ended` (or `crashed`) event and the process exit, and inspect
the final saved outcomes before reporting completion. Account for background work you launched; do not leave it running
without explaining its state. Report accepted/rejected attempts, actual
measurements, retained changes, failures, and useful evidence paths. Execute
post-run checks only under the entrypoint's existing cheap/authorized policy.

Use available usage records and the [accounting boundaries](config.md#budgets-and-run-state).
Missing dollar usage is unknown, not zero; supervisor/setup expenditure is
separate from the current loop counter. Do not start additional review,
report, or improvement calls merely to complete the handoff when existing
records suffice.

A segment (one `run` or `resume` command) ending is a checkpoint, not proof that an open-ended experiment
is finished. Explain why this execution ended and what remains uncertain.
For requested resumption, use `autohelix resume <id>`, never `autohelix run`:
`run` always starts a new run with fresh notes. `resume` only continues the
same run (its stored config, its own tip): to apply an edited config, to keep
going after the target branch moved past the run (it landed and the user
committed more), or to fork from an old run (`git checkout autohelix/<id>`
first), start a new run that continues from it, `autohelix run <config> --import
<id>`: it sees that run's notes and history read-only and continues its
observations at the baseline. When the branch moved past a run that lands
automatically, `resume` refuses; `autohelix resume <id> --land manual`
continues the same run from its own tip without landing. Budgets count each
segment: `resume -n 5 --cost 3` is 5 more iterations, up to $3 more, and a
`resume` without budget flags runs 1 more. Prefer `--until N` (stop at
iteration N in total, safe to repeat) over `-n` (N more iterations) in scripts
and retries: a retried `-n` would run N again. `--until` makes retries safe for
iterations only: `--cost` and `--time` start fresh on each segment, so track
the total yourself (`run.progress.cost_usd` in `autohelix view ID --json`) and
pass only the remainder. `run` and `resume` exit 0 when the segment ended
by a budget, a `stop:` condition or `autohelix stop`; 1 on an error (no run to
resume, failed preflight checks, the agent failing three times in a row, a
landing that failed); 130 when interrupted (Ctrl-C, `autohelix stop --now`). A failed actual main-agent call consumes its number; resume at the next
iteration. Do not archive or delete runs, reset accepted code, or extend the
agreed effort without authorization. Record why you start or archive a run, one
line each, so `autohelix view` explains it: `autohelix run a --why "baseline for the
cache idea"`, `autohelix archive <id> --why "lost to k3f9 on speed"`.
A run that finishes writes its summary to `.autohelix/runs/<id>/report.md`; after an
interrupted run, `autohelix report` writes one when requested.

## Orchestrate several runs

**Compare** approaches (models, prompts, or the same setup several times) with
separate runs that land manually: they must not share notes or code, or the
comparison measures their mix. Write one config per approach
(`autohelix/a.yaml`, `autohelix/b.yaml`; runs are named after the file; repeats
of one setup share a config), commit them, and start each run in its own
background session with the same budget flags:

```sh
autohelix run a -p PROJECT --land manual -n 10 --why "approach a"
autohelix run b -p PROJECT --land manual -n 10 --why "approach b"
```

Without session tools, `nohup autohelix run a -p PROJECT --land manual -n 10 >
a.log 2>&1 &` works too (`autohelix stop <id>` ends it; Ctrl-C doesn't reach it).
`run` and `resume` keep the machine awake until their segment ends (macOS
`caffeinate`, Linux `systemd-inhibit`, nothing where the tool is missing or
refused), but a closed lid still sleeps and they don't keep credentials or a VPN
alive: say so when the user leaves a laptop to run overnight.

Follow them with `autohelix view --json` (`view --once` for a table, `view --once
--metric NAME` to compare one metric). Each run keeps its own budget, stop
conditions and cost. When they finish, compare the latest accepted metrics and
diffs (`git diff main autohelix/<id>`), land the winner with `autohelix land
<id>`, and archive the rest with the reason (`autohelix archive <id> --why
"lost to k3f9 on speed"`). Report which run won and why.

**Collaborate** with a group when runs should build on each other's work:
runs started with the same `--group NAME` see each other's notes and history
(copied into each iteration's `.autohelix-iteration/runs/<id>/`) and code
(refs `autohelix/<id>` in the iteration's git), and land manually by default.
There are no group commands: when they finish, land the run to keep and
archive the rest (an archived run leaves the group). Use `resume <id> --group
NAME` to add an existing run (`--group ''` removes it).

To **continue or fork** from a run (the winner, after more commits on the
branch, or with an edited config), start a new run with `autohelix run
<config> --import <id>` (see above); it is not a group membership.
