# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Usage of model calls made by commands the harness runs ($AUTOHELIX_USAGE_FILE).

The harness gives every command it runs (gate, observe and stop commands)
the path of a per-iteration JSONL file. A tool that calls a model, such as
`autohelix ask`, appends one record when a call starts (cost unknown) and one
when it ends, with the same id. Reading keeps the last record per id, so a call
killed mid-way still counts as a call of unknown cost rather than vanishing.

In a call the harness made (`$AUTOHELIX_CALL`, e.g. `gates/2`), a record's
`call` is that name and its `log` the transcript's file name in the call's
folder (`RunPaths.usage_log` finds it); any other record's `log` is a path.
A baseline retry keeps the earlier attempts' records (they count toward its
cost) and appends a marker, `{"superseded": "000-1"}` (where that attempt's
folder went; "" if it wasn't kept): `read_usage` gives each record before it
that `attempt`. Nothing is ever rewritten.
"""

import json
import math
import os
import uuid
from pathlib import Path
from typing import Any

ENV = "AUTOHELIX_USAGE_FILE"
SUPERSEDED = "superseded"


def superseded_marker(attempt: str) -> dict[str, str]:
    """The line appended when a retry keeps an earlier attempt's records: `attempt` is
    the folder name it moved to below `.internal/attempts/`, "" if none."""
    return {SUPERSEDED: attempt}


def new_call_id() -> str:
    return uuid.uuid4().hex


def append_usage(record: dict[str, Any], path: Path | None = None) -> None:
    """Append a usage record to `path`, or to $AUTOHELIX_USAGE_FILE if set."""
    target = path or (Path(os.environ[ENV]) if os.environ.get(ENV) else None)
    if target is None:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a") as f:
        f.write(json.dumps(record) + "\n")


def read_usage(path: Path) -> list[dict[str, Any]]:
    """The calls recorded in `path`: the last record per id, in first-seen order. A
    record before a `superseded` marker gets that marker's `attempt` (the first one
    after it); no other record has one.

    Costs that aren't a non-negative number are treated as unknown, since the
    file sits where commands (and so agent-written code) can write to it."""
    calls: dict[str, dict[str, Any]] = {}
    attempts: dict[str, str] = {}
    since: list[str] = []  # ids recorded since the last marker
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if SUPERSEDED in record and "id" not in record:
            attempt = record[SUPERSEDED]
            for key in since:
                attempts[key] = attempt if isinstance(attempt, str) else ""
            since = []
            continue
        record.pop("attempt", None)  # only a marker says which attempt a record was in
        cost = record.get("cost_usd")
        if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float))
                                 or not math.isfinite(cost) or cost < 0):
            record["cost_usd"] = None
        key = str(record.get("id") or f"line-{len(calls)}")
        calls[key] = record
        attempts.pop(key, None)
        since.append(key)
    for key, attempt in attempts.items():
        calls[key]["attempt"] = attempt
    return list(calls.values())
