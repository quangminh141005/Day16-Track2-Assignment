#!/usr/bin/env python3
"""CPU LightGBM benchmark for README_gcp.md.

Run on the provisioned VM after downloading the Kaggle dataset:
    python3 benchmark.py
    python3 benchmark.py --data /path/to/creditcard.csv --output results/run.json

Requires lightgbm, scikit-learn, pandas and numpy (installed by Terraform).
Billing reports and monitoring screenshots must be collected separately.
"""

import argparse
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, help="CSV path (default: ./creditcard.csv or ~/ml-benchmark/creditcard.csv)")
    parser.add_argument("--output", type=Path, default=Path("benchmark_result.json"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--threads", type=int, default=min(2, os.cpu_count() or 1))
    parser.add_argument("--estimators", type=int, default=1000)
    parser.add_argument("--early-stopping-rounds", type=int, default=50)
    parser.add_argument("--latency-repeats", type=int, default=100)
    parser.add_argument("--throughput-repeats", type=int, default=20)
    args = parser.parse_args()
    for name in ("threads", "estimators", "early_stopping_rounds", "latency_repeats", "throughput_repeats"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.data is None:
        candidates = [Path("creditcard.csv"), Path.home() / "ml-benchmark/creditcard.csv"]
        args.data = next((path for path in candidates if path.is_file()), candidates[-1])
    args.data = args.data.expanduser()
    args.output = args.output.expanduser()
    return args


def run(args):
    try:
        import lightgbm as lgb
        import numpy as np
        import pandas as pd
        import sklearn
        from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score, roc_auc_score
        from sklearn.model_selection import train_test_split
    except ImportError as exc:
        raise ValueError("Missing ML dependency. Install: python3 -m pip install lightgbm scikit-learn pandas numpy") from exc

    if not args.data.is_file():
        raise ValueError(f"Dataset not found: {args.data}. Download mlg-ulb/creditcardfraud from Kaggle or pass --data.")
    print(f"Loading {args.data.resolve()}", flush=True)
    start = perf_counter()
    data = pd.read_csv(args.data)
    load_seconds = perf_counter() - start
    if "Class" not in data or len(data.columns) < 2:
        raise ValueError("CSV must contain numeric features and a binary 'Class' target.")
    if data.isna().any().any() or not all(pd.api.types.is_numeric_dtype(dtype) for dtype in data.dtypes):
        raise ValueError("CSV must contain numeric values without missing data.")
    if not np.isfinite(data.to_numpy()).all():
        raise ValueError("CSV contains infinite values.")
    if set(data["Class"].unique()) != {0, 1}:
        raise ValueError("Class must contain both 0 (normal) and 1 (fraud).")

    features = data.drop(columns="Class")
    target = data["Class"].astype(int)
    # 60% training, 20% validation, 20% held-out testing.
    x_dev, x_test, y_dev, y_test = train_test_split(
        features, target, test_size=0.2, stratify=target, random_state=args.seed
    )
    x_train, x_valid, y_train, y_valid = train_test_split(
        x_dev, y_dev, test_size=0.25, stratify=y_dev, random_state=args.seed
    )
    if any(labels.nunique() != 2 for labels in (y_train, y_valid, y_test)):
        raise ValueError("Each split must contain both classes; provide more fraud samples.")
    if len(x_test) < 1000:
        raise ValueError("The held-out test set needs at least 1000 rows for the required throughput benchmark.")

    parameters = dict(
        objective="binary", n_estimators=args.estimators, learning_rate=0.05,
        num_leaves=31, class_weight="balanced", random_state=args.seed,
        n_jobs=args.threads, verbosity=-1, deterministic=True, force_col_wise=True,
    )
    model = lgb.LGBMClassifier(**parameters)
    print(f"Training on {len(x_train):,} rows; validation {len(x_valid):,}; test {len(x_test):,}", flush=True)
    start = perf_counter()
    model.fit(
        x_train, y_train, eval_set=[(x_valid, y_valid)], eval_metric="auc",
        callbacks=[lgb.early_stopping(args.early_stopping_rounds, first_metric_only=True, verbose=False)],
    )
    training_seconds = perf_counter() - start
    best_iteration = int(model.best_iteration_ or model.n_estimators_)
    probabilities = model.predict_proba(x_test, num_iteration=best_iteration)[:, 1]
    predictions = (probabilities >= 0.5).astype(int)

    def measure(rows, repeats):
        # Warm up the prediction path; slicing and JSON serialization are excluded.
        for _ in range(5):
            model.predict_proba(rows, num_iteration=best_iteration)
        elapsed = []
        for _ in range(repeats):
            start = perf_counter()
            model.predict_proba(rows, num_iteration=best_iteration)
            elapsed.append(perf_counter() - start)
        return np.asarray(elapsed)

    single_times = measure(x_test.iloc[:1], args.latency_repeats)
    batch_times = measure(x_test.iloc[:1000], args.throughput_repeats)
    metrics = {
        "data_load_time_seconds": load_seconds,
        "training_time_seconds": training_seconds,
        "best_iteration": best_iteration,
        "auc_roc": float(roc_auc_score(y_test, probabilities)),
        "accuracy": float(accuracy_score(y_test, predictions)),
        "f1_score": float(f1_score(y_test, predictions, zero_division=0)),
        "precision": float(precision_score(y_test, predictions, zero_division=0)),
        "recall": float(recall_score(y_test, predictions, zero_division=0)),
        "inference_latency_1_row_ms": float(single_times.mean() * 1000),
        "inference_throughput_1000_rows_per_second": float(1000 * len(batch_times) / batch_times.sum()),
    }
    result = {
        **metrics,
        "inference_details": {
            "method": "predict_proba on pandas DataFrames; 5 warmups; perf_counter wall time",
            "latency_repeats": args.latency_repeats,
            "latency_median_ms": float(np.median(single_times) * 1000),
            "latency_p95_ms": float(np.percentile(single_times, 95) * 1000),
            "batch_rows": 1000,
            "throughput_repeats": args.throughput_repeats,
            "batch_mean_time_seconds": float(batch_times.mean()),
        },
        "dataset": {
            "path": str(args.data.resolve()), "rows": len(data),
            "features": list(features.columns), "fraud_rows": int(target.sum()),
            "split_rows": {"train": len(x_train), "validation": len(x_valid), "test": len(x_test)},
            "split_fraud_rows": {"train": int(y_train.sum()), "validation": int(y_valid.sum()), "test": int(y_test.sum())},
        },
        "run": {
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "seed": args.seed, "decision_threshold": 0.5,
            "early_stopping_rounds": args.early_stopping_rounds,
            "model_parameters": parameters, "platform": platform.platform(),
            "cpu_count": os.cpu_count(), "python_version": platform.python_version(),
            "versions": {"lightgbm": lgb.__version__, "scikit_learn": sklearn.__version__, "pandas": pd.__version__, "numpy": np.__version__},
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print("\nREADME_gcp.md benchmark metrics")
    for name, value in metrics.items():
        print(f"  {name:48s} {value if isinstance(value, int) else f'{value:.6f}'}")
    print(f"\nResults saved to {args.output.resolve()}")


def main():
    args = parse_args()
    try:
        run(args)
    except (ValueError, OSError) as exc:
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
