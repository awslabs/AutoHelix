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
    """Build template variables for the agent prompt. `members` are the other runs of
    the run's `group`, as `runs.group_members` returns them."""
    from autohelix.iteration_time import format_duration

    variables: dict[str, Any] = {
        "goal": config.goal.strip(),
        "iteration": iteration,
        "worktree": str(worktree_dir),
    }

    # Recent history
    history_summary = history.format_summary()
    if history_summary == "No previous iterations.":
        variables["history_summary"] = ""
    else:
        variables["history_summary"] = history_summary

    # Time limit (only iteration_time is communicated to the agent). Render as a
    # compact forward-looking duration ("15m"), not the timeout *message*.
    if config.iteration_time_seconds is not None:
        variables["iteration_time"] = format_duration(config.iteration_time_seconds)
    else:
        variables["iteration_time"] = ""

    # Constraints
    variables["constraints"] = [c.command for c in config.constraints]
    variables["stop"] = [c.command for c in config.stop]

    # Observables — pass structured data for the template to iterate
    def _format_metric(name: str, direction: str) -> str:
        arrow = "↑" if direction == "higher" else "↓"
        return f"{name} ({arrow})"

    variables["observables"] = [
        {
            "command": obs.command,
            "metric_labels": [_format_metric(n, d) for n, d in obs.values.items()],
        }
        for obs in config.observables
    ]

    # Scope
    variables["editable"] = config.editable
    variables["frozen"] = config.frozen

    # Review
    variables["has_reviewer"] = config.reviewer is not None

    # Other runs of the group: their state is copied into .autohelix-iteration/runs/<id>/.
    variables["group"] = group or ""
    variables["group_runs"] = [describe_member(paths, data) for paths, data in members]

    # Hints from `autohelix hint` command
    hints_path = history.paths.hints
    variables["has_hints"] = hints_path.exists() and hints_path.stat().st_size > 0

    return variables


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
