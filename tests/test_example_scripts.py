"""The posttrain and workflow-optimization examples' scripts, with stand-ins for the
GPU stack and the model CLI (no real model calls)."""

import json
import os
import runpy
import subprocess
import sys
import types
from pathlib import Path

import pytest

from autohelix.usage_log import read_usage

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
POSTTRAIN = EXAMPLES / "posttrain"


def committed_copy(tmp_path: Path) -> Path:
    """The posttrain example as a copy holds it after `git add -A`: its files but the
    ignored ones (by its .gitignore, whatever the index says)."""
    names = sorted(str(p.relative_to(POSTTRAIN)) for p in POSTTRAIN.rglob("*") if p.is_file())
    ignored = subprocess.run(["git", "check-ignore", "--no-index", "--stdin", "-z"], cwd=POSTTRAIN,
                             input="\0".join(names), capture_output=True, text=True).stdout
    copy = tmp_path / "posttrain"
    for name in sorted(set(names) - set(ignored.split("\0"))):
        target = copy / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((POSTTRAIN / name).read_bytes())
        target.chmod((POSTTRAIN / name).stat().st_mode)
    return copy


# --- posttrain: the baseline's eval needs no generated file ---

def test_posttrain_commits_no_chat_template(tmp_path):
    """The Qwen chat template must not be committed here (a license question); it
    stays ignored, and nothing in the committed example needs it."""
    copy = committed_copy(tmp_path)
    assert not (copy / "templates").exists()
    assert "templates/qwen3.jinja" in (POSTTRAIN / ".gitignore").read_text().splitlines()
    for path in copy.rglob("*"):
        if path.is_file() and path.name != ".gitignore":
            assert "qwen3.jinja" not in path.read_text(errors="replace"), path


@pytest.fixture
def gpu_stack(monkeypatch):
    """Stand-ins for transformers, inspect_ai and inspect_evals: the tokenizer ships
    `TEMPLATE`, the eval records its arguments and scores 0.5."""
    calls = {}

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(model, **kwargs):
            calls["tokenizer"] = (model, kwargs)
            return types.SimpleNamespace(chat_template="TEMPLATE")

    def inspect_eval(task, model, model_args, **kwargs):
        calls["eval"] = {"task": task, "model": model,
                         "template": Path(model_args["chat_template"]).read_text()}
        metric = types.SimpleNamespace(value=0.5)
        results = types.SimpleNamespace(scores=[types.SimpleNamespace(metrics={"accuracy": metric})])
        return [types.SimpleNamespace(results=results)]

    modules = {
        "transformers": types.SimpleNamespace(AutoTokenizer=AutoTokenizer),
        "inspect_ai": types.SimpleNamespace(eval=inspect_eval),
        "inspect_ai.util": types.ModuleType("inspect_ai.util"),
        "inspect_ai.util._display": types.SimpleNamespace(init_display_type=lambda kind: None),
        "inspect_evals": types.ModuleType("inspect_evals"),
        "inspect_evals.gsm8k": types.ModuleType("inspect_evals.gsm8k"),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return calls


def test_posttrain_eval_reads_the_base_models_chat_template(tmp_path, monkeypatch, capsys, gpu_stack):
    """The failing input of 15-1: in a copy (committed files only) the baseline's eval
    used to exit "chat template not found at templates/qwen3.jinja"."""
    copy = committed_copy(tmp_path)
    monkeypatch.chdir(copy)
    out = tmp_path / "metrics.json"
    monkeypatch.setattr(sys, "argv", ["evaluate.py", "--model-path", "Qwen/Qwen3-4B-Base",
                                      "--json-output-file", str(out),
                                      "--chat-template-model", "Qwen/Qwen3-4B-Base"])
    runpy.run_path(str(copy / "evaluate.py"), run_name="__main__")

    assert gpu_stack["tokenizer"] == ("Qwen/Qwen3-4B-Base", {"local_files_only": True})
    assert gpu_stack["eval"]["template"] == "TEMPLATE"
    assert "GSM8K accuracy: 0.5" in capsys.readouterr().out
    assert json.loads(out.read_text()) == {"accuracy": 0.5}
    assert not (copy / "templates").exists()


def test_run_eval_passes_the_base_model_for_the_template():
    text = (POSTTRAIN / "run_eval.sh").read_text()
    assert '--chat-template-model "$BASE_MODEL"' in text


# --- posttrain: UV_ENV outside the project, no default ---

def run_native(copy: Path, uv_env: str | None, *command: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in {"UV_ENV", "ARTIFACTS"}}
    if uv_env is not None:
        env["UV_ENV"] = uv_env
    return subprocess.run(["bash", str(copy / "run_native.sh"), *command], capture_output=True,
                          text=True, env=env)


def test_run_native_refuses_without_uv_env(tmp_path):
    result = run_native(committed_copy(tmp_path), None, "true")
    assert result.returncode == 2
    assert "set UV_ENV in config.sh" in result.stderr


def test_run_native_refuses_an_env_inside_the_project(tmp_path):
    copy = committed_copy(tmp_path)
    (copy / "uv_env" / "bin").mkdir(parents=True)
    result = run_native(copy, str(copy / "uv_env"), "true")
    assert result.returncode == 2
    assert "inside the project" in result.stderr and "set UV_ENV in config.sh" in result.stderr


def test_run_native_refuses_a_missing_env(tmp_path):
    result = run_native(committed_copy(tmp_path), str(tmp_path / "nowhere"), "true")
    assert result.returncode == 2
    assert "no Python env" in result.stderr


def test_run_native_runs_in_an_env_outside_the_project(tmp_path):
    copy = committed_copy(tmp_path)
    python = tmp_path / "env" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\necho stand-in python\n")
    python.chmod(0o755)
    result = run_native(copy, str(tmp_path / "env"), "python")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "stand-in python\n"


def test_posttrain_ignores_training_by_products():
    ignored = (POSTTRAIN / ".gitignore").read_text().splitlines()
    for pattern in ("trainer_output/", "checkpoints/", "wandb/", "*.safetensors"):
        assert pattern in ignored


# --- workflow-optimization: inner model calls count in the run's cost ---

def test_workflow_benchmark_calls_are_read_as_priced_calls(tmp_path, monkeypatch):
    """The benchmark's usage lines, read the way the harness reads a command's usage
    file: one call per model call, with its cost."""
    monkeypatch.syspath_prepend(str(EXAMPLES / "workflow-optimization"))
    benchmark = __import__("benchmark")
    usage = tmp_path / "usage.jsonl"
    monkeypatch.setenv("AUTOHELIX_USAGE_FILE", str(usage))

    class Completed:
        returncode = 0
        stderr = ""
        stdout = json.dumps({"result": "{}", "total_cost_usd": 0.25})

    monkeypatch.setattr(benchmark.subprocess, "run", lambda command, **kwargs: Completed())
    model = benchmark.ClaudeCliModel(model=None)
    model.complete("a")
    model.complete("b")

    calls = read_usage(usage)
    assert [call["cost_usd"] for call in calls] == [0.25, 0.25]
    assert all(call["source"] == "workflow-benchmark" for call in calls)
