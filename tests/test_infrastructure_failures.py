"""Infrastructure failures (credentials, the model service): each backend reports one
from its CLI's specific signals only; anything else stays an ordinary failure.

The lines are small hand-made fixtures with only the fields the backends read, in the
shape the CLIs print them."""

import json

import pytest

from autohelix.agents import AgentConfig, infrastructure_reason
from autohelix.agents.claudecode import ClaudeCodeAgent
from autohelix.agents.codex import CodexAgent
from autohelix.agents.opencode import OpenCodeAgent
from autohelix.agents.runner import ProcessRunResult


def _run(monkeypatch, tmp_path, agent, module, lines, *, exit_code=1, timed_out=False):
    def fake_run_process(**kwargs):
        for line in lines:
            kwargs["line_callback"](json.dumps(line))
        return ProcessRunResult(exit_code=exit_code, timed_out=timed_out)

    monkeypatch.setattr(f"autohelix.agents.{module}.run_process", fake_run_process)
    return agent.run(copy_path=tmp_path, prompt="test", iteration=1,
                     log_path=tmp_path / "agent.log", event_callback=lambda event: None)


@pytest.mark.parametrize(("status", "reason"), [
    (401, "authentication failed (HTTP 401)"),
    (403, "authentication failed (HTTP 403)"),
    (429, "model service error (HTTP 429)"),
    (503, "model service error (HTTP 503)"),
    (400, None), (404, None), (None, None), ("403", None), (True, None),
])
def test_infrastructure_reason(status, reason):
    assert infrastructure_reason(status) == reason


def _claude_result(**fields):
    return {"type": "result", "subtype": "success", "duration_ms": 10, "total_cost_usd": 0, "usage": {}, **fields}


def _api_error(status):
    return _claude_result(is_error=True, terminal_reason="api_error", api_error_status=status,
                          result="Invalid API key · Fix external API key")


class TestClaudeCode:
    def _run(self, monkeypatch, tmp_path, lines, **kwargs):
        agent = ClaudeCodeAgent(config=AgentConfig(timeout=5))
        return _run(monkeypatch, tmp_path, agent, "claudecode", lines, **kwargs)

    @pytest.mark.parametrize(("status", "reason"), [
        (401, "authentication failed (HTTP 401)"),
        (403, "authentication failed (HTTP 403)"),
        # No real sample of these (none captured): the same fields, by the CLI's schema.
        (429, "model service error (HTTP 429)"),
        (529, "model service error (HTTP 529)"),
    ])
    def test_an_api_error_result_is_an_infrastructure_failure(self, monkeypatch, tmp_path, status, reason):
        result = self._run(monkeypatch, tmp_path, [_api_error(status)])
        assert not result.success and result.error == "Invalid API key · Fix external API key"
        assert result.infrastructure == reason

    def test_other_errors_are_ordinary_failures(self, monkeypatch, tmp_path):
        lines = [_claude_result(is_error=True, terminal_reason="api_error", api_error_status=400,
                                result="API Error: invalid model")]
        assert self._run(monkeypatch, tmp_path, lines).infrastructure is None
        lines = [_claude_result(is_error=True, api_error_status=403, result="something else")]
        assert self._run(monkeypatch, tmp_path, lines).infrastructure is None
        assert self._run(monkeypatch, tmp_path, [], exit_code=1).infrastructure is None

    def test_a_failed_continuation_after_an_answer_is_a_failed_call(self, monkeypatch, tmp_path):
        """The session answered (`completed`), went on when a background task ended, and
        every API call failed: the call failed, with the infrastructure reason."""
        lines = [_claude_result(is_error=False, terminal_reason="completed", result="Done."),
                 _api_error(403), _api_error(403)]
        result = self._run(monkeypatch, tmp_path, lines)
        assert not result.success and result.infrastructure == "authentication failed (HTTP 403)"

    def test_the_last_failed_result_decides(self, monkeypatch, tmp_path):
        lines = [_api_error(403), _claude_result(is_error=True, result="something else")]
        result = self._run(monkeypatch, tmp_path, lines)
        assert not result.success and result.error == "something else"
        assert result.infrastructure is None

    def test_a_timeout_is_not_one(self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, [_api_error(403)], exit_code=124, timed_out=True)
        assert not result.success and result.infrastructure is None

    def test_a_successful_call_has_none(self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, [_claude_result(is_error=False, terminal_reason="completed")],
                           exit_code=0)
        assert result.success and result.infrastructure is None


class TestCodex:
    def _run(self, monkeypatch, tmp_path, message, **kwargs):
        agent = CodexAgent(config=AgentConfig(cli="codex"))
        lines = [{"type": "error", "message": f"Reconnecting... 1/5 ({message})"},
                 {"type": "turn.failed", "error": {"message": message}}]
        return _run(monkeypatch, tmp_path, agent, "codex", lines, **kwargs)

    @pytest.mark.parametrize("status", [401, 403])
    def test_an_unauthorized_status_is_an_infrastructure_failure(self, monkeypatch, tmp_path, status):
        message = f"unexpected status {status} Unauthorized: Missing bearer or basic authentication in header"
        result = self._run(monkeypatch, tmp_path, message)
        assert not result.success and result.error == message
        assert result.infrastructure == f"authentication failed (HTTP {status})"

    @pytest.mark.parametrize("message", [
        "unexpected status 500 Internal Server Error",  # only 401/403: the message is all there is
        "unexpected status 4010",
        "credentials expired",
    ])
    def test_other_messages_are_ordinary_failures(self, monkeypatch, tmp_path, message):
        result = self._run(monkeypatch, tmp_path, message)
        assert not result.success and result.infrastructure is None


class TestOpenCode:
    def _run(self, monkeypatch, tmp_path, lines, **kwargs):
        agent = OpenCodeAgent(config=AgentConfig(cli="opencode"))
        return _run(monkeypatch, tmp_path, agent, "opencode", lines, **kwargs)

    @staticmethod
    def _error(status):
        return {"type": "error", "error": {"name": "APIError", "data": {
            "message": "invalid x-api-key", "statusCode": status, "isRetryable": False}}}

    def test_an_error_event_fails_the_call_at_exit_0(self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, [self._error(401)], exit_code=0)
        assert not result.success and result.exit_code == 0
        assert result.error == "invalid x-api-key"
        assert result.infrastructure == "authentication failed (HTTP 401)"

    @pytest.mark.parametrize(("status", "reason"), [
        (403, "authentication failed (HTTP 403)"),
        (429, "model service error (HTTP 429)"),
        (502, "model service error (HTTP 502)"),
        (400, None),
        (None, None),
    ])
    def test_its_status_decides(self, monkeypatch, tmp_path, status, reason):
        result = self._run(monkeypatch, tmp_path, [self._error(status)], exit_code=0)
        assert not result.success and result.infrastructure == reason

    def test_an_error_event_without_data_is_an_ordinary_failure(self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, [{"type": "error", "error": {"name": "UnknownError"}}],
                           exit_code=0)
        assert not result.success and result.error == "UnknownError" and result.infrastructure is None

    def test_a_non_zero_exit_alone_is_an_ordinary_failure(self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, [], exit_code=3)
        assert result.error == "OpenCode exited with status 3" and result.infrastructure is None
