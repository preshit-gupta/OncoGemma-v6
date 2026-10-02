"""Local scikit-learn adapter, non-VLM output models and request batching (WP-2.3b)."""
import hashlib
import math
import shutil

import numpy as np
import pytest
from pydantic import ValidationError

from app.core.pipeline_config import get_pipeline_config
from app.inference.adapters.base import AdapterRequest, CallRejected, Unavailable
from app.inference.adapters.local_sklearn import REPO_ROOT, LocalSklearnAdapter
from app.inference.batching import base64_size, plan_batches
from app.inference.outputs import ClassProbabilities, EmbeddingBatch


def probe_entry(**changes):
    return get_pipeline_config().models.models["tumor_head"].model_copy(update=changes)


def features(n=3):
    rows = np.random.default_rng(0).standard_normal((n, 384)).astype(np.float32)
    return rows / np.linalg.norm(rows, axis=1, keepdims=True)


# --- LocalSklearnAdapter ----------------------------------------------------------------


def test_repo_tumor_head_predicts_class_probabilities():
    raw = LocalSklearnAdapter().call(probe_entry(), AdapterRequest("tumor_head", features=features()), 5.0)
    output = ClassProbabilities.model_validate(raw.data)
    assert output.classes == ["invasive_tumor", "stroma", "inflammatory", "necrosis"]  # models/tumor_head/1.0.0/card.json
    assert output.column("invasive_tumor").shape == (3,)


def test_artifact_is_loaded_once(tmp_path):
    root = tmp_path / "repo"
    (root / "models" / "tumor_head" / "1.0.0").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "models" / "tumor_head" / "1.0.0" / "model.joblib", root / "models" / "tumor_head" / "1.0.0" / "model.joblib")
    adapter = LocalSklearnAdapter(root=root)
    adapter.call(probe_entry(), AdapterRequest("tumor_head", features=features()), 5.0)
    (root / "models" / "tumor_head" / "1.0.0" / "model.joblib").unlink()
    adapter.call(probe_entry(), AdapterRequest("tumor_head", features=features()), 5.0)


def test_missing_artifact_is_unavailable(tmp_path):
    with pytest.raises(Unavailable, match="does not exist"):
        LocalSklearnAdapter(root=tmp_path).call(probe_entry(), AdapterRequest("tumor_head", features=features()), 5.0)


def test_artifact_must_match_the_pinned_sha256(tmp_path):
    (tmp_path / "models" / "tumor_head" / "1.0.0").mkdir(parents=True)
    (tmp_path / "models" / "tumor_head" / "1.0.0" / "model.joblib").write_bytes(b"retrained elsewhere")
    with pytest.raises(CallRejected, match="the registry pins"):
        LocalSklearnAdapter(root=tmp_path).call(probe_entry(), AdapterRequest("tumor_head", features=features()), 5.0)


def test_remote_artifacts_are_not_fetched():
    entry = probe_entry(artifact_uri="gs://models/tumor_head/1.0.0/model.joblib")
    with pytest.raises(CallRejected, match="only repository artifacts"):
        LocalSklearnAdapter().call(entry, AdapterRequest("tumor_head", features=features()), 5.0)


def test_features_are_required():
    with pytest.raises(CallRejected, match="needs features"):
        LocalSklearnAdapter().call(probe_entry(), AdapterRequest("tumor_head"), 5.0)


# --- output models --------------------------------------------------------------------


def test_embedding_batch_summarises_itself_for_the_record():
    batch = EmbeddingBatch.model_validate({"embeddings": [[1.0, 2.0], [3, 4.5]]})
    summary = batch.decision_output()
    assert summary["n"] == 2 and summary["dim"] == 2
    assert summary["float32_sha256"] == hashlib.sha256(np.asarray([[1, 2], [3, 4.5]], dtype=np.float32).tobytes()).hexdigest()


@pytest.mark.parametrize(
    "embeddings",
    [[], [[]], [[1.0, 2.0], [3.0]], [[1.0, math.nan]], [[True, 1.0]], [["1.0"]]],
)
def test_invalid_embeddings_are_refused(embeddings):
    with pytest.raises(ValidationError):
        EmbeddingBatch.model_validate({"embeddings": embeddings})


@pytest.mark.parametrize(
    "payload",
    [
        {"classes": [0], "probabilities": [[1.0]]},
        {"classes": [0, 0], "probabilities": [[0.5, 0.5]]},
        {"classes": [0, 1], "probabilities": [[0.5, 0.6]]},
        {"classes": [0, 1], "probabilities": [[1.0]]},
        {"classes": [0, 1], "probabilities": [[1.2, -0.2]]},
        {"classes": [0, 1], "probabilities": []},
    ],
)
def test_invalid_class_probabilities_are_refused(payload):
    with pytest.raises(ValidationError):
        ClassProbabilities.model_validate(payload)


def test_unknown_class_label_raises():
    output = ClassProbabilities.model_validate({"classes": [0, 1], "probabilities": [[0.3, 0.7]]})
    with pytest.raises(ValueError):
        output.column(2)


# --- batching ------------------------------------------------------------------------


def test_batches_respect_count_and_encoded_size():
    sizes = [300] * 7
    assert [list(b) for b in plan_batches(sizes, max_batch=3, max_request_bytes=10_000)] == [[0, 1, 2], [3, 4, 5], [6]]
    per_image = base64_size(300)
    assert [len(b) for b in plan_batches(sizes, max_batch=6, max_request_bytes=2 * per_image)] == [2, 2, 2, 1]


def test_an_image_over_the_request_limit_is_an_error_not_dropped():
    with pytest.raises(ValueError, match="image 1 alone"):
        plan_batches([10, 10_000], max_batch=6, max_request_bytes=1_000)


def test_no_images_no_batches():
    assert plan_batches([], max_batch=6, max_request_bytes=1_000) == []
