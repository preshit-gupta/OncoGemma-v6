"""How the v5 development probe (models/probe/probe_v1.joblib) was made.

It is a LogisticRegression fitted to random vectors, not to pathology, so it is
provenance only: v5 retrained it at runtime when the file was missing, and v6 moved
that out of the worker (SPEC-01 §3.9). The bytes depend on the scikit-learn version,
so a retrained file will not match the sha256 pinned in configs/models.yaml.
"""
import os

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression


def train_default_probe(output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    joblib_path = os.path.join(output_dir, "probe_v1.joblib")
    np.random.seed(42)
    features = np.random.randn(500, 384).astype(np.float32)
    labels = (np.mean(features, axis=1) > 0).astype(int)
    classifier = LogisticRegression(C=1.0, max_iter=2000)
    classifier.fit(features, labels)
    joblib.dump(classifier, joblib_path)
    return joblib_path
