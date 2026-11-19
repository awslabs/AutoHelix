"""Trusted scorer: bits per byte of an exported model (ar/export.py) on a parquet shard.

    python ar/score.py MODEL.pt2 [--data PARQUET] [--tokenizer DIR] [--tokens N] [--seed N]

Prints one JSON object on stdout: `val_bpb`, the tokens and bytes scored, the probe
results and the timings. A rejected model prints `{"error": ...}` and exits 1.

1. Validate the archive as plain data (zip members and JSON), before torch deserializes
   anything: `torch.export.load` on an archive someone else wrote can run code (pickled
   sample inputs and payloads, sympy expressions, guard code, operator names resolved by
   getattr on `torch`). See `validate`.
2. Write a copy holding only what was validated, load it, run it on batches built as
   upstream's `evaluate_bpb` builds them, and compute the cross-entropy here: the model
   only maps token ids to logits.
3. Probes, any failure rejecting the model: causality, row independence, and repeat
   (the same batch scored first and last).

The data, tokenizer and FA3 cache must be the operator's own copies: the tokenizer is a
pickle (prepare.py writes it that way).
"""

import argparse
import functools
import json
import math
import os
import pickle
import re
import secrets
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import pyarrow.parquet as pq
import torch
import torch.nn.functional as F
from torch._export.serde.schema import SCHEMA_VERSION, ScalarType

# The export contract (ar/export.py).
BATCH, SEQ_LEN, VOCAB = 16, 2048, 8192
# Upstream prepare.py's evaluation.
EVAL_TOKENS = 40 * 524288
CACHE = Path.home() / ".cache" / "autoresearch"
VAL_SHARD = "shard_06542.parquet"
SPECIAL_TOKENS = [f"<|reserved_{i}|>" for i in range(4)]
BOS_TOKEN = SPECIAL_TOKENS[0]
FA3_REPO = "varunneal/flash-attention-3"
FA3_REVISION = "de87b9b5af06dd9984df595bef90b2eba44b181a"
FA3_TARGET = "torch.ops.flash_attn_3._flash_attn_forward.default"
AUTOCAST_TARGET = "torch.ops.higher_order.wrap_with_autocast"

# Probes (see `evaluate`): ROW_PAIRS random batch pairs for row independence. TOLERANCE is
# the largest logit difference a probe accepts; the baseline's is exactly 0.
ROW_PAIRS = 4
TOLERANCE = 1e-4

torch.set_float32_matmul_precision("high")  # as train.py sets it, so fp32 matmuls match


class Rejected(Exception):
    """The archive or the model breaks the contract; the message says how."""


# ---------------------------------------------------------------------------
# Validation: the archive as plain data
# ---------------------------------------------------------------------------

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")  # graph value names become Python names
FQN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*\Z")  # and attribute paths
ROOT = re.compile(r"[A-Za-z0-9_-]+\Z")
ATEN = re.compile(r"torch\.ops\.aten\.([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\Z")
FIXED_MEMBERS = {
    "archive_format", "archive_version", "byteorder", ".data/version", ".data/serialization_id",
    "models/model.json", "data/sample_inputs/model.pt",
    "data/weights/model_weights_config.json", "data/constants/model_constants_config.json",
}
PAYLOAD = re.compile(r"data/(weights/weight|constants/tensor)_\d+\Z")
DTYPES = {t.value for t in ScalarType if t != ScalarType.UNKNOWN}
FLOATS = {ScalarType.FLOAT, ScalarType.BFLOAT16, ScalarType.HALF}
STRIDED = 7  # schema.Layout.Strided
TENSOR_KINDS = {"as_tensor", "as_tensors", "as_optional_tensors"}
VALUE_KINDS = {"as_none", "as_int", "as_ints", "as_float", "as_floats", "as_bool", "as_bools",
               "as_string", "as_strings", "as_scalar_type", "as_memory_format", "as_layout"}
# What export.py's wrapper serializes to: forward(idx) with one tensor out.
IN_SPEC = ('[1, {"type": "builtins.tuple", "context": "null", "children_spec": [{"type": '
           '"builtins.tuple", "context": "null", "children_spec": [{"type": null, "context": '
           'null, "children_spec": []}]}, {"type": "builtins.dict", "context": "[]", '
           '"children_spec": []}]}]')
OUT_SPEC = '[1, {"type": null, "context": null, "children_spec": []}]'
ROOT_CALL = {"fqn": "", "signature": {"inputs": [], "outputs": [], "in_spec": IN_SPEC,
                                      "out_spec": OUT_SPEC, "forward_arg_names": ["idx"]}}
