"""The trusted scorer: what it rejects, and that it agrees with upstream's evaluate_bpb."""

import io
import json
import zipfile

import pytest
import torch
import torch.nn as nn
import torch.nn.functional as F

from ar import export


def rewrite(src, dst, edit):
    """Copy the archive at src to dst, passing every member through edit(name, data)."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_STORED) as zout:
        for info in zin.infolist():
            zout.writestr(info, edit(info.filename, zin.read(info)))
    return dst


def edit_json(src, dst, member, change):
    """Rewrite one JSON member of the archive with change(obj)."""
    def edit(name, data):
        if not name.endswith(member):
            return data
        obj = json.loads(data)
        change(obj)
        return json.dumps(obj).encode()
    return rewrite(src, dst, edit)


def subgraph(program):
    """The graph inside the top-level wrap_with_autocast node."""
    (node,) = program["graph_module"]["graph"]["nodes"]
    return next(a["arg"]["as_graph"]["graph"] for a in node["inputs"] if "as_graph" in a["arg"])


def rejected(score, path, *words):
    with pytest.raises(score.Rejected) as info:
        score.validate(path)
    for word in words:
        assert word in str(info.value)
    return str(info.value)


class Toy(nn.Module):
    """A small model under the export contract; `mode` makes it break one rule."""

    def __init__(self, mode=None):
        super().__init__()
        self.mode = mode
        self.embed = nn.Embedding(8192, 32)
        self.out = nn.Linear(32, 8192)
        self.register_buffer("calls", torch.zeros(()))

    def forward(self, idx):
        h = self.embed(idx)
        if self.mode == "peek":  # sees the next token
            h = h + self.embed(idx.roll(-1, dims=1))
        if self.mode == "leak":  # position 1000 alone sees token 1001
            at = (torch.arange(idx.shape[1], device=idx.device) == 1000).float()[None, :, None]
            h = h + at * self.embed(idx.roll(-1, dims=1))
        if self.mode == "dropout":  # ordinary code, off in eval mode
            h = F.dropout(h, 0.1, training=self.training) + F.alpha_dropout(h, 0.1, training=self.training)
        if self.mode == "tensor":  # a constant made in forward
            h = h * torch.tensor(0.5, device=idx.device)
        if self.mode == "mix":  # sees the other rows (symmetrically: permuting doesn't show it)
            h = h + h.mean(dim=0, keepdim=True)
        if self.mode == "state":  # carries a counter from call to call
            self.calls.add_(1)
            h = h + self.calls
        return self.out(h).float()


def toy(mode, tmp_path):
    return export.save(Toy(mode).cuda(), str(tmp_path / f"{mode}.pt2"))


def test_baseline_passes(score, baseline, rows, nbytes):
    result = score.evaluate(score.load(score.validate(baseline)), rows[:4 * score.BATCH], nbytes, seed=1)
    print("probes", result["probes"])
    assert result["probes"]["causality_cuts"] == 4 * score.BATCH
    assert 0 < result["val_bpb"] < 10


def test_bpb_matches_upstream(score, prepare, gpt, baseline, rows, nbytes, monkeypatch):
    assert torch.equal(nbytes, prepare.get_token_bytes().to(torch.int32))
    monkeypatch.setattr(prepare, "EVAL_TOKENS", len(rows) * score.SEQ_LEN)
    with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16):
        upstream = prepare.evaluate_bpb(gpt, prepare.Tokenizer.from_directory(), 128)
    ours = score.evaluate(score.load(score.validate(baseline)), rows, nbytes, seed=1)["val_bpb"]
    print("upstream", upstream, "scorer", ours)
    assert abs(ours - upstream) < 1e-5


def test_planted_pickle_in_sample_inputs(score, baseline, tmp_path):
    marker = tmp_path / "ran"

    class Evil:
        def __reduce__(self):
            return (open, (str(marker), "w"))

    buffer = io.BytesIO()
    torch.save(Evil(), buffer)
    path = rewrite(baseline, tmp_path / "evil.pt2",
                   lambda name, data: buffer.getvalue() if name.endswith("sample_inputs/model.pt") else data)
    rejected(score, path, "sample_inputs", "must be empty")
    assert not marker.exists()


def test_pickled_weight(score, baseline, tmp_path):
    def change(config):
        next(iter(config["config"].values()))["use_pickle"] = True
    path = edit_json(baseline, tmp_path / "bad.pt2", "model_weights_config.json", change)
    rejected(score, path, "pickled payload")


def test_guards_code(score, baseline, tmp_path):
    path = edit_json(baseline, tmp_path / "bad.pt2", "models/model.json",
                     lambda p: p.update(guards_code=["__import__('os').system('true')"]))
    rejected(score, path, "guards_code")


def test_symbolic_expression(score, baseline, tmp_path):
    def change(program):
        meta = next(iter(subgraph(program)["tensor_values"].values()))
        meta["sizes"][0] = {"as_expr": {"expr_str": "__import__('os').getpid()", "hint": {"as_int": 16}}}
    path = edit_json(baseline, tmp_path / "bad.pt2", "models/model.json", change)
    rejected(score, path, "symbolic value")


def test_operator_outside_allowlist(score, baseline, tmp_path):
    def change(program):
        subgraph(program)["nodes"][0]["target"] = "torch.load"
    path = edit_json(baseline, tmp_path / "bad.pt2", "models/model.json", change)
    rejected(score, path, "'torch.load'", "is not allowed")


def test_operator_argument(score, baseline, tmp_path):
    def change(program):
        subgraph(program)["nodes"][0]["inputs"][1]["arg"] = {"as_operator": "torch.load"}
    path = edit_json(baseline, tmp_path / "bad.pt2", "models/model.json", change)
    rejected(score, path, "as_operator", "not allowed")


@pytest.mark.parametrize("op, allowed", [
    (("linear", "default"), True), (("gelu", "default"), True), (("softmax", "int"), True),
    (("layer_norm", "default"), True), (("scaled_dot_product_attention", "default"), True),
    (("index", "Tensor"), True), (("einsum", "default"), True),
    (("add_", "Tensor"), False), (("add", "out"), False), (("rand", "default"), False),
    (("native_dropout", "default"), True), (("bernoulli", "p"), False), (("empty", "memory_format"), False),
    (("from_file", "default"), False), (("set", "source_Storage"), False),
    (("item", "default"), False), (("no_such_op", "default"), False),
])
def test_aten_rule(score, op, allowed):
    assert (score.aten_problem(*op) is None) == allowed, score.aten_problem(*op)


def test_attention_dropout(score, baseline, tmp_path):
    """SDPA is allowed, but only without dropout."""
    def change(program):
        node = subgraph(program)["nodes"][0]
        node["target"] = "torch.ops.aten.scaled_dot_product_attention.default"
        node["inputs"] = [{"name": "dropout_p", "arg": {"as_float": 0.1}, "kind": 1}]
    path = edit_json(baseline, tmp_path / "bad.pt2", "models/model.json", change)
    rejected(score, path, "dropout_p must be 0")


def test_mutated_buffer(score, baseline, tmp_path):
    """Export keeps a buffer update as an in-place op, which save() reports from the
    scorer's validation; a signature can also declare one."""
    with pytest.raises(export.ExportError, match="the scorer rejects it: .*aten.add_.Tensor writes to its inputs"):
        toy("state", tmp_path)

    def change(program):
        program["graph_module"]["signature"]["output_specs"].append(
            {"buffer_mutation": {"arg": {"name": "getitem"}, "buffer_name": "m.cos"}})
    path = edit_json(baseline, tmp_path / "bad.pt2", "models/model.json", change)
    rejected(score, path, "buffer_mutation 'm.cos' not allowed")


