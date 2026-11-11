# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run summary: one agent call that writes the selected run's output/report.md.

The harness calls `write_report` when a run finishes; `autohelix report` calls it on demand.
The agent writes only the narrative; the viewer shows the facts around it.
"""

import json
import time
from pathlib import Path

from autohelix.config import Config
from autohelix.history import History
from autohelix.runs import RunPaths, RunLock, run_tip, git

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


def output_dir(project: Path, run_id: str | None = None) -> Path:
    return RunPaths(project, run_id).output


def read_report_info(project: Path, run_id: str | None = None) -> dict | None:
    """What the saved report covers and what writing it took:
    {"last_iteration": N, "written_at": unix time, "seconds": s, "cost_usd": usd or None}."""
    if not RunPaths(project, run_id).run_id:
        return None
    try:
        return json.loads((output_dir(project, run_id) / "report.json").read_text())
    except (OSError, ValueError):
        return None


def build_report_prompt(config: Config, project: Path, config_path: Path | None, draft: Path, run_id: str | None = None) -> str:
    paths = RunPaths(project, run_id)
    if not paths.run_id:
        raise RuntimeError("No current run; start a run first.")
    records = History(project, paths.run_id).load()
    last = max((r.iteration for r in records), default=0)
    identity = paths.read_identity()
    git = (f"- Git: baseline `{identity['base_commit']}`; accepted code `autohelix/{paths.run_id}` "
           f"(current accepted tip `{run_tip(paths, identity)}`). Use git diff baseline accepted-tip.")
    if not any(row.iteration > 0 and row.accepted for row in records):
        git += " No iteration was accepted."
    from autohelix.run_status import read_run_status

    reason = (read_run_status(project, paths.run_id) or {}).get("stop_reason")
    ended = f"\nThe run ended because: {reason['detail']}.\n" if isinstance(reason, dict) and reason.get("detail") else ""
    return f"""\
Write a summary of this AutoHelix run.

## Goal
{config.goal.strip()}

The summary covers iterations 1–{last} (iteration 0 is the baseline measurement).
{ended}
## Where to look
State paths are relative to {paths.run_dir}; candidate code is in the current working directory.
- history.jsonl: one record per iteration (accepted, metrics, rejection reason and \
failure output, commit, cost and time).
- notes/: notes the agent wrote during the run (iter-N.md, plus shared notes).
- reviews/iter-N.md: reviewer feedback, if a reviewer was configured.
- logs/iter-N/call-*/agent.log: full agent transcripts. They are large; search them rather \
than reading them whole.
{git}
- {config_path or project / "autohelix.yaml"}: the run's config.

## Instructions
{INSTRUCTIONS}

Only read. Don't run tests, benchmarks or experiments, and don't edit any file other than the summary.
{WRITE_TO}{draft}.
"""


def write_report(config: Config, project: Path, console, config_path: Path | None = None, *, run_id: str | None = None, _owned: bool = False) -> bool:
    """Run the report agent; replace the saved report only if it wrote a new one."""
    from rich.live import Live

    from autohelix.agents import AgentConfig, AgentEvent, create_agent
    from autohelix.display import LiveDisplay

    paths = RunPaths(project, run_id)
    if not paths.run_id:
        raise RuntimeError("No current run; start a run first.")
    if paths.run_id and not _owned:
        with RunLock(paths.lock):
            return write_report(config, project, console, config_path,
                                run_id=paths.run_id, _owned=True)
    out = paths.output
    out.mkdir(parents=True, exist_ok=True)
    draft = out / "report.draft.md"
    draft.unlink(missing_ok=True)
    last = History(project, paths.run_id).get_last_iteration()

    agent = create_agent(AgentConfig(
        type=config.agent.type,
        command=config.agent.command,
        model=config.agent.model,
        timeout_seconds=min(config.agent.timeout_seconds or TIMEOUT_SECONDS, TIMEOUT_SECONDS),
    ), heartbeat_seconds=30)
    log_path = paths.logs / "report.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    display = LiveDisplay(console=console, title="writing summary", verbose=False)

    def on_event(event: AgentEvent) -> None:
        display.handle_event(event)

    from autohelix.sandbox import Sandbox

    sandbox = Sandbox(project, paths)
    expected_tip = run_tip(paths)
    had_ref = git(project, "rev-parse", "--verify", paths.ref, check=False).returncode == 0
    worktree = sandbox.create_worktree(-1, expected_tip)
    cwd = worktree.working_dir
    tree_before = _changed_files(cwd, worktree.git)
    started = time.monotonic()
    try:
        with Live(display, console=console, refresh_per_second=10, transient=True):
            result = agent.run(
                worktree_path=cwd,
                prompt=build_report_prompt(config, project, config_path, draft, paths.run_id),
                iteration=0, log_path=log_path, event_callback=on_event,
            )
            display.flush()
        if touched := sorted(_changed_files(cwd, worktree.git) - tree_before):
            console.print("[yellow]The summary agent changed candidate files:[/yellow] " + ", ".join(touched))
    finally:
        if worktree:
            try:
                sandbox.normalize(worktree, expected_tip)
                if not had_ref:
                    git(project, "update-ref", "--no-deref", "-d", paths.ref, expected_tip)
            finally:
                sandbox.discard_worktree(worktree)
    seconds = round(time.monotonic() - started, 1)

    if not draft.is_file() or not draft.read_text().strip():
        draft.unlink(missing_ok=True)
        console.print("[yellow]The agent finished without writing a summary; "
                      "the previous report (if any) is unchanged.[/yellow]")
        return False
    draft.replace(out / "report.md")
    info = {"last_iteration": last, "written_at": time.time(), "seconds": seconds,
            "cost_usd": (result.usage or {}).get("cost_usd")}
    (out / "report.json").write_text(json.dumps(info))
    console.print(f"[green]Summary of iterations 1–{last} written to {out / 'report.md'}[/green]")
    return True


def _changed_files(project: Path, context) -> set[str]:
    """Files git reports as modified or untracked (.autohelix/ is ignored)."""
    result = context.run("status", "--porcelain", cwd=project, check=False)
    return {line[3:] for line in result.stdout.splitlines()} if result.returncode == 0 else set()
