# Examples

These examples show some of the ways an AutoHelix loop can be structured. They
are starting points, not an exhaustive list of supported use cases: the goal,
agent prompt, editable scope, evaluation, feedback, and budget can all be
customized.

Each example highlights one possible combination of those pieces, from local
code optimization to research, GPU post-training, and nested agent runs. Adapt
or combine them to build a loop for your own project. See
[Designing Your Own Loop](../docs/concepts.md#designing-your-own-loop) for
guidance.

## Choose an Example

| Example | What is improved | What the agent can change |
|---------|------------------|---------------------------|
| [**sorting**](sorting/) | Sorting speed | Sorting implementation |
| [**AlgoTune**](algotune/setup.py) | Solver speed | Solver implementation |
| [**ml-recipe**](ml-recipe/) | Training recipe | Preprocessing, model choice, and hyperparameters |
| [**posttrain**](posttrain/) | Trained model | Training code and generated model weights |
| [**writing**](writing/) | Clarity and quality | Written article |
| [**task-queue**](task-queue/) | Implementation tasks | Task state and deliverables |
| [**research**](research/) | Research evidence | Experiment, results, and report |
| [**workflow-optimization**](workflow-optimization/) | AI workflow | Workflow code, prompt, and skill |
| [**nested-autohelix**](nested-autohelix/) | Inner AutoHelix loop | AutoHelix setup and instructions |

The checks and evaluators remain outside the agent's editable surface. See each
example's configuration or README for its exact constraints, metrics, and
setup.

## Set Up an Example

Run setup from the AutoHelix checkout, but place the runnable project outside
it. The examples intentionally ship their baseline state and should not be
optimized in the source tree.

AutoHelix must already be installed. To install it from the checkout with
dependencies used by all bundled examples:

```bash
source .venv/bin/activate
python -m pip install -e ".[dev,examples]"
```

`setup_example.py` lists the available examples and copies a bundled example
into a clean standalone git repository:

```bash
python examples/setup_example.py --list
python examples/setup_example.py sorting --dir /tmp/autohelix-sorting
cd /tmp/autohelix-sorting
autohelix run
```

AlgoTune and nested AutoHelix fetch tasks and use their own setup commands:

```bash
python examples/algotune/setup.py --list
python examples/algotune/setup.py eigenvalues_real \
  --dir /tmp/autohelix-algotune

python examples/nested-autohelix/setup.py \
  --dir /tmp/autohelix-nested
```

The [post-training example](posttrain/) requires a prepared GPU environment and
manual setup. Follow each example's README or generated instructions for any
additional requirements.
