# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Offline, Standard API-equivalent pricing for reported Codex token usage.

Rates verified against https://developers.openai.com/api/docs/pricing and the
individual model pages on 2026-11-06. Keep exact model names: unknown models must
not silently inherit another model's prices. Promotional gpt-5.6-sol rates are
intentionally excluded because they expire on 2026-11-30.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID


# USD per million tokens: fresh input, cached input, output, 30m cache writes.
_RATES = {
    "gpt-6.1-sol": (2.0, 0.10, 10.0, 2.50),
    "gpt-6-sol": (2.0, 0.20, 10.0, 2.50),
    "gpt-6.1-terra": (1.0, 0.05, 5.0, 1.25),
    "gpt-6-astra": (10.0, 1.0, 50.0, 12.50),
    "gpt-6-luna": (0.10, 0.01, 0.50, 0.125),
    "gpt-5.6-luna": (0.20, 0.02, 1.20, 0.25),
    "gpt-5.5": (5.0, 0.50, 30.0, None),
    "gpt-5.4": (2.50, 0.25, 15.0, None),
    "gpt-5.4-mini": (0.75, 0.075, 4.50, None),
    "gpt-5.3-codex": (1.75, 0.175, 14.0, None),
    "gpt-5.2-codex": (1.75, 0.175, 14.0, None),
    "gpt-5-codex": (1.25, 0.125, 10.0, None),
    "gpt-5.1-codex": (1.25, 0.125, 10.0, None),
    "gpt-5.1-codex-max": (1.25, 0.125, 10.0, None),
    "gpt-5.1-codex-mini": (0.25, 0.025, 2.0, None),
}
TOKEN_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens",
)
_MAX_SESSION_BYTES = 32 * 1024 * 1024
_MAX_RECORD_BYTES = 2 * 1024 * 1024


def estimate_cost(usage: dict, model: str | None, *, output_includes_reasoning: bool = True) -> dict:
    """Return auditable cost metadata, or an explanation of unavailable cost."""
    canonical = model.removeprefix("openai.") if isinstance(model, str) else None
    rates = _RATES.get(canonical)
    if rates is None:
        return {"cost_unavailable_reason": f"No API pricing for model {model or '<unknown>'}."}
    counts = {key: usage.get(key, 0) for key in TOKEN_FIELDS}
    if (
        any(key not in usage for key in ("input_tokens", "cached_input_tokens", "output_tokens"))
        or (not output_includes_reasoning and "reasoning_output_tokens" not in usage)
        or any(type(value) is not int or value < 0 for value in counts.values())
    ):
        return {"cost_unavailable_reason": "Incomplete or invalid Codex token usage."}
    fresh = counts["input_tokens"] - counts["cached_input_tokens"] - counts["cache_write_input_tokens"]
    if fresh < 0:
        return {"cost_unavailable_reason": "Cached token counts exceed total input tokens."}
    input_rate, cached_rate, output_rate, write_rate = rates
    if counts["cache_write_input_tokens"] and write_rate is None:
        return {"cost_unavailable_reason": f"No cache-write pricing for model {model}."}
    output = counts["output_tokens"]
    if output_includes_reasoning and counts["reasoning_output_tokens"] > output:
        return {"cost_unavailable_reason": "Reasoning tokens exceed inclusive output tokens."}
    if not output_includes_reasoning:
        output += counts["reasoning_output_tokens"]
    cost = (
        fresh * input_rate
        + counts["cached_input_tokens"] * cached_rate
        + counts["cache_write_input_tokens"] * (write_rate or 0)
        + output * output_rate
    ) / 1_000_000
    return {
        "cost_usd": cost,
        "cost_source": "calculated",
        "cost_model": model,
        "cost_basis": "openai_standard_api",
        "cost_rates": {
            "input": input_rate, "cached_input": cached_rate,
            "output": output_rate, "cache_write": write_rate,
            "unit": "usd_per_million_tokens", "cache_write_ttl": "30m",
            "verified_at": "2026-11-06",
        },
        "output_includes_reasoning": output_includes_reasoning,
    }


def session_metadata(thread_id: str | None, started_at: datetime) -> dict:
    """Read only this thread's rollout, never user config or other threads.

    UUIDv7 encodes the creation time. Try its UTC date and adjacent dates because
    Codex stores rollouts by local date. Older UUIDs use the process start time.
    Scan all records within fixed byte limits so model changes cannot hide
    between sampled portions. Exceeding a limit makes the estimate unavailable.
    Absence (including --ephemeral) is harmless.
    """
    try:
        uid = UUID(thread_id) if isinstance(thread_id, str) else None
        if uid is None:
            return {}
        created = (
            datetime.fromtimestamp((uid.int >> 80) / 1000, timezone.utc)
            if uid.version == 7 else started_at
        )
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        matches = []
        for offset in (-1, 0, 1):
            directory = home / "sessions" / (created + timedelta(days=offset)).strftime("%Y/%m/%d")
            matches.extend(directory.glob(f"rollout-*-{uid}.jsonl"))
        if len(matches) != 1:
            return {}
        result: dict = {}
        verified = False
        models: set[str] = set()
        conventions: set[bool] = set()
        scanned = 0
        with matches[0].open("rb") as log:
            while line := log.readline(_MAX_RECORD_BYTES + 1):
                scanned += len(line)
                if len(line) > _MAX_RECORD_BYTES or scanned > _MAX_SESSION_BYTES:
                    return {"cost_unavailable_reason": "Codex session metadata exceeds the bounded read limit."}
                try:
                    entry = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    return {"cost_unavailable_reason": "Codex session metadata is incomplete or invalid."}
                if not isinstance(entry, dict):
                    continue
                payload = entry.get("payload")
                if entry.get("type") == "turn_context" and (
                    not isinstance(payload, dict)
                    or not isinstance(payload.get("model"), str)
                    or not payload["model"]
                ):
                    return {"cost_unavailable_reason": "Codex session has an invalid model context."}
                if not isinstance(payload, dict):
                    continue
                if entry.get("type") == "session_meta":
                    verified = payload.get("id") == str(uid)
                    if isinstance(payload.get("model_provider"), str):
                        result["provider"] = payload["model_provider"]
                elif entry.get("type") == "turn_context" and isinstance(payload.get("model"), str):
                    models.add(payload["model"])
                elif entry.get("type") == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info")
                    if isinstance(info, dict) and isinstance(info.get("last_usage_includes_reasoning"), bool):
                        conventions.add(info["last_usage_includes_reasoning"])
        if not verified:
            return {}
        if len(models) == 1:
            result["model"] = models.pop()
        elif models:
            result["cost_unavailable_reason"] = "Codex used multiple models in this thread."
        if len(conventions) == 1:
            result["output_includes_reasoning"] = conventions.pop()
        elif conventions:
            result["cost_unavailable_reason"] = "Codex used mixed reasoning-token conventions."
        return result
    except (OSError, ValueError, OverflowError):
        return {}
