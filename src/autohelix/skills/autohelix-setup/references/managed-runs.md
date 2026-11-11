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

Read the id from `.autohelix/current` (or pass a run id to `view`, `watch` and `report`), then read `.autohelix/runs/<id>/history.jsonl`, the current backend log under
`.autohelix/runs/<id>/logs/iter-N/`, captured observations under
`.autohelix/runs/<id>/observations/iter-N/`, and persistent `.autohelix/runs/<id>/notes/iter-N.md`.
`autohelix watch` provides a terminal view; `.autohelix/runs/<id>/runtime/status.json` records
the run's state and phase, and `.autohelix/runs/<id>/output/report.html` is saved when it stops. If watch does not present the selected
backend's activity use its logs and saved records. Infer active execution from
the launched process, not from the configured iteration count or old logs.

Distinguish an agent's exploratory results from the harness's recorded
acceptance measurements. A rejected candidate is an experiment outcome; a
failed agent or evaluator may indicate an infrastructure problem. Explain the
evidence before deciding what to do next. Keep the agreed scope, evaluators,
acceptance rules, and budgets fixed while running unless the user authorizes
a change. The supervisor observes and steers; iteration agents make candidate
changes.

## Give useful hints

When new evidence suggests a useful next step, use the installed hint command:

```sh
autohelix hint --path PROJECT 'H1: Evidence and suggested next step.'
```

Hints are copied into subsequent worktrees; they do not interrupt the current
turn. Send a hint before the next worktree is prepared. Use a recognizable ID
and briefly record its evidence and rationale outside the editable candidate.
Avoid repeating advice the iteration agents already follow.

If steering matters to the user's request, confirm later reading and use from
the agent log or notes. The hint command succeeding only confirms it was
queued. Ask for acknowledgment in the harness-requested `iter-N.md` notes when
that would make the intervention useful to inspect.

## Finish the execution and hand off

Observe the process exit and inspect the final saved outcomes before reporting
completion. Account for background work you launched; do not leave it running
without explaining its state. Report accepted/rejected attempts, actual
measurements, retained changes, failures, and useful evidence paths. Execute
post-run checks only under the entrypoint's existing cheap/authorized policy.

Use available usage records and the [accounting boundaries](config.md#budgets-and-run-state).
Missing dollar usage is unknown, not zero; supervisor/setup expenditure is
separate from the current loop counter. Do not start additional reviewer,
report, or improvement calls merely to complete the handoff when existing
records suffice.

An invocation ending is a checkpoint, not proof that an open-ended experiment
is finished. Explain why this execution ended and what remains uncertain.
For requested resumption, use `autohelix resume <id>`, never `autohelix run`:
`run` always starts a new run with fresh notes and a fresh budget. Prefer
`--until N` (stop at iteration N, safe to repeat) over `-n` (N more
iterations) in scripts and retries. `resume` exits 3 when there is no run to
resume. A failed actual main-agent call consumes its number; resume at the next
iteration. Do not archive or delete runs, reset accepted code, or extend the
agreed effort without authorization.
A run that finishes writes its summary to `.autohelix/runs/<id>/output/report.md`; after an
interrupted run, `autohelix report` writes one when requested.
