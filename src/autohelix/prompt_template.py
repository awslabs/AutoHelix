# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Prompt template loading and rendering using Jinja2."""

from __future__ import annotations

from importlib.resources import files as pkg_files
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import BaseLoader, Environment, TemplateSyntaxError

if TYPE_CHECKING:
    from autohelix.config import Config
    from autohelix.history import History

_TEMPLATES_PKG = "autohelix.templates"

DEFAULT_TEMPLATE = (
    pkg_files(_TEMPLATES_PKG).joinpath("default_prompt.md").read_text()
)


def _create_env() -> Environment:
    """Create a Jinja2 environment configured for prompt templates."""
    return Environment(
        loader=BaseLoader(),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def load_template(config) -> str:
    """The agent prompt template: the config's `prompt_template:` file, else the built-in
    one (so new features reach every project). `.autohelix/prompt.md` is no longer read."""
    path = getattr(config, "prompt_template", None)
    if path is None:
        return DEFAULT_TEMPLATE
    try:
        return Path(path).read_text()
    except OSError as e:
        raise RuntimeError(f"Cannot read prompt_template {path}: {e}") from e


def describe_member(paths, data: dict) -> str:
    """One line about another run of the group: where its state is in the copy, how far
    it got, its latest accepted metric values (what landing it would deliver, as the
    metric gates compare), whether it is running, and its code's ref."""
    from autohelix.history import History
    from autohelix.run_status import read_run_status

    where = f"`.autohelix-iteration/runs/{paths.run_id}/`"
    try:  # another run's state: unreadable or odd values must not stop this run
        records = History(paths.project, paths.run_id).load()
        attempts = max((r.iteration for r in records), default=0)
        parts = [f"{attempts} iteration{'s' * (attempts != 1)}"]
        latest = next((r for r in reversed(records) if r.accepted and r.metrics), None)
        if latest:
            parts.append("latest accepted " + ", ".join(
                f"{name} {value:g}" if isinstance(value, (int, float)) else f"{name} {value}"
                for name, value in latest.metrics.items()))
        # From its status file, not a lock probe: probing would briefly block the user's
        # own commands on that run, and this runs every iteration.
        running = (read_run_status(paths.project, paths.run_id) or {}).get("state") == "running"
        parts.append("running" if running else "finished")
    except Exception as e:  # noqa: BLE001
        return f"{where} (unreadable: {e}; code at `autohelix/{paths.run_id}`)"
    return f"{where} ({', '.join(parts)}; code at `autohelix/{paths.run_id}`)"


def uses_old_layout(template: str) -> bool:
    """Whether a prompt template names the copy's material at `.autohelix/` (before 0.2)."""
    import re

    return bool(re.search(r"\.autohelix/(notes|review\.md|commit_summary|observations|hints|peer_notes|history)",
                          template))


def build_prompt_variables(
    config: Config,
    history: History,
    iteration: int,
    worktree_dir: Path,
    *,
    group: str | None = None,
    members=(),
) -> dict[str, Any]:
    """The variables a prompt template gets: a stable, documented contract.

    - `goal`: the config's goal, whole; `title`: its first line (`Config.title`).
    - `iteration`: this iteration's number (the baseline is 0); `worktree`: the agent's
      working directory (its copy), as a string.
    - `history`: the last iterations, one line each (`History.format_summary`); "" if none.
    - `gates`: every gate in list order, `{"text", "kind": "command"|"metric"}`; a metric
      gate's text says what it compares ("speed: at most 5% worse than ...").
    - `observe`: every observe entry, `{"name", "kind": "command"|"review", "command"
      (command entries) or "prompt" (review entries, on one line), "metric_labels"}`.
    - `stop`: the stop commands; `editable`, `frozen`: the scope patterns.
    - `notes_path`: where this iteration's notes go (in the copy).
    - `hints`: the copy's hints file (`.autohelix-iteration/hints.md`) if there are
      hints, else "".
    - `rejected_patch`: `.autohelix-iteration/rejected.patch` if the copy has one (the
      latest iteration was rejected and its tree was kept), else "".
    - `discarded`: the out-of-scope paths the latest iteration's changes to were dropped.
    - `last`: the latest iteration (the baseline before iteration 1), or None:
      `{"iteration", "accepted", "reason", "summary", "failure_output", "gates": [{"text",
      "kind", "status": "passed"|"failed"|"pending"}], "observe": [{"name", "ok",
      "error", "path"}]}`; `path` is its folder in the copy
      (`.autohelix-iteration/observations/iter-K/<name>/`).
    - `time_limit`: `agent.timeout` as a compact duration ("15m"), or "".
    - `group`: the run's group, or ""; `group_runs`: one line per other run of the
      group (`describe_member`; `members` as `runs.group_members` returns them).
    """
    from autohelix.config import MetricGate
    from autohelix.iteration_time import format_duration
    from autohelix.workspace import ITERATION_DIR

    state = Path(worktree_dir) / ITERATION_DIR
    summary = history.format_summary()
    records = [r for r in history.load() if r.iteration < iteration]
    latest = max(records, key=lambda r: r.iteration, default=None)

    variables: dict[str, Any] = {
        "goal": config.goal.strip(),
        "title": config.title,
        "iteration": iteration,
        "worktree": str(worktree_dir),
        "history": "" if summary == "No previous iterations." else summary,
        "gates": [{"text": _describe_gate(gate), "kind": "metric" if isinstance(gate, MetricGate) else "command"}
                  for gate in config.gates],
        "observe": [_describe_entry(entry) for entry in config.observe],
        "stop": [c.command for c in config.stop],
        "editable": config.editable,
        "frozen": config.frozen,
        "notes_path": str(state / "notes" / f"iter-{iteration}.md"),
        "hints": "",
        "rejected_patch": f"{ITERATION_DIR}/rejected.patch" if (state / "rejected.patch").is_file() else "",
        "discarded": list(latest.discarded) if latest else [],
        "last": _describe_last(latest) if latest else None,
        "time_limit": format_duration(config.agent.timeout) if config.agent.timeout is not None else "",
        "group": group or "",
        "group_runs": [describe_member(paths, data) for paths, data in members],
    }
    hints = history.paths.hints
    if hints.exists() and hints.stat().st_size > 0:
        variables["hints"] = f"{ITERATION_DIR}/hints.md"
    return variables


def _describe_entry(entry) -> dict[str, Any]:
    """One observe entry as the prompt lists it."""
    labels = [f"{name} ({'↑' if direction == 'higher' else '↓'})" for name, direction in entry.metrics.items()]
    if entry.review is not None:
        return {"name": entry.name, "kind": "review", "prompt": " ".join(entry.review.split()),
                "metric_labels": labels}
    return {"name": entry.name, "kind": "command", "command": entry.command, "metric_labels": labels}


def _describe_last(record) -> dict[str, Any]:
    """The latest iteration's outcome: what the next agent is told about it."""
    from autohelix.workspace import ITERATION_DIR

    status = {True: "passed", False: "failed", None: "pending"}
    return {
        "iteration": record.iteration,
        "accepted": record.accepted,
        "reason": record.reason or "",
        "summary": record.summary or "",
        "failure_output": record.failure_output or "",
        "gates": [{"text": gate.get("text", ""), "kind": gate.get("kind", "command"),
                   "status": status.get(gate.get("passed"), "pending")} for gate in record.gates],
        "observe": [{"name": item.get("name", ""), "ok": bool(item.get("ok")), "error": item.get("error") or "",
                     "path": f"{ITERATION_DIR}/observations/iter-{record.iteration}/{item.get('name', '')}/"}
                    for item in record.observe],
    }


def _describe_gate(gate) -> str:
    """One gate as the prompt lists it."""
    from autohelix.config import MetricGate

    if not isinstance(gate, MetricGate):
        return gate.text
    if gate.mode == "regress":
        bound = "no worse" if gate.threshold_pct == 0 else f"at most {gate.threshold_pct:g}% worse"
        return f"{gate.metric}: {bound} than the last accepted iteration"
    bound = "better" if gate.threshold_pct == 0 else f"at least {gate.threshold_pct:g}% better"
    return f"{gate.metric}: {bound} than the last accepted iteration"


def render_template(template: str, variables: dict[str, Any]) -> str:
    """Render a Jinja2 template with the given variables.

    Falls back to returning the raw template with a warning prefix if the
    template has syntax errors.
    """
    env = _create_env()
    try:
        tmpl = env.from_string(template)
    except TemplateSyntaxError as e:
        return f"[Template error: {e.message} at line {e.lineno}]\n\n{template}"

    rendered = tmpl.render(**variables)

    # Collapse 3+ consecutive newlines into 2
    import re
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip() + "\n"
