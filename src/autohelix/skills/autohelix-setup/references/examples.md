# Example experiment designs

Adapt these patterns to existing files and commands. They illustrate design
choices; they do not assume that a package install includes the source
repository's example projects. The commands below assume the named evaluators
already exist; inspect and replace them with the target project's commands.
When one is missing, propose its design as described in
[loop design](loop-design.md) and obtain any needed implementation agreement
before presenting it as ready. Iteration counts, gates, and reviewers are
illustrations to discuss, not defaults to impose.

## Ordinary requests

An agent that has discovered this skill can start from a natural request such
as "Use AutoHelix for improving this API guide." The user need not specify a
skill name or draft YAML first. More explicit examples make the launch intent
clear:

- "Use AutoHelix to make the solver faster. Prepare a config template for review;
  leave optional budgets unset."
- "Use AutoHelix to improve this API guide. Prepare the loop and start a pilot
  using my configured backend and budget."
- "Use AutoHelix to make the solver faster. Prepare, run, and supervise three
  iterations, with four minutes per agent call and fifteen minutes for the
  loop. Use my configured agent and model, keep existing evaluations fixed,
  and explain the actual results when the execution ends."

The first request finishes at preparation; the second includes launch within
the stated authorization and the entrypoint's execution rules. A setup-only
handoff need not choose every budget: use the
[commented template](config.md#budgets-and-run-state) and explain the five-attempt
default when iterations is omitted. The selected budgets in the patterns below
illustrate possible run designs rather than required preparation defaults.
Present the config as the proposed agreement on behavior and evaluation,
including its limits, rather than treating a general goal as permission for
unlimited work.
The third request includes ongoing management; read
[managed runs](managed-runs.md) for monitoring, hints, and completion handoff.

The installer's `--agent` selects which coding agent discovers the skill.
The config's `agent.type` selects the backend that performs loop iterations;
these choices can differ.

## Performance optimization

The user wants a faster implementation with unchanged outputs. Keep the tests
and benchmark outside the editable scope, track runtime, and optionally gate
regressions when the measurement is sufficiently stable.

```yaml
goal: |
  Reduce solver runtime on the benchmark cases while preserving behavior.
scope:
  editable: [src/solver.py]
constraints:
  - python -m pytest tests/test_solver.py
metrics:
  - command: python benchmark.py
    values:
      runtime_ms: lower
    capture: [results/benchmark.json]
budget:
  iterations: 3
```

The benchmark must emit `##autohelix[runtime_ms=12.3]` (with the measured value).
Capture the detailed results so a speedup can be inspected. If timings are
noisy, choose repeated measurement and a justified tolerance before gating.
This pattern starts with observation; add a `runtime_ms` gate only if useful
for the agreed experiment. A focused existing test may be cheap enough to run
during setup, while the full suite or benchmark may need execution authorization.
Without benchmark execution, report runtime and emitted/captured results as
unverified and provide the benchmark command for later.

## Document improvement

The user wants a clearer API guide. Keep the API specification protected, use
available link/example checks, and request feedback for the intended audience.

```yaml
goal: |
  Make docs/guide.md usable by a first-time API user. Preserve factual accuracy
  against api-spec.json and include a complete introductory example.
scope:
  editable: [docs/guide.md]
constraints:
  - python validate_guide.py
reviewer:
  prompt: |
    Read docs/guide.md and api-spec.json. Assess clarity for a first-time user,
    accuracy against the specification, and whether the example is complete.
    Support feedback with specific passages and propose useful revisions.
    Do not modify files.
budget:
  iterations: 3
```

Do not invent `validate_guide.py` if the project has no such evaluator.
A small proposal could check local link targets and required example sections,
explain its limits on semantic accuracy, and estimate its filesystem-only cost.
Agree on implementation if needed. The reviewer is an optional source of later
feedback and is not needed to finish setup; explain the limits if the run relies
on review alone. No numeric score is required.

## Autonomous research

The user wants to investigate a question rather than obtain a particular
answer. This pattern keeps the question and evidence standards fixed and
permits experiment, results, and report changes. For exploratory or protocol
design work, agree on a different scope that allows the relevant parts to evolve.

```yaml
goal: |
  Advance the research question in PROJECT.md with reproducible evidence.
  Keep experiments, results, and REPORT.md consistent. Retain negative
  findings and describe uncertainty and limitations.
scope:
  editable: [experiment.py, results/, REPORT.md]
constraints:
  - python validate.py
observables:
  - command: python summarize_results.py
    capture: [results/latest.json, REPORT.md]
reviewer:
  prompt: |
    Review PROJECT.md, experiment.py, results/latest.json, and REPORT.md.
    Assess experimental validity, whether claims follow from evidence, and
    the treatment of uncertainty and negative findings. Give specific
    feedback. Inspect supplied artifacts without running experiments or
    modifying files.
budget:
  iterations: 4
  iteration_time: 8m
```

The validator and summary command must exist and fit the project's protocol.
The observation needs no scalar `values`; captured output and files can guide
the next iteration and support later inspection.

For a missing validator, propose checks for result schema, recorded seeds and
parameters, and consistency between results and report references; explain that
these cannot establish scientific validity. Estimate cost by distinguishing
reading existing results from rerunning experiments. Static inspection can
finish setup while experiment execution, generated evidence, and reviewer
feedback remain unverified.

If experiments call models or use remote resources, set limits in those
commands too. AutoHelix does not automatically count that external expenditure.
