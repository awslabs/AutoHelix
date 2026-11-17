# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Duration helpers.

`parse_duration_string` holds the one duration grammar, used by every config
duration (`agent.timeout`, entry `timeout:`) and `--time` through
config.parse_duration.
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"([0-9]+)\s*([hms])", re.IGNORECASE)
_UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}


def parse_duration_string(s: str) -> int:
    """Parse a duration string like '1h30m' into seconds (order-independent).

    Enforces only the grammar: rejects empty/garbage/leftover text and
    duplicate units, and returns the (possibly zero) total. Callers decide
    whether a zero total is an error or means "unset". Raises ValueError on a
    malformed string.
    """
    s = s.strip()
    if not s:
        raise ValueError("Empty duration string")

    matches = _TOKEN_RE.findall(s)
    if not matches:
        raise ValueError(f"Invalid duration format: '{s}' (use e.g. '1h', '30m', '2h30m')")

    # Verify that the entire string is consumed by valid tokens.
    consumed = _TOKEN_RE.sub("", s).strip()
    if consumed:
        raise ValueError(f"Invalid duration format: '{s}' (unexpected: '{consumed}')")

    # Reject duplicate units (e.g. "1h2h").
    seen_units: set[str] = set()
    total = 0
    for amount, unit in matches:
        u = unit.lower()
        if u in seen_units:
            raise ValueError(f"Duplicate unit '{u}' in duration: '{s}'")
        seen_units.add(u)
        total += int(amount) * _UNIT_SECONDS[u]
    return total


def format_limit(seconds: int) -> str:
    """A limit (`agent.timeout`, `--time`) as the config and flags spell it: `30m`,
    `1h30m`, `12m5s`. Measured times use `formatting.format_duration` (`1m 02s`)."""
    if seconds < 0:
        return "0s"
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return "".join(parts)
