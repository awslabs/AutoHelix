"""docs/phases.md shows the default prompts verbatim: the main agent's, rendered for a
sample iteration after a rejection, and the `ask` / `review` system prompts. These
tests render them and compare, so the page can't drift from the code."""

import re
from pathlib import Path

import yaml

from autohelix import review
from autohelix.config import load_config
from autohelix.history import History, IterationResult
from autohelix.judge import Target, build_system_prompt, describe_change
from autohelix.prompt_template import build_prompt_variables, load_template, render_template

PHASES = Path(__file__).resolve().parents[1] / "docs" / "phases.md"

# How the page writes the paths that differ per machine and run.
COPIES = "~/.cache/autohelix/copies/sort-1a2b3c4d/20261118-1205-sort-x7k2"
JUDGE_COPY = "<copy>"

CONFIG = {
    "goal": "Make sort.py's sort faster without changing its results.",
    "gates": ["pytest -q", {"metric": "speed", "regress": "5%"}],
    "observe": [{"name": "bench", "command": "python bench.py", "metrics": {"speed": "higher"}},
                {"name": "clarity", "review": "Review the change for clarity and correctness."}],
    "stop": ["jq -e '.speed >= 50' \"$AUTOHELIX_METRICS\""],
    "scope": {"editable": ["sort.py"]},
    "agent": {"timeout": "30m"},
}
OBSERVED = [{"name": "bench", "ok": True, "error": None, "metrics": {"speed": 10.0}},
            {"name": "clarity", "ok": True, "error": None, "metrics": {}}]
RECORDS = [
    {"iteration": 0, "accepted": True, "metrics": {"speed": 10.0}, "reason": "baseline",
     "gates": [{"text": "pytest -q", "kind": "command", "passed": True},
               {"text": "speed regress 5%", "kind": "metric", "passed": None}],
     "observe": OBSERVED},
    {"iteration": 1, "accepted": True, "metrics": {"speed": 14.2}, "summary": "Insertion sort for short runs",
     "gates": [{"text": "pytest -q", "kind": "command", "passed": True},
               {"text": "speed regress 5%", "kind": "metric", "passed": True}],
     "observe": [{**OBSERVED[0], "metrics": {"speed": 14.2}}, OBSERVED[1]]},
    {"iteration": 2, "accepted": False, "reason": "gate failed: pytest -q",
     "failure_output": "FAILED test_sort.py::test_negative_numbers - assert [-1, -3] == [-3, -1]",
     "summary": "Radix sort", "discarded": ["test_sort.py"], "tree": "abc",
     "rejected_ref": "refs/autohelix-rejected/20261118-1205-sort-x7k2/2",
     "gates": [{"text": "pytest -q", "kind": "command", "passed": False},
               {"text": "speed regress 5%", "kind": "metric", "passed": None}]},
]


def main_agent_prompt(tmp_path: Path) -> str:
    """The built-in template for iteration 3, after iteration 2 was rejected (with a
    hint sent), its copy seeded as the harness seeds it."""
    project = tmp_path / "project"
    (project / ".autohelix").mkdir(parents=True)
    history = History(project)
    for record in RECORDS:
        history.append(IterationResult(**record))
    history.paths.hints.write_text("- [2026-11-18 13:02] Negative numbers break radix sort\n")
    copies = tmp_path / "copies"
    copy = copies / "iter-3-agent"
    state = copy / ".autohelix-iteration"
    (state / "notes").mkdir(parents=True)
    (state / "rejected.patch").write_text("diff --git a/sort.py b/sort.py\n")
    for n in (0, 1):
        for name in ("bench", "clarity"):
            (state / "iterations" / f"{n:03d}" / "observe" / name).mkdir(parents=True)
    (state / "iterations" / "002" / "gates" / "1").mkdir(parents=True)
    (project / "autohelix.yaml").write_text(yaml.safe_dump(CONFIG, sort_keys=False))
    config = load_config(project)  # as a run loads it: its config file is frozen
    variables = build_prompt_variables(config, history, 3, copy)
    return render_template(load_template(config), variables).replace(str(copies), COPIES)


def _target(tmp_path: Path, previous: bool = False) -> Target:
    copy = tmp_path / "copy"
    state = copy / ".autohelix-iteration"
    state.mkdir(parents=True, exist_ok=True)
    (state / "candidate.diff").write_text("diff --git a/sort.py b/sort.py\n")
    shown = None
    if previous:
        shown = state / "previous.md"
        shown.write_text("Done: 3 of 5.\n")
    return Target(cwd=copy, guarded=copy, diff=state / "candidate.diff", previous=shown)


def ask_prompt(tmp_path: Path) -> str:
    """`ask:` in a run, with its copy's `candidate.diff`."""
    target = _target(tmp_path)
    text = build_system_prompt("Do the tests cover negative numbers?", change=describe_change(target, judging="judge"))
    return text.replace(str(target.cwd), JUDGE_COPY)


def review_prompt(tmp_path: Path) -> str:
    """A `review:` entry with `previous: true` and a metric, in a run."""
    target = _target(tmp_path, previous=True)
    text = review.build_system_prompt("Track SPEC.md: what is done, what is left?", target, metrics=["remaining"])
    return text.replace(str(target.cwd), JUDGE_COPY)


def documented(name: str) -> str:
    """The fenced block after `<!-- prompt: name -->` in docs/phases.md."""
    text = PHASES.read_text()
    found = re.search(rf"<!-- prompt: {name} -->\n+(`{{3,}})text\n(.*?)\n\1\n", text, re.S)
    assert found, f"no `<!-- prompt: {name} -->` block in {PHASES}"
    return found.group(2)


def test_the_main_agents_prompt_is_documented_verbatim(tmp_path):
    assert documented("agent") == main_agent_prompt(tmp_path).rstrip("\n")


def test_the_ask_prompt_is_documented_verbatim(tmp_path):
    assert documented("ask") == ask_prompt(tmp_path)


def test_the_review_prompt_is_documented_verbatim(tmp_path):
    assert documented("review") == review_prompt(tmp_path)


def test_the_samples_show_every_part(tmp_path):
    """The samples are worth reading: a rejection, its patch, the hints and the time limit;
    the review's previous output and number."""
    agent = main_agent_prompt(tmp_path)
    for part in ("Iteration 2 was rejected", "rejected.patch", "Negative numbers break radix sort",
                 "Your changes to test_sort.py were discarded", "30m", "Frozen (", "autohelix.yaml"):
        assert part in agent, part
    assert "Your own review from the last accepted iteration" in review_prompt(tmp_path / "r")
    assert "Also report these numbers: remaining." in review_prompt(tmp_path / "s")
