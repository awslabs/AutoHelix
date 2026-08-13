"""Laptop-runnable tests for the KernelBench example scaffolding."""

import ast
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from autohelix.config import Config


ROOT = Path(__file__).resolve().parents[1]
KERNELBENCH = ROOT / "examples" / "kernelbench"
TEMPLATES = KERNELBENCH / "templates"


def _load_setup_module():
    spec = importlib.util.spec_from_file_location(
        "kernelbench_setup", KERNELBENCH / "setup.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_screen():
    source = (TEMPLATES / "check_solution.py.tmpl").read_text().format()
    namespace = {
        "__file__": str(TEMPLATES / "check_solution.py"),
        "__name__": "kernelbench_check_solution",
    }
    exec(compile(source, "check_solution.py", "exec"), namespace)
    return namespace["scan"]


def _load_eval_output_helpers():
    source = (TEMPLATES / "eval_solution.py.tmpl").read_text().format(
        correctness_trials=3
    )
    tree = ast.parse(source)
    wanted = {
        "RewardHackDetected",
        "check_lazy_outputs",
        "check_correctness",
    }
    definitions = [
        node
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in wanted
    ]

    class FakeTensor:
        def __init__(self, value):
            self.value = value
            self.shape = ()

    fake_torch = SimpleNamespace(
        Tensor=FakeTensor,
        allclose=lambda left, right, **kwargs: left.value == right.value,
    )
    namespace = {"torch": fake_torch, "ATOL": 1e-4, "RTOL": 1e-4}
    module = ast.fix_missing_locations(ast.Module(body=definitions, type_ignores=[]))
    exec(compile(module, "eval_solution.py", "exec"), namespace)
    return namespace, FakeTensor


def test_static_screen_allows_honest_kernel_tools_and_local_ref_time():
    scan = _load_screen()
    source = """
import torch
import triton
from torch.utils.cpp_extension import load_inline

ref_time = 1.0
compiled = torch.compile(lambda x: x)
extension = load_inline(name="kernel", cpp_sources="")

try:
    pass
except triton.runtime.errors.OutOfResources:
    raise
"""
    assert scan(source) == []


def test_static_screen_rejects_direct_and_indirect_harness_access():
    scan = _load_screen()
    cases = [
        "import sys\nsys._getframe()",
        'import sys\ngetattr(sys, "_" + "getframe")()',
        "import sys as runtime\nruntime._getframe()",
        "import inspect as introspect\nintrospect.currentframe()",
        'import inspect as introspect\ngetattr(introspect, "current" + "frame")()',
        "import ctypes",
        'import importlib\nimportlib.import_module("ct" + "ypes")',
        'from importlib import import_module as load\nload("ctypes.util")',
        '__import__("ctypes")',
        "harness.ref_time = 0",
    ]
    for source in cases:
        assert scan(source), source


def test_static_screen_rejects_network_imports():
    scan = _load_screen()
    for source in (
        "import urllib.request",
        "from urllib.request import urlretrieve",
        "import requests",
        "import httpx",
        "import socket",
    ):
        assert scan(source), source


def test_eval_template_matches_official_metric_and_protects_harness():
    source = (TEMPLATES / "eval_solution.py.tmpl").read_text().format(
        correctness_trials=3
    )
    compile(source, "eval_solution.py", "exec")
    assert "and not name.startswith('_')" not in source
    assert "integrity_checker = check_harness_integrity" in source
    assert source.count("integrity_checker(_harness_snapshot)") >= 4
    assert source.count('print("mean_speedup: 0.0")') == 0
    assert "WARMUP_RUNS = 3" in source
    assert "TIMING_RUNS = 100" in source
    assert "DISCARD_FIRST = 1" in source
    assert "ATOL = 1e-4" in source
    assert "RTOL = 1e-4" in source
    assert "PRECISION = torch.float32" in source
    assert "clear_l2_cache(device)" in source
    assert 'return float(f"{sum(times) / len(times):.3g}")' in source
    assert "timing_inputs = _build_timing_inputs()" in source
    assert "_refresh_inputs" not in source
    assert source.index("_trust_reference_rng_functions()", source.index("ref_model =")) < (
        source.index("# Load the solution after reference timing")
    )
    assert "Post-timing cache probe failed" in source
    assert "sys.modules['inspect'] = None" not in source


def test_eval_rejects_non_tensor_replacement_for_tensor_output():
    namespace, FakeTensor = _load_eval_output_helpers()
    ok, error = namespace["check_correctness"](FakeTensor(1), None)

    assert not ok
    assert "type mismatch FakeTensor vs NoneType" in error


def test_eval_recursively_validates_structured_outputs():
    namespace, FakeTensor = _load_eval_output_helpers()
    check = namespace["check_correctness"]
    reference = {
        "logits": FakeTensor(1),
        "state": (FakeTensor(2), [None, 3]),
    }

    assert check(reference, reference) == (True, "ok")

    ok, error = check(
        reference,
        {
            "logits": FakeTensor(1),
            "state": (FakeTensor(2), [None, 4]),
        },
    )
    assert not ok
    assert "output['state'][1][1]: value mismatch 3 vs 4" == error

    class LazyTensor(FakeTensor):
        pass

    with pytest.raises(
        namespace["RewardHackDetected"],
        match=r"Lazy evaluation detected at output\['state'\]\[0\]",
    ):
        namespace["check_lazy_outputs"]({"state": [LazyTensor(2)]})


def test_autohelix_template_splits_correctness_from_timing():
    rendered = (TEMPLATES / "autohelix.yaml.tmpl").read_text().format(
        problem_name="L1_test",
        evaluation_timeout=1234,
        iterations=7,
        agent_model="us.anthropic.claude-opus-4-8",
    )
    raw = yaml.safe_load(rendered)
    config = Config.from_dict(raw)

    assert [constraint.command for constraint in config.constraints] == [
        "python check_solution.py",
        "python eval_solution.py --check-only",
    ]
    assert config.constraints[1].timeout == 1234
    assert len(config.observables) == 1
    assert config.observables[0].command == "python eval_solution.py"
    assert config.observables[0].timeout == 1234
    assert config.max_iterations == 7
    assert config.iteration_time_seconds is None
    assert config.agent.type == "claude"
    assert config.agent.model == "us.anthropic.claude-opus-4-8"
    assert config.acceptance.metric_gates[0].metric == "mean_speedup"
    assert "You may read and run the evaluation scripts" in config.goal
    assert "do not inspect" not in config.goal


def test_setup_scaffolds_and_smoke_checks_without_cuda(tmp_path, monkeypatch):
    setup_module = _load_setup_module()
    data_dir = tmp_path / "data"
    problem_dir = data_dir / "L1" / "1_Test"
    problem_dir.mkdir(parents=True)
    problem_dir.joinpath("reference.py").write_text(
        """\
import torch
import torch.nn as nn

class Model(nn.Module):
    def forward(self, x):
        return x + 1

def get_inputs():
    return [torch.randn(4)]

def get_init_inputs():
    return []
"""
    )

    monkeypatch.setattr(setup_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(setup_module, "git_init", lambda directory: None)

    output_dir = tmp_path / "scaffold"
    assert setup_module.setup("L1_1_Test", output_dir)

    expected = {
        "autohelix.yaml",
        "check_solution.py",
        "eval_solution.py",
        "solution.py",
        "problem/reference.py",
    }
    assert expected <= {
        str(path.relative_to(output_dir))
        for path in output_dir.rglob("*")
        if path.is_file()
    }
    assert "examples/kernelbench/setup.py" in setup_module.__doc__


def test_setup_cli_creates_clean_git_project(tmp_path):
    suite_dir = tmp_path / "kernelbench"
    shutil.copytree(KERNELBENCH, suite_dir)
    problem_dir = suite_dir / "data" / "L1" / "1_Test"
    problem_dir.mkdir(parents=True)
    problem_dir.joinpath("reference.py").write_text(
        """\
import torch
import torch.nn as nn

class Model(nn.Module):
    def forward(self, x):
        return x + 1

def get_inputs():
    return [torch.randn(4)]

def get_init_inputs():
    return []
"""
    )

    output_dir = tmp_path / "project"
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "AutoHelix Test",
            "GIT_AUTHOR_EMAIL": "autohelix-test@example.com",
            "GIT_COMMITTER_NAME": "AutoHelix Test",
            "GIT_COMMITTER_EMAIL": "autohelix-test@example.com",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            str(suite_dir / "setup.py"),
            "L1_1_Test",
            "--dir",
            str(output_dir),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert output_dir.joinpath(".git").is_dir()
    assert subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=output_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout == ""
    assert Config.from_dict(
        yaml.safe_load(output_dir.joinpath("autohelix.yaml").read_text())
    ).observables[0].command == "python eval_solution.py"


def test_download_repairs_a_partially_populated_level(tmp_path, monkeypatch):
    setup_module = _load_setup_module()
    data_dir = tmp_path / "data"
    partial_level = data_dir / "L1"
    partial_level.mkdir(parents=True)
    partial_level.joinpath("stale").mkdir()

    dataset = [
        {
            "name": "1_Test_.py",
            "problem_id": 1,
            "code": "class Model: pass\n",
        }
    ]
    monkeypatch.setitem(
        sys.modules,
        "datasets",
        SimpleNamespace(load_dataset=lambda *args, **kwargs: dataset),
    )
    monkeypatch.setattr(setup_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(
        setup_module,
        "load_config",
        lambda: {
            "source": {
                "hf_dataset": "test/kernelbench",
                "levels": [1],
            }
        },
    )

    assert setup_module.download([1])
    assert data_dir.joinpath("L1", "1_Test", "reference.py").read_text() == (
        "class Model: pass\n"
    )
