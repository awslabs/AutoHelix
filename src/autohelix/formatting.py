# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared display formatting helpers."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable

from autohelix.costs import summarize_costs


_ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|.)", re.S)
_BIDI = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))
SUMMARY_LIMIT = 200  # characters of the agent's one-line summary kept


def _printable(text: str, keep: str) -> str:
    text = _ESCAPES.sub("", text)
    return "".join(ch for ch in text if ch in keep
                   or (unicodedata.category(ch) != "Cc" and ord(ch) not in _BIDI))


def clean(text, limit: int | None = None) -> str:
    """Agent-written text made safe to show as one line (console, prompt, commit
    message): terminal escape sequences, control characters (newlines included) and
    bidirectional overrides removed, whitespace collapsed; cut to `limit` characters."""
    text = " ".join(_printable(str(text or ""), "\t\n\r\v\f").split())
    if limit is not None and len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text


def clean_block(text) -> str:
    """Multi-line agent or command output for a prompt: lines kept, other control
    characters and escape sequences removed."""
    return _printable(str(text or "").replace("\r\n", "\n"), "\n\t")


def format_duration(seconds: float) -> str:
    """Compact, at most 7 characters: 48s, 1m 02s, 1h 01m."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def format_cost(cost: float | None, precision: int = 2) -> str:
    """Display recorded or calculated dollars without treating absence as zero."""
    return f"${cost:.{precision}f}" if cost is not None else "cost unavailable"


def format_cap(cost: float) -> str:
    """A dollar cap as given: cents (`$5.00`), else up to four significant digits, in
    plain decimals (`$0.015`, `$0.000000001`)."""
    if abs(cost * 100 - round(cost * 100)) < 1e-9:
        return f"${cost:.2f}"
    decimals = min(12, max(2, 3 - math.floor(math.log10(cost))))
    return "$" + f"{cost:.{decimals}f}".rstrip("0").rstrip(".")


def format_total_cost(usages: Iterable[dict]) -> str:
    """Describe a total as incomplete when some agent calls have no known cost."""
    total = summarize_costs(usages)
    if total.cost_usd is None:
        return "cost unavailable"
    text = format_cost(total.cost_usd)
    return f"{text} (incomplete)" if total.incomplete else text
