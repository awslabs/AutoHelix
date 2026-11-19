# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Prompt template loading and rendering using Jinja2."""

from __future__ import annotations

from importlib.resources import files as pkg_files
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import BaseLoader, Environment, TemplateSyntaxError

from autohelix.formatting import SUMMARY_LIMIT, clean, clean_block

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
    """The agent prompt template: the config's `prompt_template:` (a run's stored text,
    else the file), else the built-in one (so new features reach every project).
    `.autohelix/prompt.md` is no longer read."""
    text = getattr(config, "prompt_template_text", None)
    if text is not None:
        return text
    path = getattr(config, "prompt_template", None)
    if path is None:
        return DEFAULT_TEMPLATE
    try:
        return Path(path).read_text()
    except OSError as e:
        raise RuntimeError(f"Cannot read prompt_template {path}: {e}") from e


def describe_member(paths, data: dict) -> str:
    """One line about another run of the group (or the imported run): where its state is in the copy, how far
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
        # Its status, checked against its lock only when it says running (`holder_gone`).
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
    copy_dir: Path,
    *,
    group: str | None = None,
    members=(),
    rejected_diff: str = "",
    imported=None,
    head: str | None = None,
) -> dict[str, Any]:
    """The variables a prompt template gets (`VARIABLES`): a stable contract, documented
    in docs/concepts.md's variable table (a test keeps the two in step). Agent-written
    text in them is cleaned of control characters."""
    from autohelix.config import MetricGate
    from autohelix.iteration_time import format_limit
    from autohelix.iteration import SEEDED_ITERATIONS
    from autohelix.workspace import ITERATION_DIR

    state = Path(copy_dir) / ITERATION_DIR
    latest = history.latest(before=iteration)
    # The latest rejection's failure output is shown in full (`last`), not in the history too.
    summary = history.format_summary(without_output=latest.iteration if latest and not latest.accepted else None)

    variables: dict[str, Any] = {
        "goal": config.goal.strip(),
        "title": config.title,
        "iteration": iteration,
        "copy": str(copy_dir),
        "history": "" if summary == "No previous iterations." else summary,
        "gates": [{"text": _describe_gate(gate), "kind": "metric" if isinstance(gate, MetricGate) else "command"}
                  for gate in config.gates],
        "observe": [_describe_entry(entry) for entry in config.observe],
        "stop": [c.command for c in config.stop],
        "editable": config.editable,
        "frozen": config.frozen_names(),
        "notes_path": str(state / "notes" / f"iter-{iteration}.md"),
        "hints": "",
        "hints_text": "",
        "rejected_patch": f"{ITERATION_DIR}/rejected.patch" if (state / "rejected.patch").is_file() else "",
        "rejected_diff": rejected_diff,
        "discarded": [clean(path) for path in latest.discarded] if latest else [],
        "discarded_frozen": [clean(path) for path in latest.discarded_frozen] if latest else [],
        "last": _describe_last(latest, state) if latest else None,
        "last_accepted": _describe_accepted(history, iteration, state),
        "recent_iterations": SEEDED_ITERATIONS,
        "time_limit": format_limit(config.agent.timeout) if config.agent.timeout is not None else "",
        "group": group or "",
        "group_runs": [describe_member(paths, data) for paths, data in members
                       if _in_copy(state, "runs", paths.run_id)],
        "imported": _describe_imported(imported, state, head or _own_tip(history)),
        "experiment": _in_copy(state, "experiment"),
    }
    hints = history.paths.hints
    if hints.exists() and hints.stat().st_size > 0:
        variables["hints"] = f"{ITERATION_DIR}/hints.md"
        variables["hints_text"] = clean_block(hints.read_text(errors="replace")).strip()
    return variables


def _own_tip(history) -> str | None:
    """This run's tip (`runs.run_tip`), or None if it can't be read."""
    from autohelix.runs import run_tip

    try:
        return run_tip(history.paths)
    except Exception:  # noqa: BLE001 - only decides whether a hint is shown
        return None


def _same_tree(project: Path, a: str | None, b: str) -> bool:
    """Whether commits `a` and `b` hold the same tree (False when either can't be read)."""
    from autohelix.runs import git

    if not a or not b:
        return False
    try:
        trees = [git(project, "rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}", check=False)
                 for commit in (a, b)]
    except Exception:  # noqa: BLE001
        return False
    return all(t.returncode == 0 for t in trees) and trees[0].stdout.strip() == trees[1].stdout.strip()


def _describe_imported(imported, state: Path, head: str | None = None) -> dict[str, Any] | None:
    """The imported run (`(paths, data)`, or None) as the prompt names it, if its state
    made it into the copy (`state` is its `.autohelix-iteration/`); `head` is the commit
    the copy's code is."""
    from autohelix.runs import run_tip, short_id

    if imported is None:
        return None
    paths, data = imported
    path = _in_copy(state, "runs", paths.run_id)
    if not path:
        return None
    try:
        tip = run_tip(paths, data)
    except Exception:  # noqa: BLE001 - another run's state must not stop this run
        tip = None
    return {"id": paths.run_id, "short": short_id(paths.run_id), "path": path,
            "description": describe_member(paths, data),
            "same_code": _same_tree(paths.project, head, tip)}


def _describe_entry(entry) -> dict[str, Any]:
    """One observe entry as the prompt lists it."""
    labels = [f"{name} ({'↑' if direction == 'higher' else '↓'})" for name, direction in entry.metrics.items()]
    if entry.review is not None:
        return {"name": entry.name, "kind": "review", "prompt": " ".join(entry.review.split()),
                "metric_labels": labels}
    return {"name": entry.name, "kind": "command", "command": entry.command, "metric_labels": labels}


def _in_copy(state: Path, *parts: str) -> str:
    """`.autohelix-iteration/<parts>/` if that folder is in the copy (`state` is its
    `.autohelix-iteration/`), else ""."""
    from autohelix.workspace import ITERATION_DIR

    folder = state.joinpath(*parts)
    return clean(f"{ITERATION_DIR}/{'/'.join(parts)}/") if folder.is_dir() and not folder.is_symlink() else ""


def _describe_last(record, state: Path) -> dict[str, Any]:
    """The latest iteration's outcome: what the next agent is told about it."""
    from autohelix.runs import RunPaths

    status = {True: "passed", False: "failed", None: "pending"}
    folder = ("iterations", RunPaths.iteration_name(record.iteration))
    gates = []
    for k, gate in enumerate(record.gates, start=1):
        failed = gate.get("passed") is False
        gates.append({"text": clean(gate.get("text")), "kind": gate.get("kind", "command"),
                      "status": status.get(gate.get("passed"), "pending"),
                      "path": _in_copy(state, *folder, "gates", str(k)) if failed else ""})
    observe = []
    for item in record.observe:
        name = item.get("name")
        valid = isinstance(name, str) and name and not name.startswith(".") and "/" not in name
        observe.append({"name": clean(name), "ok": bool(item.get("ok")), "error": clean(item.get("error")),
                        "missing": [clean(metric) for metric in item.get("metric_errors") or {}],
                        "path": _in_copy(state, *folder, "observe", name) if valid else ""})
    return {
        "iteration": record.iteration,
        "accepted": record.accepted,
        "reason": clean(record.reason),
        "summary": clean(record.summary, SUMMARY_LIMIT),
        "failure_output": clean_block(record.failure_output).strip(),
        "gates": gates,
        "observe": observe,
    }


def _describe_accepted(history, iteration: int, state: Path) -> dict[str, Any] | None:
    """The latest accepted iteration below `iteration`: its number and folder in the copy."""
    from autohelix.iteration import last_accepted
    from autohelix.runs import RunPaths

    n = last_accepted([record for record in history.load() if record.iteration < iteration])
    if n is None:
        return None
    return {"iteration": n, "path": _in_copy(state, "iterations", RunPaths.iteration_name(n))}


# The documented variables (`build_prompt_variables`); a custom template naming others
# gets a warning at run start (`unknown_variables`).
VARIABLES = frozenset({
    "goal", "title", "iteration", "copy", "history", "gates", "observe", "stop", "editable", "frozen",
    "notes_path", "hints", "hints_text", "rejected_patch", "rejected_diff", "discarded", "discarded_frozen", "last", "last_accepted",
    "recent_iterations", "time_limit", "group", "group_runs", "imported", "experiment",
})


# Variables that were renamed: a template naming the old one is a config error.
RENAMED_VARIABLES = {"worktree": "copy"}


def template_problem(config) -> str | None:
    """Why the config's `prompt_template:` can't be used (a config error naming the file
    and line: it doesn't parse, or can't be read), else None. Config validation calls it,
    so a broken template fails `run`, not the agent."""
    name = config.prompt_template.name if config.prompt_template else "prompt template"
    try:
        parsed = _create_env().parse(load_template(config))
    except TemplateSyntaxError as e:
        return f"prompt_template {name}, line {e.lineno}: {e.message}"
    except (RuntimeError, UnicodeDecodeError) as e:
        return f"prompt_template {name}: {e}"
    from jinja2 import meta

    renamed = sorted(old for old in RENAMED_VARIABLES if old in meta.find_undeclared_variables(parsed))
    if renamed:
        return f"prompt_template {name}: " + "; ".join(
            f"`{old}` is now `{RENAMED_VARIABLES[old]}` ({{{{ {RENAMED_VARIABLES[old]} }}}})" for old in renamed)
    return None


def unknown_variables(template: str) -> list[str]:
    """Names a template uses that are not prompt variables (nor Jinja globals), sorted:
    e.g. `constraints` or `history_summary` from before config v2. The template parses
    (config validation, `template_problem`)."""
    from jinja2 import meta

    env = _create_env()
    names = meta.find_undeclared_variables(env.parse(template))
    return sorted(names - VARIABLES - set(env.globals))


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


def render_template(template: str, variables: dict[str, Any], name: str | None = None) -> str:
    """Render a Jinja2 template with the given variables. A template that doesn't parse
    or render raises RuntimeError naming it (`name`: the config's template file; None:
    the built-in prompt)."""
    from jinja2 import TemplateError

    name = f"prompt_template {name}" if name else "The built-in prompt"
    try:
        rendered = _create_env().from_string(template).render(**variables)
    except TemplateSyntaxError as e:
        raise RuntimeError(f"{name}, line {e.lineno}: {e.message}") from e
    except TemplateError as e:
        raise RuntimeError(f"{name} could not be rendered: {e}") from e

    # Collapse 3+ consecutive newlines into 2
    import re
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip() + "\n"
