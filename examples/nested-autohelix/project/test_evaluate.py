"""Fast tests for the nested evaluator. No model agents are invoked."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from evaluate import (
    InnerTemplate,
    aggregate,
    configure_inner_project,
    evaluate_persisted_project,
    render_goal,
    summarize_history,
    validate_template,
)


def write_template(tmp_path: Path, *, iterations: object = 2) -> tuple[Path, Path]:
    template_path = tmp_path / "inner-template.yaml"
    rubric_path = tmp_path / "rubric.md"
    template_path.write_text(
        yaml.safe_dump(
            {
                "iterations": iterations,
                "goal": "{{TASK_GOAL}}\n\n{{RUBRIC}}\n",
            },
            sort_keys=False,
        )
    )
    rubric_path.write_text("Record evidence for the next iteration.\n")
    return template_path, rubric_path


def test_template_is_bounded_and_requires_placeholders(tmp_path):
    template_path, rubric_path = write_template(tmp_path)
    template = validate_template(template_path, rubric_path)
    assert template.iterations == 2

    template_path.write_text(
        "iterations: 5\ngoal: '{{TASK_GOAL}} {{RUBRIC}}'\n"
    )
    with pytest.raises(ValueError, match="iterations must be"):
        validate_template(template_path, rubric_path)

    template_path.write_text("iterations: 2\ngoal: '{{TASK_GOAL}}'\n")
    with pytest.raises(ValueError, match=r"\{\{RUBRIC\}\}"):
        validate_template(template_path, rubric_path)


def test_render_goal_injects_task_and_ephemeral_rubric():
    template = InnerTemplate(
        iterations=2,
        goal="{{TASK_GOAL}}\nHandoff:\n{{RUBRIC}}",
        rubric="Record measurements.",
    )
    assert render_goal(template, "Optimize solve().") == (
        "Optimize solve().\nHandoff:\nRecord measurements.\n\n"
        "Harness rule: leave solver.py changes uncommitted. Do not run git "
        "commit;\nAutoHelix validates and commits accepted changes."
    )


def test_configure_inner_project_freezes_budget_and_task_contract(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "autohelix.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "goal": "Original task goal.",
                "constraints": ["python check.py"],
                "metrics": [
                    {
                        "command": "python benchmark.py",
                        "values": {"speedup": "higher"},
                    }
                ],
                "scope": {"editable": ["solver.py"]},
                "budget": {"iterations": 99},
            },
            sort_keys=False,
        )
    )
    monkeypatch.setenv("NESTED_AUTOHELIX_AGENT", "mock")
    template = InnerTemplate(
        iterations=3,
        goal="{{TASK_GOAL}}\n{{RUBRIC}}",
        rubric="Keep evidence.",
    )

    configure_inner_project(
        tmp_path,
        template,
        total_seconds=360,
        cost_cap=0.75,
    )

    configured = yaml.safe_load(config_path.read_text())
    assert configured["constraints"] == ["python check.py"]
    assert configured["scope"] == {"editable": ["solver.py"]}
    assert configured["budget"] == {
        "iterations": 3,
        "iteration_time": 120,
        "cost": 0.75,
    }
    assert configured["agent"]["type"] == "mock"
    assert "Original task goal." in configured["goal"]
    assert "Keep evidence." in configured["goal"]
    assert "Do not run git commit" in configured["goal"]


def test_persisted_project_is_rechecked_and_rebenchmarked(tmp_path):
    (tmp_path / "check.py").write_text("print('ok')\n")
    (tmp_path / "benchmark.py").write_text(
        "print('speedup: 2.5')\n"
    )

    result = evaluate_persisted_project(tmp_path)

    assert result["valid"] is True
    assert result["speedup"] == 2.5
    assert result["validation_output"] == "ok\n"
    assert result["benchmark_output"] == "speedup: 2.5\n"


def test_history_summary_tracks_final_accepted_speedup_and_cost():
    history = [
        {
            "iteration": 0,
            "accepted": True,
            "metrics": {"speedup": 1.0},
            "usage": {},
        },
        {
            "iteration": 1,
            "accepted": True,
            "metrics": {"speedup": 3.0},
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cost_usd": 0.1,
            },
        },
        {
            "iteration": 2,
            "accepted": False,
            "metrics": {"speedup": 4.0},
            "usage": {
                "input_tokens": 50,
                "output_tokens": 10,
                "cost_usd": 0.05,
            },
        },
    ]

    summary = summarize_history(history)

    assert summary["speedup"] == 3
    assert summary["speedup_curve"] == [1, 3, 3]
    assert summary["tokens"] == 180
    assert summary["cost_usd"] == pytest.approx(0.15)


def test_aggregate_exposes_two_headline_metrics_and_keeps_diagnostics():
    metrics = aggregate(
        [
            {
                "speedup": 2.0,
                "valid": True,
                "cost_usd": 0.1,
                "tokens": 10,
                "wall_seconds": 2,
            },
            {
                "speedup": 8.0,
                "valid": True,
                "cost_usd": 0.2,
                "tokens": 30,
                "wall_seconds": 3,
            },
        ]
    )

    assert metrics["speedup"] == pytest.approx(4.0)
    assert metrics["cost"] == pytest.approx(0.3)
    assert metrics["valid_task_rate"] == 1.0
    assert metrics["inner_tokens"] == 40
    assert metrics["wall_seconds"] == 5