INPUT_SPECS = {"parameter": "parameter_name", "buffer": "buffer_name",
               "tensor_constant": "tensor_constant_name"}


def validate(path):
    """`check_archive`, with any unexpected error a rejection too."""
    try:
        return check_archive(path)
    except Rejected:
        raise
    except Exception as e:
        raise Rejected(f"malformed archive: {type(e).__name__}: {e}")


def check_archive(path):
    """Check the archive without deserializing anything; return its validated parts.

    Fails closed: anything not known to be safe is rejected, with the reason.
    - zip members: an allowlist, stored uncompressed, under one root folder;
    - no pickles: `sample_inputs` empty (export.py writes none), no `use_pickle` payloads,
      no custom objects;
    - `guards_code == []`, `range_constraints == {}`, no symbolic values anywhere: every
      size, stride and offset a plain int;
    - the signature: parameters, buffers and constants in, then idx int64 [16, 2048];
      float logits [16, 2048, 8192] out and nothing else (no buffer, parameter or input
      mutation, so no state carries over between calls);
    - every graph node an allowed operator (`check_node`), recursing into the subgraphs
      of `wrap_with_autocast`, the one higher-order op allowed;
    - names that end up in generated Python code are plain identifiers or attribute paths,
      and every other argument is plain JSON data.
    Node metadata (stack traces, module paths) is dropped, so the copy loaded is exactly
    the validated structure.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            members = read_members(archive)
    except (OSError, zipfile.BadZipFile) as e:
        raise Rejected(f"not a readable zip archive: {e}")
    if members["data/sample_inputs/model.pt"]:
        raise Rejected("data/sample_inputs/model.pt must be empty: sample inputs are pickled "
                       "(export with ar.export.save, which writes none)")
    for name, expected in [("archive_format", b"pt2"), ("archive_version", b"0"), ("byteorder", b"little")]:
        if members[name] != expected:
            raise Rejected(f"{name} is {members[name][:20]!r}, expected {expected!r}")
    program = parse_json(members, "models/model.json")
    weights = parse_json(members, "data/weights/model_weights_config.json")
    constants = parse_json(members, "data/constants/model_constants_config.json")
    payloads = {name for name in members if PAYLOAD.match(name)}
    used = check_payloads(weights, "weights", members) | check_payloads(constants, "constants", members)
    if payloads != used:
        raise Rejected(f"payload files not named by a config: {sorted(payloads - used)[:5]}")
    check_program(program)
    return {"program": program, "weights": weights, "constants": constants,
            "payloads": {name: members[name] for name in payloads}}


def read_members(archive):
    """name (without the root folder) -> bytes, for allowlisted members only."""
    infos = archive.infolist()
    roots = {info.filename.split("/", 1)[0] for info in infos}
    if len(roots) != 1 or not ROOT.match(next(iter(roots))):
        raise Rejected(f"expected one root folder in the archive, found {sorted(roots)[:5]}")
    members = {}
    for info in infos:
        name = info.filename.partition("/")[2]
        if name not in FIXED_MEMBERS and not PAYLOAD.match(name):
            raise Rejected(f"unexpected archive member {info.filename!r}")
        if name in members:
            raise Rejected(f"duplicate archive member {info.filename!r}")
        if info.compress_type != zipfile.ZIP_STORED or info.flag_bits & 1:
            raise Rejected(f"archive member {info.filename!r} is compressed or encrypted")
        members[name] = archive.read(info)
    if FIXED_MEMBERS - set(members):
        raise Rejected(f"archive members missing: {sorted(FIXED_MEMBERS - set(members))}")
    return members


def parse_json(members, name):
    def no_duplicates(pairs):
        if len(pairs) != len(dict(pairs)):
            raise Rejected(f"{name}: duplicate JSON keys")
        return dict(pairs)

    try:
        return json.loads(members[name], object_pairs_hook=no_duplicates)
    except ValueError as e:
        raise Rejected(f"{name} isn't valid JSON: {e}")


def keys(obj, required, optional=(), where=""):
    """`obj` is a dict with all of `required` and nothing outside `required` + `optional`."""
    if not (isinstance(obj, dict) and set(required) <= set(obj) <= set(required) | set(optional)):
        raise Rejected(f"{where}: expected an object with keys {sorted(required)}, got {str(obj)[:100]}")


def one_key(obj, where):
    if not (isinstance(obj, dict) and len(obj) == 1):
        raise Rejected(f"{where}: expected a one-key object, got {str(obj)[:100]}")
    return next(iter(obj.items()))


def name_like(value, pattern, where):
    if not (isinstance(value, str) and pattern.match(value)):
        raise Rejected(f"{where}: {str(value)[:80]!r} isn't a plain name")
    return value


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def check_payloads(config, kind, members):
    """Weights and constants: raw tensor bytes only, never pickles. Returns the files used."""
    keys(config, ["config"], where=f"{kind} config")
    used = set()
    for fqn, meta in config["config"].items():
        where = f"{kind} {fqn!r}"
        if not isinstance(meta, dict) or meta.get("use_pickle") is not False:
            raise Rejected(f"{where} is a pickled payload (use_pickle); only plain tensors are allowed")
        keys(meta, ["path_name", "is_param", "use_pickle", "tensor_meta"], where=where)
        name_like(fqn, FQN, where)
        file = f"data/{kind}/{meta['path_name']}"
        if not (isinstance(meta["path_name"], str) and PAYLOAD.match(file) and file in members):
            raise Rejected(f"{where}: {str(meta['path_name'])[:80]!r} isn't a {kind} file of the archive")
        if not isinstance(meta["is_param"], bool):
            raise Rejected(f"{where}: is_param isn't a bool")
        check_tensor_meta(meta["tensor_meta"], where)
        used.add(file)
    return used


def check_tensor_meta(meta, where):
    keys(meta, ["dtype", "sizes", "requires_grad", "device", "strides", "storage_offset", "layout"],
         where=where)
    if not (meta["dtype"] in DTYPES and meta["layout"] == STRIDED and isinstance(meta["requires_grad"], bool)
            and isinstance(meta["sizes"], list) and isinstance(meta["strides"], list)):
        raise Rejected(f"{where}: only dense tensors of a known dtype are allowed")
    for value in meta["sizes"] + meta["strides"] + [meta["storage_offset"]]:
        fixed_int(value, where)
    check_device(meta["device"], where)


def fixed_int(value, where):
    """A SymInt that is a plain int: symbolic expressions go to sympy, which evals them."""
    if not (isinstance(value, dict) and list(value) == ["as_int"] and is_int(value["as_int"])):
        raise Rejected(f"{where}: symbolic value {str(value)[:80]}; only fixed shapes are allowed")
    return value["as_int"]


def check_device(device, where):
    keys(device, ["type"], ["index"], where=f"{where} device")
    if device["type"] not in ("cuda", "cpu") or not (device.get("index") is None or is_int(device["index"])):
        raise Rejected(f"{where}: device {str(device)[:80]} not allowed")


def check_program(program):
    keys(program, ["graph_module", "opset_version", "range_constraints", "schema_version",
                   "verifiers", "torch_version", "guards_code"], where="model.json")
    for field, expected, why in [("guards_code", [], "it becomes generated Python code"),
                                 ("range_constraints", {}, "only fixed shapes are allowed"),
                                 ("verifiers", ["TRAINING"], "export with ar.export.save")]:
        if program[field] != expected:
            raise Rejected(f"{field} must be {expected!r}: {why}")
    version, opset = program["schema_version"], program["opset_version"]
    keys(version, ["major", "minor"], where="schema_version")
    if version["major"] != SCHEMA_VERSION[0]:
        raise Rejected(f"schema version {version}, expected major {SCHEMA_VERSION[0]}")
    if not (isinstance(opset, dict) and set(opset) <= {"aten"} and all(map(is_int, opset.values()))
            and isinstance(program["torch_version"], str)):
        raise Rejected("malformed opset_version or torch_version")
    module = program["graph_module"]
    keys(module, ["graph", "signature", "module_call_graph", "metadata", "treespec_namedtuple_fields"],
         where="graph_module")
    if module["metadata"] != {} or module["treespec_namedtuple_fields"] != {}:
        raise Rejected("graph_module metadata and treespec_namedtuple_fields must be empty")
    calls = module["module_call_graph"]
    if not (isinstance(calls, list) and calls and calls[0] == ROOT_CALL):
        raise Rejected("the call signature isn't forward(idx) -> logits (export with ar.export.save)")
    for entry in calls[1:]:
        keys(entry, ["fqn", "signature"], where="module_call_graph entry")
        if entry["fqn"] != "" and not FQN.match(str(entry["fqn"])) or entry["signature"] is not None:
            raise Rejected(f"module_call_graph entry {str(entry)[:80]} not allowed")
    graph = module["graph"]
    if not isinstance(graph, dict):
        raise Rejected("malformed graph")
    idx, logits = check_signature(module["signature"], graph)
    check_graph(graph, "graph")
    check_io(graph["tensor_values"].get(idx), "input idx", [BATCH, SEQ_LEN], {ScalarType.LONG})
    check_io(graph["tensor_values"].get(logits), "output logits", [BATCH, SEQ_LEN, VOCAB], FLOATS)


def check_signature(signature, graph):
    """Parameters, buffers and constants in, then idx; the logits out and nothing else.

    Returns the graph names of idx and of the logits."""
    keys(signature, ["input_specs", "output_specs"], where="signature")
    inputs, outputs = signature["input_specs"], signature["output_specs"]
    for spec in outputs:
        kind, body = one_key(spec, "output spec")
        if kind != "user_output":
            what = next((body[k] for k in ("buffer_name", "parameter_name", "user_input_name")
                         if isinstance(body, dict) and k in body), "")
            raise Rejected(f"output of kind {kind} {what!r} not allowed: the model may not mutate "
                           "buffers, parameters or inputs (no state may carry over between calls)")
    if len(outputs) != 1:
        raise Rejected(f"the program must return one tensor, the logits; it returns {len(outputs)}")
    names = []
    for i, spec in enumerate(inputs):
        kind, body = one_key(spec, "input spec")
        if kind == "user_input" and i == len(inputs) - 1:
            keys(body, ["arg"], where="user input")
            names.append(tensor_ref(body["arg"], "user input"))
        elif kind in INPUT_SPECS and i < len(inputs) - 1:
            keys(body, ["arg", INPUT_SPECS[kind]], ["persistent"], where=f"{kind} input")
            name_like(body[INPUT_SPECS[kind]], FQN, f"{kind} input")
            keys(body["arg"], ["name"], where=f"{kind} input")
            names.append(body["arg"]["name"])
        else:
            raise Rejected(f"input {kind} not allowed: only parameters, buffers and constants, then idx")
    keys(outputs[0]["user_output"], ["arg"], where="user output")
    out = tensor_ref(outputs[0]["user_output"]["arg"], "user output")
    graph_inputs = [tensor_ref(a, "graph input") for a in graph.get("inputs", [])]
    graph_outputs = [tensor_ref(a, "graph output") for a in graph.get("outputs", [])]
    if graph_inputs != names or graph_outputs != [out]:
        raise Rejected("the signature doesn't match the graph's inputs and outputs")
    return names[-1], out


def check_io(meta, what, shape, dtypes):
    sizes = [fixed_int(size, what) for size in meta["sizes"]] if meta else None
    if sizes != shape or meta["dtype"] not in dtypes:
        raise Rejected(f"{what} must be {'/'.join(ScalarType(d).name.lower() for d in sorted(dtypes))} "
                       f"{shape}, got {meta and ScalarType(meta['dtype']).name.lower()} {sizes}")


def check_graph(graph, where):
    keys(graph, ["inputs", "outputs", "nodes", "tensor_values", "sym_int_values", "sym_bool_values",
                 "sym_float_values", "is_single_tensor_return", "custom_obj_values"], where=where)
    for field in ("sym_int_values", "sym_bool_values", "sym_float_values", "custom_obj_values"):
        if graph[field] != {}:
            raise Rejected(f"{where}: {field} must be empty: only fixed shapes, no custom objects")
    if not (isinstance(graph["is_single_tensor_return"], bool) and isinstance(graph["tensor_values"], dict)
            and isinstance(graph["nodes"], list)):
        raise Rejected(f"{where}: malformed graph")
    for name, meta in graph["tensor_values"].items():
        check_tensor_meta(meta, f"{where} value {name_like(name, IDENT, where)}")
    for arg in graph["inputs"] + graph["outputs"]:
        tensor_ref(arg, f"{where} input/output")
    for node in graph["nodes"]:
        check_node(node, where)


def tensor_ref(arg, where):
    """{"as_tensor": {"name": NAME}} -> NAME"""
    kind, ref = one_key(arg, where)
    if kind != "as_tensor":
        raise Rejected(f"{where}: expected a tensor, got {kind}")
    keys(ref, ["name"], where=where)
    return name_like(ref["name"], IDENT, where)


def check_value(kind, value, where):
    """Tensors are referenced by plain names, a device is a device, and every other value
    is plain JSON data: no objects, so nothing the loader would resolve or parse."""
    if kind == "as_tensor":
        tensor_ref({kind: value}, where)
    elif kind in ("as_tensors", "as_optional_tensors") and isinstance(value, list):
        for item in value:
            if kind == "as_tensors":
                tensor_ref({"as_tensor": item}, where)
            elif one_key(item, where)[0] != "as_none":
                tensor_ref(item, where)
    elif kind == "as_device":
        check_device(value, where)
    elif not (kind in VALUE_KINDS and all(v is None or isinstance(v, (bool, int, float, str))
                                          for v in (value if isinstance(value, list) else [value]))):
        raise Rejected(f"{where}: value of kind {kind} not allowed: {str(value)[:80]}")


def check_node(node, where):
    """One graph node: its operator, its arguments and outputs.

    Operators are matched by their serialized name, never resolved through `torch`:
    - aten ops: `aten_problem` decides from the op's registered schema;
    - FA3's forward, `flash_attn_3._flash_attn_forward`, with q, k, v as its only tensors
      (its schema hides in-place writes through `out` and the KV-cache arguments);
    - `wrap_with_autocast`, whose subgraph is checked the same way.
    """
    keys(node, ["target", "inputs", "outputs", "metadata"], ["is_hop_single_tensor_return"],
         where=f"{where} node")
    target = node["target"]
    where = f"{where} node {str(target)[:100]!r}"
    node["metadata"] = {}  # dropped: the loader resolves operator names in it, keeps stack traces
    if (node.get("is_hop_single_tensor_return") not in (None, True, False)
            or not isinstance(node["outputs"], list)):
        raise Rejected(f"{where}: malformed node")
    for out in node["outputs"]:
        kind, value = one_key(out, f"{where} output")
        if kind not in ("as_tensor", "as_tensors", "as_none"):
            raise Rejected(f"{where}: output of kind {kind} not allowed")
        check_value(kind, value, f"{where} output")
    args = []
    for named in node["inputs"] if isinstance(node["inputs"], list) else [None]:
        keys(named, ["name", "arg", "kind"], where=where)
        if named["name"] != "":
            name_like(named["name"], IDENT, f"{where} argument")
        if named["kind"] not in (1, 2):
            raise Rejected(f"{where}: argument kind {named['kind']!r} not allowed")
        args.append((named["name"], *one_key(named["arg"], where)))
    if target == AUTOCAST_TARGET:
        return check_autocast(args, where)
    if target == FA3_TARGET:
        extra = [name for name, kind, _ in args if kind in TENSOR_KINDS and name not in ("q", "k", "v")]
        if extra:
            raise Rejected(f"{where}: {extra} must be None (FA3 may take only q, k and v: "
                           "no output buffer, no KV cache)")
    else:
        match = ATEN.match(target) if isinstance(target, str) else None
        problem = aten_problem(*match.groups()) if match else (
            "is not allowed: only aten operators, FlashAttention-3's forward and autocast "
            "regions may appear in the graph")
        if problem:
            raise Rejected(f"{where}: operator {problem}")
        if match.group(1) in DROPOUT_SWITCHES:
            switch, off, off_by_default = DROPOUT_SWITCHES[match.group(1)]
            values = [value for name, _, value in args if name == switch]
            if values != [off] and (values or not off_by_default):
                raise Rejected(f"{where}: {switch} must be {off} (no randomness: dropout is off "
                               "in the exported model, which runs in eval mode)")
    for name, kind, value in args:
        check_value(kind, value, f"{where} argument {name!r}")


def check_autocast(args, where):
    """wrap_with_autocast(device, dtype, enabled, cache_enabled, subgraph, *tensors)."""
    kinds = [kind for _, kind, _ in args]
    values = [value for _, _, value in args]
    if (kinds[:5] != ["as_string", "as_scalar_type", "as_bool", "as_bool", "as_graph"]
            or any(name for name, _, _ in args) or values[0] != "cuda" or values[1] not in DTYPES
            or not all(isinstance(v, bool) for v in values[2:4])):
        raise Rejected(f"{where}: autocast arguments {str(list(zip(kinds, values))[:4])[:200]} not allowed")
    for kind, value in zip(kinds[5:], values[5:]):
        tensor_ref({kind: value}, where)
    keys(values[4], ["name", "graph"], where=where)
    check_graph(values[4]["graph"], f"{where} > {name_like(values[4]['name'], IDENT, where)}")


# The aten rule. An aten op is allowed when its registered schema says it is a pure
# function of its arguments: nothing written in place (no `!` alias annotations, so no
# `out=` variants), not random (by its tags), tensors returned, and only plain value
# types taken (tensors, numbers, bools, strings, dtypes, devices, layouts, memory formats;
# no generators, storages, streams or objects). That admits ordinary model code:
# activations, norms, softmax, SDPA, matmuls, indexing, reductions... Ops with
# data-dependent output shapes would need symbolic sizes, which the graph check rejects.
# Some ops pass the schema test without being pure, so they are named: ones that return
# uninitialized memory (`empty*`, `resize*`: it can hold data the scorer freed),
# `from_file` (reads a file), the functional `set` (rebinds storage), `_copy_from` (writes
# to `dst`), `as_strided*` and `_reshape_alias` (raw views of a storage).
# `_assert_tensor_metadata` returns nothing and only checks a tensor; export inserts it.
# `detach_` writes only autograd metadata (`torch.tensor(...)` in forward exports it).
# Dropout ops are tagged random: allowed with their switch off (`train` False, SDPA's
# `dropout_p` 0).
DENIED_WORDS = ("empty", "resize", "as_strided", "_reshape_alias", "from_file", "_copy_from", "storage")
ALWAYS_ALLOWED = {"_assert_tensor_metadata", "detach_"}
# op -> (argument, its value when off, off when the argument is absent)
DROPOUT_SWITCHES = {"scaled_dot_product_attention": ("dropout_p", 0, True),
                    **{op: ("train", False, False)
                       for op in ("dropout", "feature_dropout", "alpha_dropout", "native_dropout")}}
VALUE_TYPES = {"Tensor", "int", "float", "bool", "number", "str", "ScalarType", "Layout", "Device",
               "MemoryFormat"}


@functools.cache
def aten_problem(name, overload):
    """None if torch.ops.aten.<name>.<overload> is allowed, else why not."""
    label = f"aten.{name}.{overload}"
    if name in ALWAYS_ALLOWED:
        return None
    if "empty" in name:
        return (f"{label} is not allowed: it returns uninitialized memory. Activation "
                "checkpointing (torch.utils.checkpoint) puts it in the graph: skip it when "
                "not training")
    if name == "set" or any(word in name for word in DENIED_WORDS):
        return f"{label} is not allowed (uninitialized memory, files or raw storage)"
    try:
        schema = torch._C._get_schema(f"aten::{name}", "" if overload == "default" else overload)
    except RuntimeError:
        return f"{label} is not a registered aten operator"
    if schema.is_mutable:
        return f"{label} writes to its inputs"
    random = torch.Tag.nondeterministic_seeded in getattr(getattr(torch.ops.aten, name), overload).tags
    if random and name not in DROPOUT_SWITCHES:
        return f"{label} is random"
    if not schema.returns or any(base_type(r) != "Tensor" for r in schema.returns):
        return f"{label} returns non-tensor values"
    for arg in schema.arguments:
        if base_type(arg) not in VALUE_TYPES:
            return f"{label} takes a {arg.real_type} argument"
    return None


def base_type(arg):
    """List[Optional[Tensor]] -> Tensor (SymInt shows as int, Scalar as number)."""
    text = str(arg.real_type)
    while match := re.fullmatch(r"(?:Optional|List)\[(.*)\]", text):
        text = match.group(1)
    return text


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_fa3():
    """Register FA3's op from the local HF cache at the pinned revision (no network); return it."""
    import kernels

    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    snapshot = hub / ("models--" + FA3_REPO.replace("/", "--")) / "snapshots" / FA3_REVISION
    if not snapshot.is_dir():
        sys.exit(f"score.py: FlashAttention-3 isn't cached at {snapshot}")
    return kernels.get_local_kernel(snapshot, "flash_attention_3")


