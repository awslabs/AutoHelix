# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Mock agent for testing - makes deterministic changes without calling an LLM."""

import json
import subprocess
import time
from pathlib import Path

from autohelix.agents import (
    AgentConfig,
    AgentEvent,
    AgentEventKind,
    AgentRunResult,
    EventCallback,
)


class MockAgent:
    """Mock agent that makes predictable changes for testing.

    Useful for integration tests that need to exercise the full harness loop
    (worktree creation, constraint checking, merging) without invoking a real LLM.

    Config settings:
        change_file: File to modify (default: "mock_change.txt")
        change_content: Content to write (default: "Mock iteration {iteration}")
        should_fail: If True, returns failure (default: False)
        notes_content: Optional notes to write
        cost_usd: Synthetic cost reported per iteration (default: none). Lets the
            mock exercise the harness cost budget without a real LLM.
        sleep_seconds: Wall-clock seconds to sleep before returning (default: 0).
            Consumes real time so the total-time budget can be exercised offline.

    For a run-summary prompt it writes a fixed summary instead of changing code.
    """

    display_name = "Mock Agent"

    def __init__(self, config: AgentConfig, heartbeat_seconds: int = 30):
        self.config = config
        self.heartbeat_seconds = heartbeat_seconds

    def run(
        self,
        worktree_path: Path,
        prompt: str,
        iteration: int,
        log_path: Path,
        event_callback: EventCallback,
        project_path: Path | None = None,
    ) -> AgentRunResult:
        settings = self.config.settings

        # Emit some events for realism
        event_callback(AgentEvent(kind=AgentEventKind.STATUS, text="mock agent starting"))
        event_callback(AgentEvent(kind=AgentEventKind.THINKING, text="Analyzing the codebase..."))

        from autohelix.report import WRITE_TO

        if WRITE_TO in prompt:
            draft = prompt.rsplit(WRITE_TO, 1)[1].strip().removesuffix(".")
            Path(draft).write_text("Mock summary of the run.\n")
            return AgentRunResult(success=True, exit_code=0)

        if self.config.read_only:  # a judge call (`autohelix ask` or `review`)
            return self._judge(settings, log_path, prompt, worktree_path)

        # Check if we should fail
        if settings.get("should_fail", False):
            event_callback(AgentEvent(kind=AgentEventKind.ERROR, text="Mock failure triggered"))
            return AgentRunResult(success=False, exit_code=1, error="Mock failure")

        # Make a change
        change_file = settings.get("change_file", "mock_change.txt")
        change_content = settings.get("change_content", f"Mock iteration {iteration}")

        # Support {iteration} placeholder in content
        if "{iteration}" in change_content:
            change_content = change_content.replace("{iteration}", str(iteration))

        file_path = worktree_path / change_file
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Append to file if it exists, otherwise create
        if file_path.exists():
            existing = file_path.read_text()
            file_path.write_text(f"{existing}\n{change_content}")
        else:
            file_path.write_text(change_content + "\n")

        event_callback(AgentEvent(
            kind=AgentEventKind.TOOL_USE,
            text="Write",
            metadata={"file_path": str(file_path)},
        ))

        # Optionally write notes
        notes_content = settings.get("notes_content")
        if notes_content:
            notes_dir = worktree_path / ".autohelix-iteration" / "notes"
            notes_dir.mkdir(parents=True, exist_ok=True)
            notes_path = notes_dir / "notes.md"

            if "{iteration}" in notes_content:
                notes_content = notes_content.replace("{iteration}", str(iteration))

            notes_path.write_text(notes_content)

        # Stage the change
        subprocess.run(["git", "add", change_file], cwd=worktree_path, capture_output=True)

        event_callback(AgentEvent(kind=AgentEventKind.STATUS, text="mock agent completed"))
        event_callback(AgentEvent(kind=AgentEventKind.RESULT, text="success"))

        # Write log
        log_path.write_text(f"Mock agent ran for iteration {iteration}\nPrompt: {prompt[:200]}...")

        # Synthetic usage so budget controls can be demonstrated without a real LLM.
        usage: dict = {}
        sleep_seconds = float(settings.get("sleep_seconds", 0) or 0)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
            usage["duration_ms"] = int(sleep_seconds * 1000)
        if settings.get("cost_usd") is not None:
            usage["cost_usd"] = float(settings["cost_usd"])

        return AgentRunResult(success=True, exit_code=0, usage=usage)

    def _judge(self, settings: dict, log_path: Path, prompt: str, cwd: Path) -> AgentRunResult:
        """Answer a judge prompt. Settings: `judge_answer` ("yes"/"no", default "yes"),
        `judge_reason`, `judge_output` (raw final text, overrides both), `judge_fail`."""
        log_path.write_text(f"Mock judge\nSystem: {self.config.system_prompt}\nPrompt: {prompt}\n")
        if settings.get("judge_change_file"):
            path = cwd / settings["judge_change_file"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(settings.get("judge_change_content", "Unvalidated judge edit\n"))
        usage = {"cost_usd": float(settings["cost_usd"])} if settings.get("cost_usd") is not None else {}
        if settings.get("judge_fail"):
            return AgentRunResult(success=False, exit_code=1, error="Mock judge failure", usage=usage)
        if "judge_output" in settings:
            return AgentRunResult(success=True, exit_code=0, usage=usage, final_text=str(settings["judge_output"]))
        schema = self.config.output_schema or {}
        if "review" in schema.get("properties", {}):  # `autohelix review`
            # `review_text` (default "mock review"), `review_metrics` (the numbers; none by
            # default, so a review asked for numbers fails unless they are set).
            reply = {"review": settings.get("review_text", "mock review")}
            if "review_metrics" in settings:
                reply["metrics"] = settings["review_metrics"]
            return AgentRunResult(success=True, exit_code=0, usage=usage, final_text=json.dumps(reply))
        answer = {"answer": settings.get("judge_answer", "yes"),
                  "reason": settings.get("judge_reason", "mock judgment")}
        return AgentRunResult(success=True, exit_code=0, usage=usage, final_text=json.dumps(answer))
