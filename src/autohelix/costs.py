# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Aggregate recorded costs without treating unavailable amounts as zero."""

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class CostSummary:
    cost_usd: float | None
    incomplete: bool


def summarize_costs(usages: Iterable[dict]) -> CostSummary:
    """Sum known charges, retaining whether any attempted call has unknown cost."""
    costs = [usage.get("cost_usd") for usage in usages]
    known = [cost for cost in costs if cost is not None]
    return CostSummary(sum(known) if known else None, len(known) != len(costs))
