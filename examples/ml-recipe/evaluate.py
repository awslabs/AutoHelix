"""Evaluate the trained model on held-out test data.

Prints per-class metrics and emits ##autohelix[accuracy=N] for tracking.
"""

import json
from pathlib import Path

import joblib
import numpy as np
from sklearn.metrics import classification_report, accuracy_score


def main():
    pipeline = joblib.load("output/pipeline.joblib")
    data = joblib.load("output/test_data.joblib")
    X_test, y_test = data["X_test"], data["y_test"]

    y_pred = pipeline.predict(X_test)
    accuracy = accuracy_score(y_test, y_pred)

    print("=== Evaluation ===")
    print()
    print(classification_report(y_test, y_pred, digits=3))

    classes = np.unique(y_test)
    print("Per-class accuracy:")
    for cls in classes:
        mask = y_test == cls
        cls_acc = (y_pred[mask] == y_test[mask]).mean()
        bar = "█" * int(cls_acc * 20) + "░" * (20 - int(cls_acc * 20))
        print(f"  class {cls}: {bar} {cls_acc:.3f}")
    print()
    print(f"  Overall accuracy: {accuracy:.4f}")
    print()

    results = {
        "accuracy": round(float(accuracy), 4),
        "per_class": {
            str(cls): round(float((y_pred[y_test == cls] == cls).mean()), 4)
            for cls in classes
        },
    }
    Path("output").mkdir(exist_ok=True)
    Path("output/eval_results.json").write_text(json.dumps(results, indent=2) + "\n")

    print(f"##autohelix[accuracy={accuracy:.4f}]")


if __name__ == "__main__":
    main()