def load(parts):
    """Load a copy of only what `validate` returned: a callable `idx -> logits`."""
    from torch.export.pt2_archive._package import PT2ArchiveWriter

    with tempfile.TemporaryDirectory() as folder:
        clean = os.path.join(folder, "model.pt2")
        with PT2ArchiveWriter(clean) as archive:
            archive.write_string("archive_format", "pt2")
            archive.write_string("archive_version", "0")
            archive.write_string("byteorder", "little")
            archive.write_string("models/model.json", json.dumps(parts["program"]))
            archive.write_string("data/weights/model_weights_config.json", json.dumps(parts["weights"]))
            archive.write_string("data/constants/model_constants_config.json", json.dumps(parts["constants"]))
            archive.write_bytes("data/sample_inputs/model.pt", b"")
            for name, data in parts["payloads"].items():
                archive.write_bytes(name, data)
        try:
            return torch.export.load(clean).module()
        except Exception as e:
            raise Rejected(f"the validated program doesn't load: {type(e).__name__}: {str(e)[:500]}")


# ---------------------------------------------------------------------------
# Data: upstream prepare.py's validation batches, for any shard
# ---------------------------------------------------------------------------

def load_tokenizer(folder):
    """The tiktoken encoding prepare.py trained (a pickle: the operator's own copy)."""
    with open(Path(folder) / "tokenizer.pkl", "rb") as f:
        return pickle.load(f)


