# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Read-only interpretation of saved agent logs (Claude Code, Codex, OpenCode)."""

import json
import os
from pathlib import Path

from autohelix.agents import AgentConfig, AgentEvent, AgentEventKind
from autohelix.agents.claudecode import ClaudeCodeAgent
from autohelix.agents.codex import CodexAgent
from autohelix.agents.opencode import OpenCodeAgent


_TYPES = {
    "claude": {"system", "stream_event", "assistant", "user", "result"},
    "codex": {"thread.started", "turn.started", "turn.failed", "turn.completed",
              "item.started", "item.completed"},
    "opencode": {"step_start", "text", "tool_use", "step_finish"},
}
_PARSERS = {"claude": ClaudeCodeAgent, "codex": CodexAgent, "opencode": OpenCodeAgent}
_STREAM_TYPES = {
    "content_block_start", "content_block_delta", "content_block_stop",
    "message_start", "message_delta", "message_stop",
}


class LogReplay:
    """parse(line) returns AgentEvents without executing agents.

    Shapes override the hint; stream/final metadata survives, Claude summaries
    are deduplicated, and unknown/malformed records remain RAW.
    """

    def __init__(self, backend: str | None = None):
        self.backend = backend if backend in _PARSERS else None
        self._parsers: dict = {}
        self._claude_streamed = False

    def parse(self, line: str) -> list[AgentEvent]:
        line = line.strip()
        if not line:
            return []
        raw = [AgentEvent(AgentEventKind.RAW, line)]
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                return raw
            kind = payload.get("type")
            if kind in {"warning", "error"}:
                detail = payload.get("message") or payload.get("error") or line
                if isinstance(detail, dict):
                    detail = detail.get("message") or json.dumps(detail)
                return [AgentEvent(AgentEventKind(kind), str(detail))]
            backend = next((name for name, types in _TYPES.items() if kind in types), None)
            if backend is None:
                return raw
            self.backend = backend
            if backend == "codex" and kind.startswith("item."):
                if payload.get("item", {}).get("type") not in {"command_execution", "agent_message"}:
                    return raw
            if backend == "claude":
                if kind == "stream_event":
                    if payload.get("event", {}).get("type") not in _STREAM_TYPES:
                        return raw
                elif kind == "assistant":
                    streamed, self._claude_streamed = self._claude_streamed, False
                    if streamed:
                        return []
            if backend not in self._parsers:
                self._parsers[backend] = _PARSERS[backend](AgentConfig(backend=backend))
            parser = self._parsers[backend]
            events: list[AgentEvent] = []
            if backend == "claude":
                parser._handle_line(line, events.append, replay=True)
                if events and kind == "stream_event" and payload["event"]["type"] in {"content_block_start", "content_block_delta"}:
                    self._claude_streamed = True
                block = payload.get("event", {}).get("content_block") or {}
                if kind == "stream_event" and block.get("type") in {"thinking", "redacted_thinking"}:
                    # Claude Code often logs only a signature for thinking; mark where it happened.
                    events.append(AgentEvent(AgentEventKind.THINKING, "", metadata={"hidden": True}))
            else:
                parser._handle_line(line, events.append)
            for event in events:
                if event.kind == AgentEventKind.TOOL_RESULT:
                    failed = (
                        payload.get("item", {}).get("exit_code")
                        or payload.get("item", {}).get("status") == "failed"
                        or payload.get("part", {}).get("state", {}).get("status") == "error"
                    )
                    if failed:
                        event.metadata["is_error"] = True
            if backend == "opencode" and kind == "tool_use":
                state = payload.get("part", {}).get("state", {})
                if state.get("status") == "error" and not any(e.kind == AgentEventKind.TOOL_RESULT for e in events):
                    events.append(AgentEvent(AgentEventKind.ERROR, str(state.get("error") or "Tool failed")))
            if not events and kind not in {"stream_event", "user"}:
                return raw
            return events
        except Exception:
            # Parsing is observational, including malformed fields in valid JSON.
            return raw


class LogTail:
    """read() returns events from complete UTF-8 lines; partial lines wait.

    reset signals replacement/truncation: clear this log's displayed history.
    I/O errors propagate so callers can warn and retry.
    """

    def __init__(self, path: Path, backend: str | None = None, opener=None):
        """`opener()`, if given, returns a read descriptor for the log (or None,
        which counts as an I/O error) instead of opening `path` by name: callers
        reading logs from run state pass `safe_open.open_run_log`."""
        self.path = path
        self._opener = opener
        self._hint = backend
        self.replay = LogReplay(backend)
        self._offset = self._size = 0
        self._identity = None
        self._anchor = (0, b"")
        self.reset = False
        self.updated = False

    def read(self) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        self.reset = False
        self.updated = False
        if self._opener is not None:
            fd = self._opener()
            if fd is None:
                raise OSError(f"not a regular, single-link log file: {self.path}")
            stream = os.fdopen(fd, "rb")
        else:
            stream = self.path.open("rb")
        with stream as log:
            stat = os.fstat(log.fileno())
            identity = (stat.st_dev, stat.st_ino)
            anchor_offset, anchor = self._anchor
            log.seek(anchor_offset)
            rewritten = log.read(len(anchor)) != anchor
            self.updated = identity != self._identity or stat.st_size != self._size or rewritten
            if self._identity is not None and (identity != self._identity or stat.st_size < self._size or rewritten):
                self._offset = 0
                self._anchor = (0, b"")
                self.replay = LogReplay(self._hint)
                self.reset = True
            self._identity, self._size = identity, stat.st_size
            log.seek(self._offset)
            for line in log:
                if not line.endswith(b"\n"):
                    break
                self._offset += len(line)
                self._anchor = (self._offset - min(64, len(line)), line[-64:])
                events.extend(self.replay.parse(line.decode("utf-8", errors="replace")))
        return events
