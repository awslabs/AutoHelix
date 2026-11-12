# Designing an improvement experiment

Propose a loop around the user's actual goal and available evidence. Suggest
a small pilot when it can reveal evaluator or environment problems before a
long run. Use the user's budget rather than imposing a fixed iteration count.

## Separate requirements, measurements, and feedback

| Purpose | AutoHelix mechanism | Example |
| --- | --- | --- |
| Preserve required behavior | Gate command (or an `ask:` gate) | Existing correctness tests |
| Observe progress | `observe:` command entry with metrics and captured files | Runtime and benchmark report |
| Reject a measured regression | Optional `metric:` gate vs the last accepted iteration | Runtime at most 5% worse |
| Guide the next attempt | `observe:` review entry or captured feedback | Editorial suggestions with examples |
| Track a worklist | Review entry with `previous: true` and a count metric | Items still missing |
| End when done | `stop:` command | Count reaches zero, or an `ask` judgment |
| Limit effort | Iteration, time, and available cost budgets | A short exploratory pilot |

Make a requirement a gate when every accepted candidate must satisfy it.
Measuring a number alone does not make it a requirement. Avoid making every
noisy measurement a strict gate: that can reject useful changes because of
measurement variation, and an `improve:` gate on a noisy number keeps lucky
measurements, so the loop stalls. Consider repeated measurements or medians
in the benchmark, and a tolerance above the measured noise.

When several metrics conflict, ask what tradeoff matters or start with
observations and feedback. The current configuration can apply separate gates
but does not define a general multi-objective selection strategy.

## Make evaluation informative

Suggest missing evaluators when they would make the experiment useful. For
example, a solver benchmark proposal could time a fixed set of small, typical,
and larger inputs after warmup, report median runtime across repeated trials,
and pair it with existing output-equality tests. State that its conclusions
cover those cases, estimate CPU time from available evidence (or label it
unknown), and propose observing runtime before choosing any regression gate.
This makes the implementation agreement concrete without requiring a fixed
interview. Follow the entrypoint's authorization distinction before coding
the evaluator or executing it.

Use representative cases and protect evaluation criteria by default, while
allowing changes that fit the user's experiment. Inspect how the evaluator
would load the candidate inside its copy. Editable installs and absolute
imports can accidentally evaluate the original checkout.

AutoHelix remaps project source paths visible in its own Python environment
into the copy's `PYTHONPATH` for checks and agents. A separate evaluator
environment or a hardcoded import may still select the original code. When that
risk applies, propose an import-location probe from a disposable copy using
the intended check environment. An import can itself trigger expensive work;
inspect the code before treating a `__file__` probe as cheap execution.

Explain the limits of the evaluation. Passing a small test suite establishes
that those tests passed; it does not establish universal correctness. A
training score may improve without improved held-out performance. A review
can identify useful issues without establishing that all claims are correct.

Preserve useful evidence with `capture`: detailed benchmark results, evaluation
reports, figures, or experiment records. Decide which artifacts the next
iteration needs and which a person needs to inspect.

## Qualitative and research goals

A useful loop does not require a numeric score. For a document, keep factual
or formatting checks as gates and consider an editorial review entry. For
research, agree which parts of the question, protocol, and evidence standards
should stay fixed and which may evolve; let the agent revise experiments and
reports within that scope. An exploratory study need not use a fixed protocol.

Review prompts benefit from a specified audience and criteria, such as
clarity for a first-time API user or whether a conclusion follows from the
reported experiment. Request examples and actionable feedback. A number from
a review exists only when the entry declares it under `metrics:`; its meaning
comes from the prompt, and it is noisy and gameable, so use it for display
and `stop:` rather than gates.

Research runs should preserve negative findings and uncertainty. Autonomous
execution is compatible with an inconclusive result; the goal should not
require evidence for a predetermined conclusion.

## Choose a budget that fits the work

Retain the user's stated budgets and agent/model preferences. By default, set
only an iteration limit (`budget: {iterations: 3}`) and no cost or time limit,
and leave timeouts at their defaults; add any of these when the user asks.

For preparation/design, optional cost, time, and `agent.timeout` may be omitted
or set to YAML `null`: no cap is configured by that field. Leave proposed values
as comments or suggestions when not selected; choosing active budgets is not a
prerequisite for setup-only handoff. If iterations is omitted, including in an
empty/null budget, the runtime defaults to five attempts, not unlimited.
Do not generate `iterations: null`; it is incompatible with the current numeric
run limit. See the [commented budget template](config.md#budgets-and-run-state).

For launch/autonomous requests, use given/existing bounds and resolve material
budget ambiguity before execution. When choosing bounds is part of the agreed
autonomy, select finite effort limits appropriate to the workload and explain
them. A small pilot can be a useful proposal when unspecified, not a universal
active default. Do not repeat approval already supplied, impose an arbitrary
dollar/iteration amount, or require a dollar cap where reporting is unavailable.

A dollar budget is optional. Suggest it only when the chosen backend actually
records authoritative reported dollar usage for those calls. Inspect available
usage records and backend support rather than making a model call just to check
accounting. A backend's support for a dollar field alone does not establish that
the chosen configuration reports it reliably.

Explain that the dollar counter covers the optimization agent and the model
calls of `ask` gates and reviews, including the baseline's, and is checked
between iterations; it may overshoot.
Report generation, external calls, and supervising/setup-agent expenditure
are separate. If reporting is unsupported, missing,
or unreliable, say that a dollar budget cannot reliably bound spend. Do not
invent a token price or interpret missing cost as free usage. Use iteration/time
limits to bound attempts and duration, explain any unmet financial requirement,
and distinguish them from a strict all-inclusive financial limit. See
[budget fields and their actual boundaries](config.md#budgets-and-run-state).

## Check the lifecycle

Rejecting an iteration discards repository code, not shared output directories,
databases, model checkpoints, or remote state. For external artifacts, use
deliberate locations and an explicit promotion/recovery procedure. Capturing a
checkpoint reference does not implement that procedure.

Estimate command runtime and cost from scripts, dataset sizes, prior logs, and
known resource/model use; mark uncertainty rather than treating a timeout as an
estimate. Static setup can finish with a proposed later verification when those
commands are expensive or their cost is unclear.
AutoHelix's main-agent dollar budget cannot cap unreported model calls in an
evaluation command.

At handoff, explain what will count as acceptance, which observations will be
retained, and why the run will end: budgets, manual interruption, or a `stop:`
command (a metric target, a tracked count reaching zero, or an
`autohelix ask` judgment). Plateau detection is not a configuration field.