def test_non_causal_model(score, rows, nbytes, tmp_path):
    model = score.load(score.validate(toy("peek", tmp_path)))
    with pytest.raises(score.Rejected, match="causality probe failed"):
        score.evaluate(model, rows[:4 * score.BATCH], nbytes, seed=1)


def test_rows_mixed(score, rows, nbytes, tmp_path):
    model = score.load(score.validate(toy("mix", tmp_path)))
    with pytest.raises(score.Rejected, match="row independence probe failed"):
        score.evaluate(model, rows[:4 * score.BATCH], nbytes, seed=1)


def test_leak_at_one_position(score, rows, nbytes, tmp_path):
    """Every position is cut at least once over 128 batches."""
    model = score.load(score.validate(toy("leak", tmp_path)))
    with pytest.raises(score.Rejected, match="causality probe failed.*FFT"):
        score.evaluate(model, rows, nbytes, seed=2)


@pytest.mark.parametrize("mode", ["dropout", "tensor"])
def test_ordinary_code_accepted(score, rows, nbytes, tmp_path, mode):
    model = score.load(score.validate(toy(mode, tmp_path)))
    score.evaluate(model, rows[:4 * score.BATCH], nbytes, seed=1)


def test_dropout_in_training_mode_rejected(score, baseline, tmp_path):
    def change(program):
        node = subgraph(program)["nodes"][0]
        node["target"] = "torch.ops.aten.dropout.default"
        node["inputs"] = [{"name": "train", "arg": {"as_bool": True}, "kind": 1}]
    path = edit_json(baseline, tmp_path / "bad.pt2", "models/model.json", change)
    rejected(score, path, "train must be False")


def test_malformed_signature(score, baseline, tmp_path):
    path = edit_json(baseline, tmp_path / "bad.pt2", "models/model.json",
                     lambda p: p["graph_module"]["signature"].update(input_specs=[]))
    rejected(score, path)


def test_malformed_autocast(score, baseline, tmp_path):
    def change(program):
        (node,) = program["graph_module"]["graph"]["nodes"]
        node["inputs"][1]["arg"] = {"as_scalar_type": [13]}
    path = edit_json(baseline, tmp_path / "bad.pt2", "models/model.json", change)
    rejected(score, path, "malformed archive")


def test_clean_toy_passes(score, rows, nbytes, tmp_path):
    model = score.load(score.validate(toy(None, tmp_path)))
    probes = score.evaluate(model, rows[:4 * score.BATCH], nbytes, seed=1)["probes"]
    assert probes["causality"] == probes["row_independence"] == probes["repeat"] == 0
