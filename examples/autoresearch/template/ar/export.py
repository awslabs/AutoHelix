"""Export the trained model for the trusted scorer (ar/score.py).

train.py calls `ar.export.save(model)` once, at the end of training. The scorer never
runs train.py's code: it runs the exported graph, so the contract is fixed here:

    forward(idx) -> logits
    idx:    int64 [16, 2048], token ids
    logits: float [16, 2048, 8192], one row of next-token logits per position

The model runs under bf16 autocast, as in training, and must be causal (position t may
use only tokens 0..t) with every batch row independent of the others: the scorer checks
both. It computes the cross-entropy itself from the logits.
"""

import os
import time

import torch
import torch.nn as nn

BATCH = 16
SEQ_LEN = 2048
VOCAB = 8192


class ExportError(RuntimeError):
    """The model can't be exported under the contract above."""


class Autocast(nn.Module):
    """forward(idx) -> logits, under bf16 autocast as in training."""

    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, idx):
        with torch.autocast("cuda", dtype=torch.bfloat16):
            return self.model(idx)


def save(model, path=None):
    """Export `model` to `path`, else $AR_EXPORT, else ./model.pt2."""
    path = path or os.environ.get("AR_EXPORT") or "model.pt2"
    start = time.time()
    model = getattr(model, "_orig_mod", model)  # torch.compile's wrapper
    model.eval()
    parameter = next(model.parameters(), None)
    if parameter is None:
        raise ExportError("ar.export.save: the model has no parameters; pass the trained model")
    device = parameter.device
    if device.type != "cuda":
        raise ExportError(f"ar.export.save: the model is on {device}; it must be on the GPU")
    idx = torch.randint(0, VOCAB, (BATCH, SEQ_LEN), device=device)
    wrapped = Autocast(model)
    with torch.no_grad():
        try:
            program = torch.export.export(wrapped, (idx,))
        except Exception as e:
            raise ExportError(
                "ar.export.save: torch.export.export failed. The model's forward(idx) must "
                "be traceable at a fixed shape: no data-dependent Python control flow, no "
                f"calls outside torch. The error was: {type(e).__name__}: {e}"
            ) from e
        logits = program.module()(idx)
    check(logits)
    program.example_inputs = None  # the scorer accepts no pickled sample inputs
    torch.export.save(program, path)
    from . import score

    try:
        score.validate(path)
    except score.Rejected as e:
        raise ExportError(f"ar.export.save: wrote {path}, but the scorer rejects it: {e}") from None
    size = os.path.getsize(path) / 1e6
    print(f"ar.export: wrote {path} ({size:.0f} MB) in {time.time() - start:.1f}s")
    return path


def check(logits):
    """The exported model's output must be finite float logits [16, 2048, 8192]."""
    expected = (BATCH, SEQ_LEN, VOCAB)
    if not isinstance(logits, torch.Tensor):
        raise ExportError(
            f"ar.export.save: forward(idx) must return one logits tensor, got {type(logits).__name__}"
        )
    if tuple(logits.shape) != expected:
        raise ExportError(
            f"ar.export.save: logits have shape {list(logits.shape)}, expected {list(expected)} "
            "(batch, position, vocabulary)"
        )
    if logits.dtype not in (torch.float32, torch.bfloat16, torch.float16):
        raise ExportError(f"ar.export.save: logits must be float32, bfloat16 or float16, got {logits.dtype}")
    if not torch.isfinite(logits).all():
        raise ExportError("ar.export.save: logits contain NaN or infinity")
