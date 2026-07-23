# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared display formatting helpers."""

from __future__ import annotations


def format_delta(value: float, baseline: float) -> str:
    """Format a metric change from baseline as an arrow + percentage or ratio.

    Returns an empty string when there is no meaningful change (zero baseline
    or value unchanged). Large changes are rendered as multipliers ("↑ 12x")
    rather than unwieldy percentages.
    """
    if baseline == 0 or value == baseline:
        return ""

    pct = (value - baseline) / baseline * 100
    arrow = "↓" if pct < 0 else "↑"
    abs_pct = abs(pct)

    # For increases ≥ 10x (≥ 900%), show a multiplier.
    if pct > 0 and abs_pct >= 900:
        mult = abs_pct / 100 + 1
        return f"{arrow} {mult:.0f}x"
    # For decreases to ≤ 1/10th (shrank ≥ 90%), show an inverse multiplier.
    if pct < 0 and 90 <= abs_pct < 100:
        mult = 100 / (100 - abs_pct)
        if mult >= 10:
            return f"{arrow} {mult:.0f}x"
    return f"{arrow} {abs_pct:.1f}%"
