"""Train a model from train_config.yaml and save it."""

import time
from pathlib import Path

import joblib
import numpy as np
import yaml
from sklearn.datasets import make_classification
from sklearn.decomposition import PCA
from sklearn.feature_selection import SelectKBest, f_classif
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import (
    GradientBoostingClassifier,
    RandomForestClassifier,
)
from sklearn.neighbors import KNeighborsClassifier
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import MinMaxScaler, RobustScaler, StandardScaler
from sklearn.svm import SVC

# Synthetic dataset. Edit these to experiment with harder/easier problems —
# they're fixed here (not in train_config.yaml) so the agent tunes the model,
# not the task. Many redundant/noise features + class imbalance leave room for
# scaling, feature selection, model choice, and class weighting to each help.
DATASET_PARAMS = dict(
    n_samples=1500,
    n_features=50,
    n_informative=10,
    n_redundant=8,
    n_classes=6,
    weights=[0.35, 0.25, 0.15, 0.12, 0.08, 0.05],
    class_sep=0.9,
    random_state=7,
    flip_y=0.05,
)

# Fixed evaluation protocol — the same split every run, so accuracy is
# comparable across iterations and can't be gamed by reseeding the split.
TEST_SIZE = 0.2
SPLIT_SEED = 42

MODELS = {
    "logistic_regression": LogisticRegression,
    "random_forest": RandomForestClassifier,
    "gradient_boosting": GradientBoostingClassifier,
    "knn": KNeighborsClassifier,
    "svc": SVC,
}

SCALERS = {
    "standard": StandardScaler,
    "minmax": MinMaxScaler,
    "robust": RobustScaler,
}


def load_config() -> dict:
    with open("train_config.yaml") as f:
        return yaml.safe_load(f)


def build_pipeline(config: dict) -> Pipeline:
    steps = []
    pre = config.get("preprocessing", {})

    scaler_name = pre.get("scaler")
    if scaler_name and scaler_name in SCALERS:
        steps.append(("scaler", SCALERS[scaler_name]()))

    feat_sel = pre.get("feature_selection")
    if feat_sel and isinstance(feat_sel, dict):
        method = feat_sel.get("method", "k_best")
        if method == "k_best":
            k = feat_sel.get("k", 10)
            steps.append(("feature_selection", SelectKBest(f_classif, k=k)))

    pca_cfg = pre.get("pca")
    if pca_cfg and isinstance(pca_cfg, dict):
        n = pca_cfg.get("n_components", 5)
        steps.append(("pca", PCA(n_components=n)))

    model_cfg = config.get("model", {})
    model_type = model_cfg.get("type", "logistic_regression")
    model_params = model_cfg.get("params", {})

    model_cls = MODELS.get(model_type, LogisticRegression)
    steps.append(("model", model_cls(**model_params)))

    return Pipeline(steps)


def main():
    config = load_config()

    X, y = make_classification(**DATASET_PARAMS)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=TEST_SIZE, random_state=SPLIT_SEED, stratify=y
    )

    print("=== Dataset ===")
    print(f"  samples: {len(X_train)} train, {len(X_test)} test")
    print(f"  features: {X.shape[1]}")
    print(f"  classes: {len(np.unique(y))}")
    print()

    pipeline = build_pipeline(config)
    print("=== Pipeline ===")
    for name, step in pipeline.steps:
        print(f"  {name}: {step.__class__.__name__}")
    print()

    print("=== Training ===")
    start = time.time()
    pipeline.fit(X_train, y_train)
    elapsed = time.time() - start
    print(f"  fit time: {elapsed:.2f}s")

    train_acc = pipeline.score(X_train, y_train)
    test_acc = pipeline.score(X_test, y_test)
    print(f"  train accuracy: {train_acc:.4f}")
    print(f"  test accuracy:  {test_acc:.4f}")
    gap = train_acc - test_acc
    if gap > 0.1:
        print(f"  WARNING: large train/test gap ({gap:.3f}) — possible overfitting")
    print()

    Path("output").mkdir(exist_ok=True)
    joblib.dump(pipeline, "output/pipeline.joblib")
    joblib.dump({"X_test": X_test, "y_test": y_test}, "output/test_data.joblib")
    print("  saved: output/pipeline.joblib")


if __name__ == "__main__":
    main()
