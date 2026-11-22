"""Fixtures for the scorer tests. They need a CUDA GPU, upstream's environment (torch,
kernels, the cached FA3 build, the data and tokenizer that prepare.py downloads) and
AR_UPSTREAM, the folder holding upstream's train.py and prepare.py. Elsewhere (the
repo's own test run) they aren't collected."""

import importlib.util
import math
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
UPSTREAM = os.environ.get("AR_UPSTREAM")

if importlib.util.find_spec("torch") is None or not UPSTREAM:
    collect_ignore_glob = ["test_*.py"]
else:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    if not torch.cuda.is_available():
        collect_ignore_glob = ["test_*.py"]
    sys.path.insert(0, str(HERE.parent / "template"))


@pytest.fixture(scope="session")
def score():
    from ar import score

    score.fa3 = score.load_fa3().flash_attn_interface
    return score


@pytest.fixture(scope="session")
def prepare():
    """Upstream's prepare.py, imported from AR_UPSTREAM."""
    spec = importlib.util.spec_from_file_location("prepare", Path(UPSTREAM) / "prepare.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def gpt(score):
    """Upstream's default model (train.py's model section and hyperparameters), random
    weights. Its init zeroes the output projections, which would hide attention from the
    probes, so they get small random values too."""
    path = Path(UPSTREAM) / "train.py"
    source = path.read_text()
    section = source[source.index("# GPT Model"):source.index("# Optimizer (MuonAdamW")]
    hp = dict(re.findall(r"^(ASPECT_RATIO|HEAD_DIM|DEPTH|WINDOW_PATTERN)\s*=\s*(\S+)", source, re.M))
    namespace = dict(torch=torch, nn=nn, F=F, math=math, dataclass=dataclass, asdict=asdict, fa3=score.fa3)
    exec(compile(section, str(path), "exec"), namespace)
    depth, head_dim = int(hp["DEPTH"]), int(hp["HEAD_DIM"])
    dim = (depth * int(hp["ASPECT_RATIO"]) + head_dim - 1) // head_dim * head_dim
    config = namespace["GPTConfig"](sequence_len=score.SEQ_LEN, vocab_size=score.VOCAB, n_layer=depth,
                                    n_head=dim // head_dim, n_kv_head=dim // head_dim, n_embd=dim,
                                    window_pattern=hp["WINDOW_PATTERN"].strip("\"'"))
    torch.manual_seed(0)
    with torch.device("meta"):
        model = namespace["GPT"](config)
    model.to_empty(device="cuda")
    model.init_weights()
    with torch.no_grad():
        for block in model.transformer.h:
            block.attn.c_proj.weight.normal_(0, 0.02)
            block.mlp.c_proj.weight.normal_(0, 0.02)
        model.lm_head.weight.normal_(0, 0.02)
    return model.eval()


@pytest.fixture(scope="session")
def baseline(gpt, tmp_path_factory):
    """The random-init upstream model, exported by ar.export.save."""
    from ar import export

    return export.save(gpt, str(tmp_path_factory.mktemp("export") / "model.pt2"))


@pytest.fixture(scope="session")
def tokenizer(score):
    return score.load_tokenizer(score.CACHE / "tokenizer")


@pytest.fixture(scope="session")
def rows(score, tokenizer):
    """16 upstream eval batches of 128 rows: 128 of the scorer's batches, so its 2048
    causality cuts reach every position."""
    return score.make_rows(tokenizer, score.CACHE / "data" / score.VAL_SHARD, 16 * 128)


@pytest.fixture(scope="session")
def nbytes(score, tokenizer):
    return score.token_bytes(tokenizer)
