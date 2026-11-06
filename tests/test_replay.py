"""Synthetic records follow the existing backend parser fixtures in test_agents."""

import json
import subprocess

import pytest

from autohelix.agents import AgentEventKind as Kind
from autohelix.agents.claudecode import ClaudeCodeAgent
from autohelix.agents.codex import CodexAgent
from autohelix.agents.opencode import OpenCodeAgent
from autohelix.replay import LogReplay, LogTail


def parse(replay, payload):
    return replay.parse(json.dumps(payload))


@pytest.fixture(autouse=True)
def prohibit_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Log replay must never execute a backend or readiness check")

    for backend in (ClaudeCodeAgent, CodexAgent, OpenCodeAgent):
        monkeypatch.setattr(backend, "run", forbidden)
        monkeypatch.setattr(backend, "_command_path", forbidden)
        monkeypatch.setattr(backend, "_build_command", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)


@pytest.mark.parametrize("hint", [None, "claude", "codex", "opencode", "unknown"])
@pytest.mark.parametrize("backend,records,kinds,text", [
    ("codex", [
        {"type": "thread.started"},
        {"type": "item.started", "item": {"type": "command_execution", "command": "pytest"}},
        {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "2 passed", "exit_code": 0}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Validation done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
    ], [Kind.STATUS, Kind.TOOL_USE, Kind.TOOL_RESULT, Kind.ASSISTANT_TEXT, Kind.RESULT], "2 passed"),
    ("opencode", [
        {"type": "step_start"},
        {"type": "tool_use", "part": {"tool": "bash", "state": {"input": {"command": "pytest"}, "output": "2 passed"}}},
        {"type": "text", "part": {"text": "Validation done"}},
        {"type": "step_finish", "part": {"reason": "stop"}},
    ], [Kind.STATUS, Kind.TOOL_USE, Kind.TOOL_RESULT, Kind.ASSISTANT_TEXT, Kind.RESULT], "2 passed"),
    ("claude", [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Validating"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}},
        ]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "2 passed"}]}},
        {"type": "result", "subtype": "success"},
    ], [Kind.STATUS, Kind.ASSISTANT_TEXT, Kind.TOOL_USE, Kind.TOOL_INPUT, Kind.TOOL_RESULT, Kind.RESULT], "2 passed"),
])
def test_backend_shapes_override_config_hint(hint, backend, records, kinds, text):
    replay = LogReplay(hint)
    events = [event for record in records for event in parse(replay, record)]
    assert [e.kind for e in events] == kinds
    assert any(e.text == text for e in events)
    assert replay.backend == backend


def test_claude_stream_and_summary_do_not_duplicate_activity():
    replay = LogReplay()
    records = [
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Checking"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " tests"}}},
        {"type": "stream_event", "event": {"type": "content_block_stop"}},
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "name": "Bash"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"command":"pytest"}'}}},
        {"type": "stream_event", "event": {"type": "content_block_stop"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Checking tests"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}},
        ]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ERROR: failing test", "is_error": True}]}},
    ]
    events = [event for record in records for event in parse(replay, record)]
    assert sum(e.kind == Kind.TOOL_USE for e in events) == 1
    assert sum(e.kind == Kind.TOOL_INPUT and bool(e.metadata.get("final")) for e in events) == 1
    assert sum(e.kind == Kind.ASSISTANT_TEXT and bool(e.metadata.get("final")) for e in events) == 1
    assert events[-1].metadata["is_error"]
    # Suppression is scoped to a single streamed message.
    events = parse(replay, {"type": "assistant", "message": {"content": [{"type": "text", "text": "Next message"}]}})
    assert events[0].text == "Next message"


def test_claude_usage_only_stream_does_not_hide_summary():
    replay = LogReplay()
    parse(replay, {"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {}}}})
    events = parse(replay, {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "Reviewing"}]}})
    assert events[0].kind == Kind.THINKING


def test_malformed_claude_stream_does_not_hide_valid_summary():
    replay = LogReplay()
    events = parse(replay, {"type": "stream_event", "event": {"type": "content_block_delta", "delta": None}})
    assert events[0].kind == Kind.RAW
    events = parse(replay, {"type": "assistant", "message": {"content": [{"type": "text", "text": "Recovered"}]}})
    assert events[0].text == "Recovered"


def test_opencode_failed_tool_with_no_output():
    events = parse(LogReplay(), {"type": "tool_use", "part": {
        "tool": "bash", "state": {"status": "error", "error": "Permission denied"},
    }})
    assert events[-1].kind == Kind.ERROR
    assert events[-1].text == "Permission denied"