def token_bytes(enc):
    """UTF-8 bytes per token as prepare.py counts them; special tokens count 0."""
    counts = []
    for token in range(enc.n_vocab):
        text = enc.decode([token])
        counts.append(0 if text in SPECIAL_TOKENS else len(text.encode("utf-8")))
    return torch.tensor(counts, dtype=torch.int32)


def documents(parquet, batch_size=128):
    """Batches of the shard's texts, starting over at the end, as upstream reads them."""
    shard = pq.ParquetFile(parquet)
    while True:
        for group in range(shard.num_row_groups):
            texts = shard.read_row_group(group).column("text").to_pylist()
            for i in range(0, len(texts), batch_size):
                yield texts[i:i + batch_size]


def make_rows(enc, parquet, n_rows, buffer_size=1000):
    """Rows of SEQ_LEN + 1 tokens, packed exactly as upstream's make_dataloader packs them.

    Every row starts at a document start (BOS first); it takes the longest buffered
    document that fits, and when none fits, the start of the shortest one. Rows don't
    depend on the batch size, so batches of 16 give the same tokens as upstream's 128.
    """
    bos = enc.encode_single_token(BOS_TOKEN)
    batches = documents(parquet)
    buffer = []
    rows = torch.empty((n_rows, SEQ_LEN + 1), dtype=torch.long)
    for r in range(n_rows):
        row = []
        while len(row) < SEQ_LEN + 1:
            while len(buffer) < buffer_size:
                buffer.extend([bos] + ids for ids in enc.encode_ordinary_batch(next(batches), num_threads=8))
            remaining = SEQ_LEN + 1 - len(row)
            best, best_len = -1, 0
            for i, doc in enumerate(buffer):
                if best_len < len(doc) <= remaining:
                    best, best_len = i, len(doc)
            if best >= 0:
                row.extend(buffer.pop(best))
            else:
                shortest = min(range(len(buffer)), key=lambda i: len(buffer[i]))
                row.extend(buffer.pop(shortest)[:remaining])
        rows[r] = torch.tensor(row)
    return rows


