# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run summary: one agent call that writes the selected run's report.md.

The harness calls `write_report` when a run finishes; `autohelix report` calls it on demand.
The agent writes only the narrative; the viewer shows the facts around it.
"""

import dataclasses
import json
import time
from pathlib import Path

from rich.text import Text

from autohelix import terminal
from autohelix.config import Config
from autohelix.experiment import has_files
from autohelix.history import History
from autohelix.runs import RunPaths, RunLock, claim_copies, ensure_copies, remove_copies, run_tip, git

INSTRUCTIONS = """\
Write a short summary of this run for someone who didn't watch it. Cover what was achieved, \
which changes mattered and why, what was tried and didn't work, and what to try next. If \
anything seems worth checking before relying on the results, mention it. Refer to iterations \
as "iter N". Use whatever structure suits this run.

Keep it proportional to the run: a few paragraphs for a short run. The page around the summary \
already shows the iteration table, metric chart and run cost; don't reproduce them. No title; \
start with the result."""

TIMEOUT_SECONDS = 15 * 60  # cap: agent.timeout can be hours for ML runs

WRITE_TO = "Write the summary in Markdown to "


def read_report_info(project: Path, run_id: str | None = None) -> dict | None:
    """What the saved report covers and what writing it took:
    {"last_iteration": N, "written_at": unix time, "seconds": s, "cost_usd": usd or None}."""
    paths = RunPaths(project, run_id)
    if not paths.run_id:
        return None
    try:
        return json.loads(paths.report_info.read_text())
    except (OSError, ValueError):
        return None


def build_report_prompt(config: Config, project: Path, draft: Path, run_id: str) -> str:
    paths = RunPaths(project, run_id)
    records = History(project, paths.run_id).load()
    last = max((r.iteration for r in records), default=0)
    identity = paths.read_identity()
    git = (f"- Git: baseline `{identity['base_commit']}`; accepted code `autohelix/{paths.run_id}` "
           f"(current accepted tip `{run_tip(paths, identity)}`). Use git diff baseline accepted-tip.")
    if not any(row.iteration > 0 and row.accepted for row in records):
        git += " No iteration was accepted."
    from autohelix.run_status import read_run_status

    reason = (read_run_status(project, paths.run_id) or {}).get("stop_reason")
    experiment = ("\n- experiment/: the experiment's folder (its scripts and material, without the config), as stored "
                  "when the run started; commands got a copy of it as $AUTOHELIX_EXPERIMENT."
                  if has_files(paths.experiment) else "")
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
- notes/: notes the agent wrote during the run (iter-N.md, plus shared notes), as of the last \
iteration. iterations/NNN/notes/ is the version saved with iteration N (NNN: three digits, 001); \
every version is in .internal/notes/<N>/.
- iterations/NNN/: one folder per iteration (000 is the baseline). Parts that don't apply are absent:
  - record.json: iteration N's record, as in history.jsonl.
  - prompt.md and agent.log: the agent's prompt and full transcript (not at the baseline). \
Transcripts are large; search them rather than reading them whole.
  - gates/<k>/ and stop/<k>/: the output of the entry at 1-based position k in gates: (stop:), \
metric gates included in the count: command.txt, stdout.txt and stderr.txt (long output keeps its \
head and tail), an ask's ask.log. Metric gates and gates that never ran have no folder.
  - observe/<name>/: each observe entry's stdout.txt (stderr.txt when not empty) and captured \
files; a review entry's stdout is its review text, review.log its transcript.
  - rejected.patch: a rejected attempt's change (not when over 2 MiB; the record's rejected_ref \
keeps the tree).
- .internal/attempts/NNN-<k>/: earlier attempts at iteration N that were superseded (a killed \
iteration, a baseline retry), laid out like iterations/NNN/.
- logs/: the harness's own logs: run-<time>.log per run or resume, report.log.
{git}
- config.yaml: the run's config, as stored when it started (verbatim); prompt_template.md: its prompt template's text, if it has one.{experiment}

## Instructions
{INSTRUCTIONS}