@pytest.mark.parametrize("record", [
    {"type": "item.completed", "item": {"type": "command_execution", "command": "pytest", "exit_code": 1}},
    {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "FAILED", "exit_code": 1}},
    {"type": "tool_use", "part": {"tool": "bash", "state": {"output": "FAILED", "status": "error"}}},
])
def test_failed_tools_are_marked_even_without_output(record):
    events = parse(LogReplay(), record)
    assert events[-1].kind == Kind.TOOL_RESULT
    assert events[-1].metadata["is_error"]


@pytest.mark.parametrize("record,kind", [
    ({"type": "turn.failed", "error": {"message": "Credentials expired"}}, Kind.ERROR),
    ({"type": "result", "is_error": True, "result": "API Error"}, Kind.ERROR),
    ({"type": "error", "error": {"message": "Rate limited"}}, Kind.ERROR),
    ({"type": "warning", "message": "Retrying"}, Kind.WARNING),
])
def test_errors_and_warnings(record, kind):
    assert parse(LogReplay(), record)[0].kind == kind


@pytest.mark.parametrize("line", [
    "stderr: backend unavailable",
    '{"type":',
    "null", "[]", "42", '"text"',
    '{"type":"future.record","detail":"important output"}',
    '{"type":"stream_event","event":{"type":"future.event","detail":"important output"}}',
    '{"type":"item.completed","item":{"type":"file_change","changes":[{"path":"main.py"}]}}',
    '{"type":"item.started","item":null}',
    '{"type":"text","part":null}',
    '{"type":"assistant","message":{"content":null}}',
    '{"type":"result","total_cost_usd":"bad"}',
])
def test_unknown_or_malformed_record_is_raw_and_next_record_still_works(line):
    replay = LogReplay()
    assert replay.parse(line)[0].kind == Kind.RAW
    assert replay.parse(line)[0].text == line
    assert parse(replay, {"type": "thread.started"})[0].kind == Kind.STATUS


def test_empty_lines_are_ignored():
    assert LogReplay().parse("  \n") == []


def test_partial_writes_wait_for_newline_without_duplicates(tmp_path):
    path = tmp_path / "agent.log"
    tail = LogTail(path)
    path.write_bytes(b'{"type":"item.completed","item":{"type":"agent_message","text":"caf\xc3')
    assert tail.read() == []
    assert tail.updated
    assert tail.read() == []
    assert not tail.updated
    with path.open("ab") as log:
        log.write(b'\xa9"}}\nplain output\n')
    assert [e.text for e in tail.read()] == ["café", "plain output"]
    assert tail.read() == []
    with path.open("ab") as log:
        log.write(b'{"type":\n')
    assert tail.read()[0].kind == Kind.RAW
    assert tail.read() == []


@pytest.mark.parametrize("change", ["truncate", "replace", "rewrite_same_size", "rewrite_larger"])
def test_changed_log_resets_parser_and_cursor(tmp_path, change):
    path = tmp_path / "agent.log"
    old = '{"type":"thread.started"}\n' + "old output\n" * 10
    path.write_text(old)
    tail = LogTail(path, "codex")
    assert len(tail.read()) == 11
    assert not tail.reset
    if change == "replace":
        replacement = tmp_path / "replacement"
        replacement.write_text('{"type":"text","part":{"text":"new output"}}\n')
        replacement.replace(path)
    elif change == "truncate":
        path.write_text("new output\n")
    else:
        path.write_text(old.replace("old output", "new output"))
        if change == "rewrite_larger":
            with path.open("a") as log:
                log.write("extra output\n")
    events = tail.read()
    if change.startswith("rewrite"):
        assert len(events) == 11 + (change == "rewrite_larger")
        assert not any(e.text == "old output" for e in events)
    else:
        assert [e.text for e in events] == ["new output"]
    assert tail.reset
    assert tail.read() == []
    assert not tail.reset


def test_unreadable_tail_can_be_retried_without_replaying(tmp_path):
    path = tmp_path / "agent.log"
    path.write_text("output\n")
    tail = LogTail(path)
    tail.read()
    moved = path.rename(tmp_path / "moved")
    with pytest.raises(OSError):
        tail.read()
    moved.rename(path)
    assert tail.read() == []


def test_empty_truncation_resets_only_once_then_follows_new_output(tmp_path):
    path = tmp_path / "agent.log"
    path.write_text("old output\n")
    tail = LogTail(path)
    tail.read()
    path.write_text("")
    assert tail.read() == []
    assert tail.reset
    assert tail.read() == []
    assert not tail.reset
    path.write_text("new output\n")
    assert tail.read()[0].text == "new output"
    assert not tail.reset
