# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Usage of model calls made by commands the harness runs ($AUTOHELIX_USAGE_FILE).

The harness gives every command it runs (gate, observe and stop commands)
the path of a per-iteration JSONL file. A tool that calls a model, such as
`autohelix ask`, appends one record when a call starts (cost unknown) and one
when it ends, with the same id. Reading keeps the last record per id, so a call
killed mid-way still counts as a call of unknown cost rather than vanishing.
"""

import json
import math
import os
import uuid
from pathlib import Path
from typing import Any

ENV = "AUTOHELIX_USAGE_FILE"


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
    """The calls recorded in `path`: the last record per id, in first-seen order.

    Costs that aren't a non-negative number are treated as unknown, since the
    file sits where commands (and so agent-written code) can write to it."""
    calls: dict[str, dict[str, Any]] = {}
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
        cost = record.get("cost_usd")
        if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float))
                                 or not math.isfinite(cost) or cost < 0):
            record["cost_usd"] = None
        calls[str(record.get("id") or f"line-{len(calls)}")] = record
    return list(calls.values())