Only read. Don't run tests, benchmarks or experiments, and don't edit any file other than the summary.
{WRITE_TO}{draft}.
"""


def write_report(config: Config, project: Path, console, *, run_id: str, _owned: bool = False) -> bool:
    """Run the report agent; replace the saved report only if it wrote a new one."""
    from rich.live import Live

    from autohelix.agents import AgentConfig, AgentEvent, create_agent
    from autohelix.display import LiveDisplay
    from autohelix.terminal import with_room

    paths = RunPaths(project, run_id)
    if not _owned:
        with RunLock(paths.lock):
            return write_report(config, project, console, run_id=paths.run_id, _owned=True)
    draft = paths.report_draft
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.unlink(missing_ok=True)
    last = History(project, paths.run_id).get_last_iteration()

    # The run's agent settings (model, effort, extra_args too), within the cap.
    agent = create_agent(dataclasses.replace(
        config.agent, timeout=min(config.agent.timeout or TIMEOUT_SECONDS, TIMEOUT_SECONDS),
    ), heartbeat_seconds=30)
    log_path = paths.logs / "report.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    display = LiveDisplay(console=console, title="summary", verbose=False)

    def on_event(event: AgentEvent) -> None:
        display.handle_event(event)

    from autohelix.workspace import Copy, Repo

    # The summary agent reads accepted code in a copy of the run's tip; it can't change it.
    before = git(project, "rev-parse", "--verify", paths.ref, check=False).stdout.strip()
    tip = before or run_tip(paths)
    root = claim_copies(paths, lambda message: terminal.warn(console, message))
    copy = Copy.create(Repo(project), ensure_copies(root) / "iter-report", files=tip, head=tip,
                       refs=(paths.ref,), watched=True)  # the prompt names `autohelix/<id>`
    started = time.monotonic()
    # Every summary call counts in the run's cost (`History.summary_usages`): recorded
    # before it starts (cost unknown until it returns), then with its usage.
    from autohelix.usage_log import append_usage, new_call_id
    from autohelix.terminal import shown_path

    call = {"id": new_call_id(), "role": "summary", "started_at": time.time(), "last_iteration": last}
    append_usage({**call, "status": "started", "cost_usd": None}, paths.summary_usage)
    try:
        with Live(with_room(display), console=console, refresh_per_second=10, transient=True):
            result = agent.run(
                copy_path=copy.working_dir,
                prompt=build_report_prompt(config, project, draft, paths.run_id),
                iteration=0, log_path=log_path, event_callback=on_event,
            )
            display.flush()
        append_usage({**call, **(result.usage or {}), "status": "ok" if result.success else "failed",
                      "cost_usd": (result.usage or {}).get("cost_usd")}, paths.summary_usage)
        if touched := copy.changed():
            terminal.show(console, ", ".join(touched[:10]),
                          Text("Error: the summary agent changed accepted code files (discarded): ", style="red"))
    finally:
        # The agent can still reach the user's repository by absolute path.
        after = git(project, "rev-parse", "--verify", paths.ref, check=False).stdout.strip()
        if before and after != before:
            git(project, "update-ref", "--no-deref", paths.ref, before, after or "0" * len(before))
            terminal.show(console, f"Error: the summary agent moved {paths.ref}; restored it.", style="red")
        try:
            copy.remove()
            remove_copies(root)
        except Exception as e:  # noqa: BLE001 - keep the agent's outcome
            terminal.warn(console, f"Could not remove {copy.path}: {e}")
    seconds = round(time.monotonic() - started, 1)

    if not draft.is_file() or not draft.read_text().strip():
        draft.unlink(missing_ok=True)
        from autohelix.formatting import clean

        why = (f"Summary failed: {clean(result.error or f'exit status {result.exit_code}', 300)}"
               if not result.success else "The agent finished without writing a summary")
        terminal.warn(console, f"{why}; the previous report (if any) is unchanged (log: {shown_path(log_path)}).")
        return False
    draft.replace(paths.report)
    info = {"last_iteration": last, "written_at": time.time(), "seconds": seconds,
            "cost_usd": (result.usage or {}).get("cost_usd")}
    paths.report_info.write_text(json.dumps(info))
    return True

