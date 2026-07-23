# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Iteration-time budget helpers.

Also the canonical home for duration parsing. `parse_duration_string` holds
the one grammar shared by `budget.time` (via config.parse_duration) and
`budget.iteration_time` (via this module's parse_duration); the two public
wrappers differ only in their zero/None policy.
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


def parse_duration(value: str | int | float | None) -> int | None:
    """Parse an optional duration to seconds; None/empty/zero mean "unset".

    Accepts None/empty as unset, numbers as seconds, and strings like "30m",
    "1h", "1h30m", "2h15m30s", or "45s".
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"Duration must be a string or number, got bool ({value})")
    if isinstance(value, (int, float)):
        if not (value == value) or value in (float("inf"), float("-inf")):
            raise ValueError(f"Duration must be finite, got {value}")
        secs = int(value)
        return secs if secs > 0 else None

    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s) or None
    total = parse_duration_string(s)
    return total if total > 0 else None


def format_duration(seconds: int) -> str:
    """Format a duration in seconds as a compact string."""
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
