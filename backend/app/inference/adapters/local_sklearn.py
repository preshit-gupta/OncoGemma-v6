"""scikit-learn classifiers stored as joblib artifacts (SPEC-01 §3.4, provider ``local_sklearn``).

The artifact named by the registry is loaded once, and only if its sha256 matches
``artifact_sha256``, so the version recorded is the model that ran. A missing artifact
is ``Unavailable``: the worker never trains or substitutes a model at runtime (SPEC-01 §3.9).
"""
import hashlib
import threading
from pathlib import Path

import joblib
import numpy as np

from app.inference.adapters.base import AdapterRequest, CallRejected, RawResponse, Unavailable

# Relative artifact URIs resolve against the repository root, which holds models/ in the
# worker and API images (/app).
REPO_ROOT = Path(__file__).resolve().parents[4]


class LocalSklearnAdapter:
    def __init__(self, root: Path = REPO_ROOT):
        self._root = Path(root)
        self._models: dict[tuple[str, str], object] = {}
        self._lock = threading.Lock()

    def _load(self, entry):
        key = (entry.artifact_uri, entry.artifact_sha256)
        with self._lock:
            if key not in self._models:
                if "://" in entry.artifact_uri:
                    raise CallRejected(f"only repository artifacts are supported, got {entry.artifact_uri}")
                path = self._root / entry.artifact_uri
                if not path.is_file():
                    raise Unavailable(f"model artifact {path} does not exist")
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                if digest != entry.artifact_sha256:
                    raise CallRejected(f"{path} has sha256 {digest}; the registry pins {entry.artifact_sha256}")
                self._models[key] = joblib.load(path)
            return self._models[key]

    def call(self, entry, request: AdapterRequest, timeout_s: float) -> RawResponse:
        if request.features is None:
            raise CallRejected("a local_sklearn request needs features")
        model = self._load(entry)
        expected = getattr(model, "n_features_in_", None)
        if expected is not None and request.features.shape[1] != expected:
            raise CallRejected(f"model expects {expected} features, got {request.features.shape[1]}")
        probabilities = model.predict_proba(np.asarray(request.features))
        return RawResponse(data={
            "classes": [c.item() if hasattr(c, "item") else c for c in model.classes_],
            "probabilities": probabilities.tolist(),
        })
