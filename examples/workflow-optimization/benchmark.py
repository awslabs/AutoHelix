#!/usr/bin/env python3
"""Frozen workload, simulator, model adapter, and evaluator."""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from workflow import Decision, ModelClient, ModelResponse, solve_request


HERE = Path(__file__).resolve().parent
TRACKED_METRICS = ("task_success_rate", "avg_tokens")

POLICIES = {
    "global": """
Apply these controls before topic-specific policy:
- If requester_matches_account is false, escalate with reason identity_review.
- If order_total_usd is greater than 500 and the proposed action would refund
  or replace an order, escalate with reason high_value_review.
""".strip(),
    "billing": """
A confirmed duplicate charge should use action refund with reason
duplicate_charge. Refund only the duplicate amount.
""".strip(),
    "returns": """
- A damaged item reported within 30 days should be replaced with reason
  damaged_in_window when inventory is available.
- If that item is out of stock, refund with reason damaged_out_of_stock.
- An unopened change-of-mind return within 14 days should be refunded with
  reason return_in_window. After 14 days, deny with reason return_window_expired.
""".strip(),
    "delivery": """
A package marked delivered but not received should be escalated with reason
delivery_investigation. Do not refund or replace it first.
""".strip(),
}


@dataclass(frozen=True)
class Case:
    case_id: str
    request: str
    record: dict[str, Any]
    expected: Decision
    expected_state: str


CASES = (
    Case(
        "duplicate-charge",
        "I was charged twice for order A-104. Please return the extra charge.",
        {
            "order_id": "A-104",
            "requester_matches_account": True,
            "order_total_usd": 84,
            "duplicate_charge_usd": 84,
        },
        Decision("refund", "duplicate_charge"),
        "duplicate_refunded",
    ),
    Case(
        "damaged-in-stock",
        "The mug in order A-205 arrived damaged. Can you send another?",
        {
            "order_id": "A-205",
            "requester_matches_account": True,
            "order_total_usd": 28,
            "days_since_delivery": 6,
            "item_in_stock": True,
        },
        Decision("replace", "damaged_in_window"),
        "replacement_created",
    ),
    Case(
        "damaged-out-of-stock",
        "My order A-306 arrived damaged, but the item now shows out of stock.",
        {
            "order_id": "A-306",
            "requester_matches_account": True,
            "order_total_usd": 46,
            "days_since_delivery": 9,
            "item_in_stock": False,
        },
        Decision("refund", "damaged_out_of_stock"),
        "order_refunded",
    ),
    Case(
        "late-return",
        "I changed my mind about unopened order A-407 and want to return it.",
        {
            "order_id": "A-407",
            "requester_matches_account": True,
            "order_total_usd": 63,
            "days_since_delivery": 21,
            "item_opened": False,
        },
        Decision("deny", "return_window_expired"),
        "closed_without_action",
    ),
    Case(
        "high-value-damage",
        "Order A-508 arrived damaged yesterday. Please replace it.",
        {
            "order_id": "A-508",
            "requester_matches_account": True,
            "order_total_usd": 780,
            "days_since_delivery": 1,
            "item_in_stock": True,
        },
        Decision("escalate", "high_value_review"),
        "specialist_review",
    ),
    Case(
        "identity-mismatch",
        "There is a duplicate charge on order A-609. Refund it.",
        {
            "order_id": "A-609",
            "requester_matches_account": False,
            "order_total_usd": 35,
            "duplicate_charge_usd": 35,
        },
        Decision("escalate", "identity_review"),
        "specialist_review",
    ),
)

ALLOWED_ACTIONS = {"refund", "replace", "deny", "escalate"}
ALLOWED_REASONS = {
    "duplicate_charge",
    "damaged_in_window",
    "damaged_out_of_stock",
    "return_in_window",
    "return_window_expired",
    "high_value_review",
    "identity_review",
    "delivery_investigation",
}


def execute_action(decision: Decision) -> str:
    if decision.action not in ALLOWED_ACTIONS:
        raise ValueError(f"unsupported action: {decision.action}")
    if decision.reason_code not in ALLOWED_REASONS:
        raise ValueError(f"unsupported reason_code: {decision.reason_code}")
    if decision.action == "refund":
        return (
            "duplicate_refunded"
            if decision.reason_code == "duplicate_charge"
            else "order_refunded"
        )
    return {
        "replace": "replacement_created",
        "deny": "closed_without_action",
        "escalate": "specialist_review",
    }[decision.action]


class ModelError(RuntimeError):
    pass


def parse_claude_output(output: str) -> ModelResponse:
    payload = json.loads(output)
    text = payload.get("result")
    if not isinstance(text, str):
        raise ModelError("Claude response did not contain text")
    usage = payload.get("usage") or {}
    input_tokens = sum(
        int(usage.get(name) or 0)
        for name in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )
    model = None
    model_usage = payload.get("modelUsage") or {}
    if len(model_usage) == 1:
        model_name, record = next(iter(model_usage.items()))
        model = str(record.get("canonicalModel") or model_name)
    return ModelResponse(
        text=text,
        input_tokens=input_tokens,
        output_tokens=int(usage.get("output_tokens") or 0),
        cost_usd=float(payload.get("total_cost_usd") or 0),
        model=model,
    )