# ---------------------------------------------------------------------------
# Probes and scoring
# ---------------------------------------------------------------------------

def inputs(rows, b):
    """Batch b's token ids: a fresh tensor on the GPU, sharing storage with nothing."""
    return rows[b * BATCH:(b + 1) * BATCH, :-1].contiguous().cuda()


def run(model, idx):
    logits = model(idx)
    if not (isinstance(logits, torch.Tensor) and tuple(logits.shape) == (BATCH, SEQ_LEN, VOCAB)
            and logits.is_floating_point()):
        raise Rejected(f"the model returned {getattr(logits, 'shape', type(logits))}, "
                       f"expected float logits {[BATCH, SEQ_LEN, VOCAB]}")
    return logits.float()


def check_probe(name, diff, hint=""):
    if not diff <= TOLERANCE:
        raise Rejected(f"{name} probe failed: logits changed by {diff:.3g} (tolerance {TOLERANCE:g}){hint}")
    return diff


CAUSALITY_HINT = (". Only tokens after the cut changed: position t may use only tokens 0..t. Operations "
                  "over the whole sequence (an FFT, a global norm or pooling) break this, and so can "
                  "bf16 rounding that depends on later positions")


def causality(model, idx, logits, cut, rng):
    """Replace each row's tokens after its cut: the logits at and before it must not change."""
    after = (torch.arange(SEQ_LEN) > cut).cuda()
    noise = torch.randint(0, VOCAB, (BATCH, SEQ_LEN), generator=rng).cuda()
    changed = run(model, torch.where(after, noise, idx))
    diff = torch.where(after[..., None], 0.0, (changed - logits).abs()).amax().item()
    return check_probe("causality", diff, CAUSALITY_HINT)


