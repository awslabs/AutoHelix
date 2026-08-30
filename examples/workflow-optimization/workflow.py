"""Editable LLM workflow: route context, prompt, validate, and execute."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol


HERE = Path(__file__).resolve().parent
PROMPT_PATH = HERE / "prompt.md"
SKILL_PATH = HERE / "SKILL.md"

# Tunable workflow controls.
CONTEXT_MODE = "routed"  # "routed" or "all"
INCLUDE_SKILL = True
MAX_ATTEMPTS = 1


@dataclass(frozen=True)
class Decision:
    action: str
    reason_code: str


@dataclass(frozen=True)
class ModelResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str | None = None


class ModelClient(Protocol):
    def complete(self, prompt: str) -> ModelResponse:
        """Return one model completion."""


@dataclass
class WorkflowResult:
    decision: Decision | None = None
    final_state: str | None = None
    error: str | None = None
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    models: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    trace: list[dict[str, Any]] = field(default_factory=list)


def select_policy_names(request: str, policies: dict[str, str]) -> list[str]:
    """Select context with a deliberately simple baseline router."""
    if CONTEXT_MODE == "all":
        return sorted(policies)

    lowered = request.lower()
    if any(word in lowered for word in ("charge", "charged", "billing")):
        return ["billing"]
    if any(word in lowered for word in ("damaged", "return", "unopened")):
        return ["returns"]
    if any(word in lowered for word in ("delivery", "delivered", "package")):
        return ["delivery"]
    return []


def render_prompt(
    *,
    case_id: str,
    request: str,
    record: dict[str, Any],
    policies: dict[str, str],
    policy_names: list[str],
    feedback: str,
) -> str:
    policy_text = "\n\n".join(
        f"## {name}\n{policies[name]}" for name in policy_names
    )
    skill = SKILL_PATH.read_text().strip() if INCLUDE_SKILL else "(not supplied)"
    return (
        PROMPT_PATH.read_text()
        .replace("{{CASE_ID}}", case_id)
        .replace("{{SKILL}}", skill)
        .replace("{{POLICIES}}", policy_text)
        .replace("{{REQUEST}}", request)
        .replace("{{RECORD}}", json.dumps(record, indent=2, sort_keys=True))
        .replace("{{FEEDBACK}}", feedback or "(first attempt)")
    )


def parse_decision(text: str) -> Decision:
    payload = json.loads(text.strip())
    if not isinstance(payload, dict):
        raise ValueError("decision must be a JSON object")
    if set(payload) != {"action", "reason_code"}:
        raise ValueError("decision must contain only action and reason_code")
    return Decision(str(payload["action"]), str(payload["reason_code"]))


def solve_request(
    *,
    case_id: str,
    request: str,
    record: dict[str, Any],
    policies: dict[str, str],
    model: ModelClient,
    execute: Callable[[Decision], str],
) -> WorkflowResult:
    """Run one request through the editable workflow."""
    result = WorkflowResult()
    started = time.monotonic()
    policy_names = select_policy_names(request, policies)
    result.trace.append(
        {
            "stage": "context",
            "policy_names": policy_names,
            "include_skill": INCLUDE_SKILL,
        }
    )

    try:
        feedback = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            prompt = render_prompt(
                case_id=case_id,
                request=request,
                record=record,
                policies=policies,
                policy_names=policy_names,
                feedback=feedback,
            )
            response = model.complete(prompt)
            result.model_calls += 1
            result.input_tokens += response.input_tokens
            result.output_tokens += response.output_tokens
            result.cost_usd += response.cost_usd
            if response.model and response.model not in result.models:
                result.models.append(response.model)
            result.trace.append(
                {"stage": "model", "attempt": attempt, "response": response.text}
            )
            try:
                result.decision = parse_decision(response.text)
                result.final_state = execute(result.decision)
                result.trace.append(
                    {
                        "stage": "execute",
                        "action": result.decision.action,
                        "reason_code": result.decision.reason_code,
                        "final_state": result.final_state,
                    }
                )
                break
            except ValueError as exc:
                feedback = f"{type(exc).__name__}: {exc}"
                result.trace.append(
                    {
                        "stage": "validation",
                        "attempt": attempt,
                        "error": feedback,
                    }
                )
                if attempt == MAX_ATTEMPTS:
                    raise
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.trace.append({"stage": "error", "message": result.error})
    finally:
        result.latency_ms = (time.monotonic() - started) * 1000

    return result
