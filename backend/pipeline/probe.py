"""
Feature preprocessing for the triage tumour classifier.

The classifier runs through the model gateway (provider local_sklearn); its artifact,
sha256 and version come from configs/models.yaml. A missing artifact fails the stage:
nothing is trained or projected at runtime (SPEC-01 §3.9).
"""
import numpy as np


def l2_normalize(embeddings: np.ndarray) -> np.ndarray:
    """Row-wise L2 normalisation of (N, D) embeddings, as the probe was trained. Zero rows stay zero."""
    if embeddings.ndim != 2:
        raise ValueError(f"Embeddings must be a 2D array of shape (N, D), got shape {embeddings.shape}")
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    return (embeddings / np.where(norms == 0, 1.0, norms)).astype(np.float32)
