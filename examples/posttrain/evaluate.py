#!/usr/bin/env python3
"""Score a model on GSM8K with inspect_ai + vLLM, and emit the accuracy.

The prompt format is fixed by the base model's chat template, read from the
Hugging Face cache (--chat-template-model), not from the fine-tuned model, so the
agent's model is always evaluated against the same format. The template isn't
kept in this folder: a file there would have to be committed (every phase runs in
a copy holding committed files only), and this repository doesn't ship it.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from inspect_ai import eval as inspect_eval
from inspect_ai.util._display import init_display_type
from transformers import AutoTokenizer

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
        "--chat-template-model",
        default="Qwen/Qwen3-4B-Base",
        help="Model whose chat template fixes the eval prompt format (the base "
        "model; read from the Hugging Face cache, never downloaded).",
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


def chat_template(model: str) -> str:
    """The chat template `model` ships, from the Hugging Face cache."""
    try:
        tokenizer = AutoTokenizer.from_pretrained(model, local_files_only=True)
    except OSError as e:
        raise SystemExit(
            f"{model} is not in the Hugging Face cache — pre-cache it (README, SETUP): {e}"
        )
    if not tokenizer.chat_template:
        raise SystemExit(f"{model} ships no chat_template")
    return tokenizer.chat_template


def main() -> None:
    args = parse_args()
    init_display_type("plain")

    # A scratch file outside the working folder, for the vLLM server to read.
    with tempfile.TemporaryDirectory() as scratch:
        template = Path(scratch) / "chat_template.jinja"
        template.write_text(chat_template(args.chat_template_model), encoding="utf-8")
        evaluate(args, template)


def evaluate(args: argparse.Namespace, template: Path) -> None:
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
