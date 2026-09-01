# Research loop

This example uses AutoHelix to advance an open research question through
experiments, evidence, and a report. Each iteration should make one defensible
advance; a useful negative result is valid progress.

```bash
python examples/setup_example.py research --dir ./research-run
cd ./research-run
autohelix run -n 1
```

`PROJECT.md` defines the question and research standards. The agent may update
`experiment.py`, `results/`, and `REPORT.md`. The frozen `validate.py` checks
the result schema and reruns the quick experiment to verify reproducibility.
The reviewer critiques the evidence and may suggest possible follow-ups, but
does not choose the next action or decide whether an iteration merges.
This example intentionally has no numeric metric: the constraint enforces
reproducibility, while reviewer feedback guides later iterations.

Start with one iteration so you can inspect the experiment, report, and review
before extending the run.

## Adapting the experiment environment

AutoHelix does not provision research infrastructure. Prepare dependencies,
data, credentials, and compute before starting the loop, and verify the
experiment command from the same environment used to launch `autohelix`.
Local commands inherit that environment, so an activated virtual environment
or configured SDK is available to the agent.

For a more involved project, expose setup and execution through a stable,
noninteractive script such as `run_experiment.sh`. The script can activate an
environment, invoke a container, or submit and wait for a remote job. Keep
infrastructure code outside the editable scope when the agent should use it
rather than change it.

For remote CPU or GPU experiments:

- Configure noninteractive authentication without storing secrets in the
  repository.
- Give each run a unique remote directory so concurrent or rejected candidates
  cannot overwrite one another.
- Copy a machine-readable result back into the iteration worktree for
  validation and review.
- Record seeds and relevant data, dependency, and hardware versions.
- Keep a quick experiment that uses the same result schema as the full run.

Remote files and jobs are external state. AutoHelix can discard a rejected
worktree, but it cannot undo remote side effects. Write candidates to isolated
locations and promote or clean them up explicitly.
