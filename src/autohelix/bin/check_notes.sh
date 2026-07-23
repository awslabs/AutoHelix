#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Stop hook: block the agent from ending its turn until it has written
# meaningful iteration notes. Bounded on three axes so it can never hang:
#   1. notes have enough content   -> allow
#   2. retry cap reached           -> allow (graceful give-up)
#   3. iteration deadline is near  -> allow (don't fight the time-kill)
#
# Reads these env vars (set by the harness when require_notes is enabled):
#   AUTOHELIX_NOTES_FILE        path to this iteration's notes file (iter-N.md)
#   AUTOHELIX_NOTES_MIN_CHARS   non-whitespace char floor (default 50)
#   AUTOHELIX_NOTES_MAX_RETRIES max times to block (default 3)
#   AUTOHELIX_NOTES_RETRY_FILE  counter file (tracks blocks this iteration)
#   AUTOHELIX_ITERATION_START   epoch seconds the iteration began (deadline escape)
#   AUTOHELIX_ITERATION_BUDGET  iteration budget in seconds (deadline escape)

set -u

NOTES="${AUTOHELIX_NOTES_FILE:-}"
MIN_CHARS="${AUTOHELIX_NOTES_MIN_CHARS:-50}"
MAX_RETRIES="${AUTOHELIX_NOTES_MAX_RETRIES:-3}"
RETRY_FILE="${AUTOHELIX_NOTES_RETRY_FILE:-}"

# No notes path configured -> nothing to enforce.
if [ -z "$NOTES" ]; then
  exit 0
fi

# --- Guard 3: deadline escape. If <60s of iteration budget remains, allow stop
# so the notes gate never blocks the agent straight into a time-kill. Computed
# from the same env vars time_left.sh uses. ---
START="${AUTOHELIX_ITERATION_START:-}"
BUDGET="${AUTOHELIX_ITERATION_BUDGET:-}"
if [ -n "$START" ] && [ -n "$BUDGET" ]; then
  now=$(date +%s)
  remaining=$((BUDGET - (now - START)))
  if [ "$remaining" -lt 60 ] 2>/dev/null; then
    exit 0
  fi
fi

# --- Check note content: count non-whitespace characters. ---
if [ -f "$NOTES" ]; then
  chars=$(tr -d '[:space:]' < "$NOTES" | wc -c | tr -d ' ')
else
  chars=0
fi

# Guard 1: enough content -> allow stop.
if [ "$chars" -ge "$MIN_CHARS" ] 2>/dev/null; then
  exit 0
fi

# --- Read + bump the retry counter. ---
count=0
if [ -n "$RETRY_FILE" ] && [ -f "$RETRY_FILE" ]; then
  count=$(tr -dc '0-9' < "$RETRY_FILE")
  count=${count:-0}
fi

# Guard 2: retry cap reached -> allow stop (don't loop forever).
if [ "$count" -ge "$MAX_RETRIES" ] 2>/dev/null; then
  exit 0
fi

# Otherwise: block, and ask the agent to write its notes. Escalate the wording
# but never dictate WHAT to write — the agent decides the content.
count=$((count + 1))
if [ -n "$RETRY_FILE" ]; then
  printf '%s' "$count" > "$RETRY_FILE"
fi

if [ "$chars" -eq 0 ]; then
  reason="Before finishing, write your iteration notes to ${NOTES}. The next iteration starts fresh and can only build on what you record here — what to capture (what you tried, the results, what to try next) is entirely up to you. Write a few substantial paragraphs, then finish."
elif [ "$count" -ge "$MAX_RETRIES" ]; then
  reason="Final reminder: your notes at ${NOTES} are still very brief. Expand them into at least a solid paragraph now — this is the last chance before the iteration ends."
else
  reason="Your notes at ${NOTES} look very brief. Please expand them into at least a solid paragraph capturing what mattered this iteration before finishing."
fi

# Emit the block decision as JSON on stdout.
printf '{"decision":"block","reason":%s}\n' "$(printf '%s' "$reason" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
exit 0
