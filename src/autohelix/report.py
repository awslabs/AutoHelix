# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run summary: one agent call that writes .autohelix/output/report.md.

The harness calls `write_report` when a run finishes; `autohelix report` calls it on demand.
The agent writes only the narrative; the viewer shows the facts around it.
"""

import json
import subprocess
import time
from pathlib import Path

from autohelix.config import Config
from autohelix.history import History

INSTRUCTIONS = """\
Write a short summary of this run for someone who didn't watch it. Cover what was achieved, \
which changes mattered and why, what was tried and didn't work, and what to try next. If \
anything seems worth checking before relying on the results, mention it. Refer to iterations \
as "iter N". Use whatever structure suits this run.

Keep it proportional to the run: a few paragraphs for a short run. The page around the summary \
already shows the iteration table, metric chart and run cost; don't reproduce them. No title; \
start with the result."""

TIMEOUT_SECONDS = 15 * 60  # cap: agent.timeout_seconds can be hours for ML runs

WRITE_TO = "Write the summary in Markdown to "


def output_dir(project: Path) -> Path:
    return project / ".autohelix" / "output"


def read_report_info(project: Path) -> dict | None:
    """What the saved report covers and what writing it took:
    {"last_iteration": N, "written_at": unix time, "seconds": s, "cost_usd": usd or None}."""
    try:
        return json.loads((output_dir(project) / "report.json").read_text())
    except (OSError, ValueError):
        return None


def build_report_prompt(config: Config, project: Path, config_path: Path | None, draft: Path) -> str:
    records = History(project).load()
    last = max((r.iteration for r in records), default=0)
    first_commit = next((r.commit for r in records if r.iteration > 0 and r.accepted and r.commit), None)
    if first_commit:
        git = (f"- Git: each accepted iteration is a commit (the `commit` field in history.jsonl). "
               f"The baseline is `{first_commit}^`; `git diff {first_commit}^ <commit>` shows what "
               "an iteration changed from it. Rejected iterations leave no commit.")
    else:
        git = "- Git: no iteration was accepted, so the code is unchanged from the baseline."
    return f"""\
Write a summary of this AutoHelix run.

## Goal
{config.goal.strip()}

The summary covers iterations 1–{last} (iteration 0 is the baseline measurement).

## Where to look
Paths are relative to {project}.
- .autohelix/history.jsonl: one record per iteration (accepted, metrics, rejection reason and \
failure output, commit, cost and time).
- .autohelix/notes/: notes the agent wrote during the run (iter-N.md, plus shared notes).
- .autohelix/reviews/iter-N.md: reviewer feedback, if a reviewer was configured.
- .autohelix/logs/iter-N/agent.log: full agent transcripts. They are large; search them rather \
than reading them whole.
{git}
- {config_path or project / "autohelix.yaml"}: the run's config.

## Instructions
{INSTRUCTIONS}

Only read. Don't run tests, benchmarks or experiments, and don't edit any file other than the summary.
{WRITE_TO}{draft}.
"""


def write_report(config: Config, project: Path, console, config_path: Path | None = None) -> bool:
    """Run the report agent; replace the saved report only if it wrote a new one."""
    from rich.live import Live

    from autohelix.agents import AgentConfig, AgentEvent, create_agent
    from autohelix.display import LiveDisplay

    out = output_dir(project)
    out.mkdir(parents=True, exist_ok=True)
    draft = out / "report.draft.md"
    draft.unlink(missing_ok=True)
    last = History(project).get_last_iteration()

    agent = create_agent(AgentConfig(
        type=config.agent.type,
        command=config.agent.command,
        model=config.agent.model,
        timeout_seconds=min(config.agent.timeout_seconds or TIMEOUT_SECONDS, TIMEOUT_SECONDS),
    ), heartbeat_seconds=30)
    log_path = project / ".autohelix" / "logs" / "report.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    display = LiveDisplay(console=console, title="writing summary", verbose=False)

    def on_event(event: AgentEvent) -> None:
        display.handle_event(event)

    tree_before = _changed_files(project)
    started = time.monotonic()
    with Live(display, console=console, refresh_per_second=10, transient=True):
        result = agent.run(
            worktree_path=project,
            prompt=build_report_prompt(config, project, config_path, draft),
            iteration=0,
            log_path=log_path,
            event_callback=on_event,
        )
        display.flush()
    seconds = round(time.monotonic() - started, 1)

    # The prompt says read-only; if files changed anyway, the next `run` would refuse to start.
    if touched := sorted(_changed_files(project) - tree_before):
        console.print("[yellow]The summary agent changed files it should only have read:[/yellow] "
                      + ", ".join(touched) + "\n[dim]Review them with git status / git diff.[/dim]")

    if not draft.is_file() or not draft.read_text().strip():
        draft.unlink(missing_ok=True)
        console.print("[yellow]The agent finished without writing a summary; "
                      "the previous report (if any) is unchanged.[/yellow]")
        return False
    draft.replace(out / "report.md")
    info = {"last_iteration": last, "written_at": time.time(), "seconds": seconds,
            "cost_usd": (result.usage or {}).get("cost_usd")}
    (out / "report.json").write_text(json.dumps(info))
    console.print(f"[green]Summary of iterations 1–{last} written to .autohelix/output/report.md[/green]")
    return True


def _changed_files(project: Path) -> set[str]:
    """Files git reports as modified or untracked (.autohelix/ is ignored)."""
    result = subprocess.run(["git", "status", "--porcelain"], cwd=project, capture_output=True, text=True)
    return {line[3:] for line in result.stdout.splitlines()} if result.returncode == 0 else set()
