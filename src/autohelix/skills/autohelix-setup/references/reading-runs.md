# Reading and explaining runs

Use this when the user asks about existing runs: what happened, why an
iteration was rejected, whether a run is making progress, what it cost.
Answer from the records; everything below only reads `.autohelix/`, so it is
safe beside a running run. The user docs have the full field lists
(`concepts.md`, "Runs and run state"; `cli.md`, "JSON output").

## Find the run and its records

- `autohelix view --once` prints the runs table (one line per run: short id
  and name, state, iterations, a metric, landing, cost, activity, goal);
  `view --json` has every run, archived ones too: `status` (`state`:
  `running`, `finished`, `interrupted`, `failed` or `crashed`; `phase`,
  `iteration`, `stop_reason`), `accepted`, `metrics` (latest accepted values
  with their `baseline` and `direction`), `progress`, `landing`, `why`.
- `autohelix view ID --once` prints one run's screen (one row per iteration,
  why each rejection was rejected); `view ID --json` has the run's row plus
  `iterations`: `iteration`, `accepted`, `metrics`, `summary`, `reason`,
  `cost`, `cost_incomplete`, `seconds`, `gates`, `observe`, `stops`,
  `discarded`. Pass the id (or its last part) to every command.
- `.autohelix/runs/<id>/` holds the run: `run.json` (name, `config_path`,
  `base_commit`, `target_branch`, `land`, `group`, `import`, `why`,
  `archived`), `config.yaml` (the config it runs with, stored at its start;
  the file in the checkout may have changed since), `history.jsonl` (one
  record per iteration, `0` the baseline; the source of truth), `hints.md`,
  `notes/` (the agent's own notes, `iter-N.md` by convention),
  `report.md` (the summary written when a segment ends, unless
  `--no-report`; not after an interrupt) and `report.html` (the run page).
- `iterations/NNN/` (three digits) per iteration: `record.json` (its history
  record, pretty-printed), `prompt.md` and `agent.log` (what the agent was
  given and did; written live while it runs), `gates/<k>/` for each gate
  that ran (k: its 1-based position in `gates:`; `command.txt`,
  `stdout.txt`, `stderr.txt`, `ask.log` for an `ask:` gate), `observe/<name>/`
  (`stdout.txt`, `stderr.txt`, captures, `review.log`; a review's feedback is
  its `stdout.txt`), `stop/<k>/`, and `rejected.patch` for a rejection.

A record's fields: `accepted`, `reason` (why it was rejected), `summary` (the
agent's one-line summary), `metrics`, `commit` (accepted), `gates` (each
`text`, `kind`, `passed`, `return_code`, `duration_s`, a failed one's
`output` tail; metric gates `value` and `reference`), `observe` (each `name`,
`ok`, `error`, `metrics`), `stops`, `discarded` (out-of-scope paths dropped),
`agent_error` (the agent call failed or timed out: rejected unchecked),
`base`, `tree`, `rejected_ref`, `started_at`/`ended_at`, `calls`.
For numbers across iterations:
`jq -c '[.iteration, .accepted, .metrics, .reason]' .autohelix/runs/<id>/history.jsonl`.

## Explain an iteration

- **Accepted**: every gate passed (and every metric gate). Say what changed
  (`summary`, `git show <commit>`), the metrics against the previous accepted
  iteration and the baseline, and what the reviews said
  (`observe/<name>/stdout.txt`). Passing gates is not proof the change is
  good; say what the gates actually check.
- **Rejected**: name the gate from `reason` (`gate failed: <command>`, or
  `metric gate: speed … worse than …`) and quote the
  decisive lines of its output (`gates/<k>/stdout.txt`, or the record's
  `output` tail). An `agent_error` means the agent call itself failed (rate
  limit, timeout, crash), not the change. The change is in `rejected.patch`,
  or `git diff <base> refs/autohelix-rejected/<id>/<n>` (kept until the run
  is deleted); the next iteration was given it to retry or abandon.
- A failed observe entry does not reject; out-of-scope changes are discarded,
  not rejected (`discarded`). Say so when they explain a surprise.

## Explain a run's progress

Lead with the state (running and in which phase, or how its last segment
ended and why: `status.stop_reason.detail`), then accepted vs attempted,
each metric from baseline to latest accepted value, and the pattern of
rejections (the same gate failing again and again points at the evaluator or
the goal, not bad luck). Read `notes/` and `report.md` for the agent's own
account, and check it against the records. Landing: `landing.commits` the
target branch lacks (manual runs wait for `autohelix land ID`). Keep it
plain: what was tried, what stuck, what to do next (`autohelix resume ID -n
N`, `land`, a new run with `--import ID`).

## Costs

`progress.cost_usd` is the run's total (baseline and the summary included),
each iteration's `cost` its own; in a record, each entry of `calls` (`role`
`agent` or `command` for `ask`/`review` calls) has `usage.cost_usd` and
`cost_source` (`calculated` is a Codex estimate). `cost_incomplete: true`
means some call reported no cost: the total is a lower bound, not the cost.
Model calls the project's own commands make (other than `autohelix ask` /
`review`) aren't counted.

## What you may do

Read the records, logs and code, and run read-only git and `view` commands.
Don't edit `.autohelix/`. Send `autohelix hint --run ID '…'` or `autohelix stop
ID --why '…'` only when the user asks, and only what they asked for; the same
for `resume`, `land`, `archive`, `delete` and `report` (a model call).
To change the experiment, propose a new config and follow the setup path.
