#!/usr/bin/env python3
"""Validate result structure and reproduce the current research experiment."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
RESULT_PATH = HERE / "results" / "latest.json"


def load_result(path: Path) -> dict[str, Any]:
    result = json.loads(path.read_text())
    if not isinstance(result, dict):
        raise AssertionError("result must be a JSON object")
    if result.get("schema_version") != 1:
        raise AssertionError("schema_version must remain 1")
    if not isinstance(result.get("seed"), int):
        raise AssertionError("result must record an integer seed")
    experiments = result.get("experiments")
    if not isinstance(experiments, list) or not experiments:
        raise AssertionError("result must contain non-empty experiments")

    required = {
        "distribution",
        "sample_size",
        "method",
        "trials",
        "coverage",
        "mean_width",
    }
    for index, record in enumerate(experiments):
        missing = required - record.keys()
        if missing:
            raise AssertionError(
                f"experiment {index} missing fields: {sorted(missing)}"
            )
        if not 0 <= record["coverage"] <= 1:
            raise AssertionError(f"experiment {index} has invalid coverage")
        if record["sample_size"] < 2 or record["trials"] < 1:
            raise AssertionError(f"experiment {index} has invalid counts")
        if record["mean_width"] <= 0:
            raise AssertionError(f"experiment {index} has invalid width")
    return result


def check_reproducible(expected: dict[str, Any]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        reproduced_path = Path(tmp) / "reproduced.json"
        subprocess.run(
            [
                sys.executable,
                str(HERE / "experiment.py"),
                "--quick",
                "--output",
                str(reproduced_path),
            ],
            cwd=HERE,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        reproduced = load_result(reproduced_path)
    if reproduced != expected:
        raise AssertionError(
            "results/latest.json does not match a fresh deterministic quick run"
        )


def main() -> int:
    expected = load_result(RESULT_PATH)
    check_reproducible(expected)
    print(
        f"Validated {len(expected['experiments'])} reproducible experiment records"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
