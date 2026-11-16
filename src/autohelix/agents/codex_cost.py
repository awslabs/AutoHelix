# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Offline, Standard API-equivalent pricing for reported Codex token usage.

Rates verified against https://developers.openai.com/api/docs/pricing and the
individual model pages on 2026-11-06. Keep exact model names: unknown models must
not silently inherit another model's prices. Promotional gpt-5.6-sol rates are
intentionally excluded because they expire on 2026-11-30.
"""


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
