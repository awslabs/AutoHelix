"""Constraints: config must be valid and model must train successfully."""

import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).parent


def test_config_valid():
    with open(HERE / "train_config.yaml") as f:
        config = yaml.safe_load(f)
    assert isinstance(config, dict)
    assert "model" in config
    assert "type" in config["model"]


def test_model_type_known():
    with open(HERE / "train_config.yaml") as f:
        config = yaml.safe_load(f)
    known = {"logistic_regression", "random_forest", "gradient_boosting", "knn", "svc"}
    assert config["model"]["type"] in known


def test_train_runs():
    result = subprocess.run(
        [sys.executable, str(HERE / "train.py")],
        cwd=HERE,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"train.py failed:\n{result.stderr}"
    assert (HERE / "output/pipeline.joblib").exists()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