def row_independence(model, rows, a, b, rng):
    """Half the rows from batch a, half from batch b, shuffled: each row's logits must
    follow it. Permuting alone misses symmetric mixing (adding the batch mean), so the
    other rows change too."""
    out_a, out_b = run(model, inputs(rows, a)), run(model, inputs(rows, b))
    half = BATCH // 2
    perm = torch.randperm(BATCH, generator=rng).cuda()
    mixed = torch.cat([inputs(rows, a)[:half], inputs(rows, b)[half:]])[perm]
    expected = torch.cat([out_a[:half], out_b[half:]])[perm]
    return check_probe("row independence", (run(model, mixed) - expected).abs().amax().item())


@torch.no_grad()
def evaluate(model, rows, nbytes, seed):
    """Bits per byte over every batch, with the probes (all positioned by the secret seed):
    - row independence on ROW_PAIRS random pairs of batches, first;
    - causality on every batch, one cut per row, against the scoring pass's logits; the
      cuts cycle through random permutations of the positions, so each is cut as often;
    - repeat: a random batch scored first and last.
    Upstream's rows are public, so no probe may sit on rows known in advance. The "score"
    time includes the causality forwards (one per batch)."""
    rng = torch.Generator().manual_seed(seed)
    n = len(rows) // BATCH
    start = time.time()
    repeat_batch = int(torch.randint(n, (1,), generator=rng))
    reference = run(model, inputs(rows, repeat_batch))
    pairs = torch.randperm(n, generator=rng)[:2 * min(ROW_PAIRS, n // 2)].view(-1, 2).tolist()
    # rows first: a model mixing rows also fails causality, since the other rows change too
    probes = {"seed": seed, "tolerance": TOLERANCE,
              "row_independence": max(row_independence(model, rows, a, b, rng) for a, b in pairs),
              "row_pairs": len(pairs), "causality": 0.0, "causality_cuts": n * BATCH}
    probe_seconds = time.time() - start

    start = time.time()
    laps = -(-n * BATCH // (SEQ_LEN - 1))
    cuts = torch.cat([torch.randperm(SEQ_LEN - 1, generator=rng) for _ in range(laps)])  # last kept
    cuts = cuts[:n * BATCH].view(n, BATCH, 1)
    nbytes = nbytes.cuda()
    total_nats = torch.zeros((), dtype=torch.float64, device="cuda")
    total_bytes = torch.zeros((), dtype=torch.int64, device="cuda")
    bad = torch.zeros((), dtype=torch.int64, device="cuda")
    for b in range(n):
        idx = inputs(rows, b)
        logits = run(model, idx)
        targets = rows[b * BATCH:(b + 1) * BATCH, 1:].reshape(-1).cuda()
        loss = F.cross_entropy(logits.view(-1, VOCAB), targets, reduction="none")
        counts = nbytes[targets]
        bad += (~torch.isfinite(loss)).sum()
        total_nats += torch.where(counts > 0, loss.double(), 0.0).sum()
        total_bytes += counts.sum()
        probes["causality"] = max(probes["causality"], causality(model, idx, logits, cuts[b], rng))
    if bad.item():
        raise Rejected(f"the model's logits give {bad.item()} non-finite losses")
    bpb = total_nats.item() / (math.log(2) * total_bytes.item())
    score_seconds = time.time() - start

    start = time.time()
    again = run(model, inputs(rows, repeat_batch))
    probes["repeat"] = check_probe("repeat", (again - reference).abs().amax().item())
    probe_seconds += time.time() - start
    return {"val_bpb": bpb, "tokens": rows[:, 1:].numel(), "bytes": total_bytes.item(),
            "probes": probes, "seconds": {"probes": probe_seconds, "score": score_seconds}}


def main():
    parser = argparse.ArgumentParser(description="Score an exported model: bits per byte on a shard.")
    parser.add_argument("model", help="the .pt2 file ar.export.save wrote")
    parser.add_argument("--data", default=str(CACHE / "data" / VAL_SHARD),
                        help="parquet shard to score on (default: upstream's validation shard)")
    parser.add_argument("--tokenizer", default=str(CACHE / "tokenizer"), help="folder holding tokenizer.pkl")
    parser.add_argument("--tokens", type=int, default=EVAL_TOKENS,
                        help=f"tokens to score, in whole batches (default: upstream's {EVAL_TOKENS})")
    parser.add_argument("--seed", type=int, help="seed for the probes (default: random)")
    args = parser.parse_args()
    n_rows = args.tokens // (BATCH * SEQ_LEN) * BATCH
    if n_rows < 2 * BATCH:
        parser.error(f"--tokens must cover at least two batches ({2 * BATCH * SEQ_LEN})")
    seed = secrets.randbits(32) if args.seed is None else args.seed

    seconds = {}
    started = last = time.time()

    def lap(name):
        nonlocal last
        seconds[name], last = time.time() - last, time.time()

    try:
        parts = validate(args.model)
        lap("validate")
        load_fa3()
        model = load(parts)
        del parts
        lap("load")
        enc = load_tokenizer(args.tokenizer)
        rows, nbytes = make_rows(enc, args.data, n_rows), token_bytes(enc)
        lap("data")
        result = evaluate(model, rows, nbytes, seed)
    except Rejected as e:
        print(json.dumps({"error": f"{args.model} rejected: {e}"}))
        return 1
    seconds.update(result["seconds"], total=time.time() - started)
    print(json.dumps({**result, "data": args.data, "seconds": seconds}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
