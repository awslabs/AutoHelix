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


def load_template(project_path: Path) -> str:
    """Load prompt template from .autohelix/prompt.md or return default."""
    template_path = project_path / ".autohelix" / "prompt.md"
    if template_path.exists():
        return template_path.read_text()
    return DEFAULT_TEMPLATE


def build_prompt_variables(
    config: Config,
    history: History,
    iteration: int,
    worktree_dir: Path,
) -> dict[str, Any]:
    """Build template variables for the agent prompt."""
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

    # Peer notes from parallel workers. The parallel wrapper symlinks peer
    # dirs into .autohelix/peer_notes/<worker>/; prepare_worktree carries
    # them into each iteration worktree.
    peers_dir = history.project_path / ".autohelix" / "peer_notes"
    peers: list[str] = []
    if peers_dir.exists():
        peers = sorted(p.name for p in peers_dir.iterdir() if p.is_dir())
    variables["peers"] = peers

    # Hints from `autohelix hint` command
    hints_path = history.project_path / ".autohelix" / "hints.md"
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
