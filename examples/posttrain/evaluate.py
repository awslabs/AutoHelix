#!/usr/bin/env python3
"""Score a model on GSM8K with inspect_ai + vLLM, and emit the accuracy.

The prompt format is fixed by a chat template read from --templates-dir (see
setup_template.py, which extracts it from the base model during setup), so the
agent's fine-tuned model is always evaluated against the same fixed format.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from inspect_ai import eval as inspect_eval
from inspect_ai.util._display import init_display_type

import inspect_evals.gsm8k  # noqa: F401  (import registers the gsm8k task)

TASK = "inspect_evals/gsm8k"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score a model on GSM8K.")
    parser.add_argument(
        "--model-path",
        default="final_model",
        help="Model to evaluate (HF directory or identifier).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=150,
        help="Number of GSM8K samples to evaluate (-1 for all).",
    )
    parser.add_argument("--json-output-file", default=None)
    parser.add_argument(
        "--templates-dir",
        default="templates/",
        help="Directory holding qwen3.jinja (the fixed eval chat template).",
    )
    parser.add_argument(
        "--fewshot",
        type=int,
        default=0,
        help="Number of in-context examples (pinned so the score doesn't depend "
        "on the eval library's default, which has changed across versions).",
    )
    parser.add_argument("--max-connections", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=4000)
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.9)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    init_display_type("plain")

    template = Path(args.templates_dir) / "qwen3.jinja"
    if not template.is_file():
        raise SystemExit(
            f"chat template not found at {template} — run setup_template.py first"
        )

    model_args = {
        "gpu_memory_utilization": args.gpu_memory_utilization,
        "chat_template": str(template),
    }

    eval_kwargs = {}
    if args.limit is not None and args.limit != -1:
        eval_kwargs["limit"] = args.limit

    eval_out = inspect_eval(
        TASK,
        model=f"vllm/{args.model_path}",
        model_args=model_args,
        task_args={"fewshot": args.fewshot},
        score_display=False,
        log_realtime=False,
        log_format="json",
        max_tokens=args.max_tokens,
        max_connections=args.max_connections,
        **eval_kwargs,
    )

    if not eval_out or eval_out[0].results is None:
        raise SystemExit(
            "eval produced no results (the vLLM model server likely failed to "
            "start) — check the inspect_ai log above for the underlying error"
        )

    scores = eval_out[0].results.scores[0].metrics
    print(f"GSM8K accuracy: {scores['accuracy'].value}")

    if args.json_output_file:
        metrics = {k: v.value for k, v in scores.items()}
        with open(args.json_output_file, "w") as f:
            json.dump(metrics, f, indent=2)


if __name__ == "__main__":
    main()
