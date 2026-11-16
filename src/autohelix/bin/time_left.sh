#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Print remaining wall-clock budget for the current AutoHelix iteration.

START="${AUTOHELIX_ITERATION_START:-}"
BUDGET="${AUTOHELIX_ITERATION_BUDGET:-}"

if [ -z "$START" ] || [ -z "$BUDGET" ]; then
    echo "[AutoHelix time_left] no iteration deadline set"
    exit 0
fi

NOW=$(date +%s)
ELAPSED=$((NOW - START))
REMAINING=$((BUDGET - ELAPSED))
# The harness formats the budget (1h 30m); a bare number of seconds otherwise.
BUDGET_TEXT="${AUTOHELIX_ITERATION_BUDGET_TEXT:-${BUDGET}s}"

# Human-friendly remaining time: hours+minutes, minutes, or seconds.
fmt_remaining() {
    local rem_h=$((REMAINING / 3600))
    local rem_m=$(((REMAINING % 3600) / 60))
    if [ "$rem_h" -gt 0 ]; then
        echo "${rem_h}h ${rem_m}m"
    elif [ "$REMAINING" -ge 60 ]; then
        echo "${rem_m}m"
    else
        echo "${REMAINING}s"
    fi
}

# Report facts, not instructions: how much time is left, and the two things the
# agent can't otherwise know — the deadline is a hard kill, and notes persist
# across it. What to do about it is the agent's call. The number ticking down
# after every tool call is the escalation; it needs no WARNING/URGENT labels.
if [ "$REMAINING" -le 0 ]; then
    echo "[AutoHelix time_left] Deadline passed $((-REMAINING))s ago (of $BUDGET_TEXT budget). The process is hard-killed at the deadline; notes you've written persist to the next iteration."
else
    echo "[AutoHelix time_left] $(fmt_remaining) left of $BUDGET_TEXT budget. The process is hard-killed at the deadline; notes you've written persist to the next iteration."
fi
