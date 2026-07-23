#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Claude Code hook handler that injects time_left output as additionalContext.

INPUT="$(cat)"

if [ -n "$AUTOHELIX_HOOK_AUDIT_LOG" ]; then
    AUDIT_LOG="$AUTOHELIX_HOOK_AUDIT_LOG"
elif [ -n "$AUTOHELIX_PROJECT" ] && [ -d "$AUTOHELIX_PROJECT/.autohelix" ]; then
    mkdir -p "$AUTOHELIX_PROJECT/.autohelix/logs" 2>/dev/null || true
    AUDIT_LOG="$AUTOHELIX_PROJECT/.autohelix/logs/hook_audit.log"
else
    AUDIT_LOG="/tmp/autohelix_hook_audit.log"
fi
echo "$(date +'%Y-%m-%dT%H:%M:%SZ') hook fired: $INPUT" >> "$AUDIT_LOG" 2>/dev/null || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TIME_LEFT_SCRIPT="${AUTOHELIX_TIME_LEFT_SCRIPT:-$SCRIPT_DIR/time_left.sh}"

if [ ! -x "$TIME_LEFT_SCRIPT" ]; then
    exit 0
fi

export CONTEXT="$("$TIME_LEFT_SCRIPT" 2>/dev/null)"
if [ -z "$CONTEXT" ]; then
    exit 0
fi

export HOOK_INPUT="$INPUT"

python3 - <<'PY'
import json
import os

raw = os.environ.get("HOOK_INPUT", "").strip()
event = "PostToolUse"
if raw:
    try:
        data = json.loads(raw)
        event = data.get("hook_event_name") or data.get("hookEventName") or event
    except json.JSONDecodeError:
        pass

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": event,
        "additionalContext": os.environ.get("CONTEXT", ""),
    }
}))
PY
