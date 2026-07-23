#!/usr/bin/env python3
"""Extract the base model's chat template into templates/qwen3.jinja.

Run once during setup. The template defines how GSM8K questions are formatted
at eval time; keeping it in a frozen file (rather than reading the fine-tuned
model's own template) fixes the eval prompt format across iterations.

    python setup_template.py                       # Qwen/Qwen3-4B-Base
    python setup_template.py --model <hf-model-id>
"""
from __future__ import annotations

import argparse
from pathlib import Path

from transformers import AutoTokenizer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="Qwen/Qwen3-4B-Base")
    parser.add_argument("--out", default="templates/qwen3.jinja")
    args = parser.parse_args()

    template = AutoTokenizer.from_pretrained(args.model).chat_template
    if not template:
        raise SystemExit(f"{args.model} ships no chat_template")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(template, encoding="utf-8")
    print(f"wrote {out} from {args.model}")


if __name__ == "__main__":
    main()