class ClaudeCliModel:
    def __init__(self, model: str | None, timeout: int = 120):
        self.model = model
        self.timeout = timeout

    def complete(self, prompt: str) -> ModelResponse:
        command = [
            "claude",
            "--print",
            "--output-format",
            "json",
            "--tools",
            "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt",
            "Follow the user instructions and return only the requested response.",
        ]
        if self.model:
            command.extend(["--model", self.model])
        env = os.environ.copy()
        env["CLAUDE_CODE_SIMPLE"] = "1"

        try:
            result = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise ModelError(f"model command failed: {exc}") from exc
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise ModelError(f"model command failed: {detail[-1000:]}")
        return parse_claude_output(result.stdout)


class CommandModel:
    def __init__(self, command: str, timeout: int):
        self.command = shlex.split(command)
        self.timeout = timeout

    def complete(self, prompt: str) -> ModelResponse:
        result = subprocess.run(
            self.command,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        if result.returncode:
            raise ModelError(result.stderr.strip())
        return ModelResponse(result.stdout)


def create_model() -> ModelClient:
    timeout = int(os.environ.get("WORKFLOW_MODEL_TIMEOUT", "120"))
    custom = os.environ.get("WORKFLOW_MODEL_COMMAND")
    if custom:
        return CommandModel(custom, timeout)
    return ClaudeCliModel(
        model=os.environ.get("WORKFLOW_MODEL_NAME"),
        timeout=timeout,
    )


class FixtureModel:
    """Free deterministic model for smoke tests only."""

    def complete(self, prompt: str) -> ModelResponse:
        case_id = prompt.split("Case ID: ", 1)[1].splitlines()[0].strip()
        case = next(case for case in CASES if case.case_id == case_id)
        return ModelResponse(
            json.dumps(
                {
                    "action": case.expected.action,
                    "reason_code": case.expected.reason_code,
                }
            )
        )


def percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)] if ordered else 0.0


def evaluate(model: ModelClient, cases: Iterable[Case]) -> dict[str, Any]:
    records = []
    for case in cases:
        result = solve_request(
            case_id=case.case_id,
            request=case.request,
            record=case.record,
            policies=POLICIES,
            model=model,
            execute=execute_action,
        )
        actual = (
            {
                "action": result.decision.action,
                "reason_code": result.decision.reason_code,
                "final_state": result.final_state,
            }
            if result.decision
            else None
        )
        expected = {
            "action": case.expected.action,
            "reason_code": case.expected.reason_code,
            "final_state": case.expected_state,
        }
        valid = result.error is None and result.decision is not None
        passed = valid and actual == expected
        unsafe = bool(
            result.decision
            and result.decision.action in {"refund", "replace"}
            and case.expected.action in {"deny", "escalate"}
        )
        records.append(
            {
                "case_id": case.case_id,
                "request": case.request,
                "record": case.record,
                "passed": passed,
                "valid": valid,
                "unsafe": unsafe,
                "expected": expected,
                "actual": actual,
                "error": result.error,
                "model_calls": result.model_calls,
                "tokens": result.input_tokens + result.output_tokens,
                "cost_usd": result.cost_usd,
                "models": result.models,
                "latency_ms": round(result.latency_ms, 3),
                "trace": result.trace,
            }
        )

    total = len(records)
    metrics = {
        "task_success_rate": 100 * sum(row["passed"] for row in records) / total,
        "valid_decision_rate": 100 * sum(row["valid"] for row in records) / total,
        "unsafe_action_rate": 100 * sum(row["unsafe"] for row in records) / total,
        "avg_model_calls": statistics.fmean(row["model_calls"] for row in records),
        "avg_tokens": statistics.fmean(row["tokens"] for row in records),
        "avg_model_cost_usd": statistics.fmean(row["cost_usd"] for row in records),
        "p95_latency_ms": percentile_95([row["latency_ms"] for row in records]),
    }
    return {"metrics": metrics, "cases": records}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument(
        "--report", type=Path, default=HERE / "results" / "report.json"
    )
    args = parser.parse_args()

    by_id = {case.case_id: case for case in CASES}
    unknown = sorted(set(args.case_ids or []) - by_id.keys())
    if unknown:
        parser.error(f"unknown case(s): {', '.join(unknown)}")
    cases = [by_id[name] for name in args.case_ids] if args.case_ids else CASES
    report = evaluate(FixtureModel() if args.mock else create_model(), cases)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    print(f"Passed {sum(row['passed'] for row in report['cases'])}/{len(cases)} cases")
    for name in TRACKED_METRICS:
        print(f"##autohelix[{name}={report['metrics'][name]:.6g}]")
    print(f"Detailed report: {args.report}")
    if report["cases"] and all(row["error"] for row in report["cases"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
