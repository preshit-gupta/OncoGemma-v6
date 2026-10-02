"""The Stage 3 tumour head (SPEC-05 §4): seven tissue classes from Path Foundation tile embeddings.

Two registry models, each a sha-pinned joblib artifact run by the gateway (provider
``local_sklearn``), so every tile batch leaves one DecisionRecord per model:

- the head (``model.joblib``, a ``TumorHeadModel``): L2-normalised embedding, z-scored with the
  train statistics, then multinomial logistic regression; optionally ensembled with a binary
  invasive-tumour head. Its ``predict_proba`` gives the seven raw class probabilities.
- the calibrator (``calibrator.joblib``, an ``IsotonicCalibrator``): isotonic regression of the
  raw invasive-tumour probability, fitted on val (one-vs-rest). Its ``predict_proba`` gives
  ``[1 - p_tumor_cal, p_tumor_cal]``.

Both classes live here because joblib pickles them by import path: the training code
(``training.tumor_head``) builds them and the worker unpickles them from this module.
Nothing is trained at runtime; a missing or altered artifact fails the stage (SPEC-01 §3.9).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.core.run_context import DecisionContext
from app.core.tasks import EntityType, Task
from app.inference.gateway import EntityRef, ModelGateway, ModelInputs
from app.inference.outputs import ClassProbabilities

CALIBRATED_CLASS = 1  # column of p_tumor_cal in the calibrator's predict_proba


def l2_normalize(embeddings: np.ndarray) -> np.ndarray:
    """Row-wise L2 normalisation of (N, D) embeddings, as v5 did. Zero rows stay zero."""
    embeddings = np.asarray(embeddings, dtype=np.float64)
    if embeddings.ndim != 2:
        raise ValueError(f"Embeddings must be a 2D array of shape (N, D), got shape {embeddings.shape}")
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    return embeddings / np.where(norms == 0, 1.0, norms)


class TumorHeadModel:
    """L2 → z-score → multinomial logistic regression, optionally ensembled with a binary head.

    With a binary head, the positive class takes ``(1 - w) * p_multi + w * p_binary`` and the
    other classes share the rest in proportion to their multinomial probabilities.
    """

    def __init__(self, classes, mean, scale, multinomial, positive_class: str, binary=None, binary_weight: float = 0.0):
        self.classes_ = np.asarray(list(classes), dtype=object)
        if len(set(self.classes_)) != len(self.classes_):
            raise ValueError(f"duplicate classes {list(self.classes_)}")
        if positive_class not in set(self.classes_):
            raise ValueError(f"positive class {positive_class!r} is not one of {list(self.classes_)}")
        self.mean_ = np.asarray(mean, dtype=np.float64)
        self.scale_ = np.asarray(scale, dtype=np.float64)
        if self.mean_.shape != self.scale_.shape or self.mean_.ndim != 1 or np.any(self.scale_ <= 0):
            raise ValueError("mean and scale must be 1-D of equal length, with every scale positive")
        if list(multinomial.classes_) != list(range(len(self.classes_))):
            raise ValueError("the multinomial head must be fitted on class indices 0..K-1 in `classes` order")
        if binary is not None and list(binary.classes_) != [0, 1]:
            raise ValueError("the binary head must be fitted on labels {0, 1}")
        if not 0.0 <= binary_weight <= 1.0 or (binary is None and binary_weight != 0.0):
            raise ValueError(f"binary_weight must be in [0, 1], and 0 without a binary head; got {binary_weight}")
        self.multinomial = multinomial
        self.binary = binary
        self.binary_weight = float(binary_weight)
        self.positive_class = positive_class
        self.positive_index = int(np.flatnonzero(self.classes_ == positive_class)[0])
        self.n_features_in_ = int(self.mean_.size)

    def transform(self, embeddings: np.ndarray) -> np.ndarray:
        """The features the heads see: L2-normalised, then z-scored with the train statistics."""
        x = l2_normalize(embeddings)
        if x.shape[1] != self.n_features_in_:
            raise ValueError(f"expected {self.n_features_in_} features, got {x.shape[1]}")
        return (x - self.mean_) / self.scale_

    def predict_proba(self, embeddings: np.ndarray) -> np.ndarray:
        z = self.transform(embeddings)
        p = self.multinomial.predict_proba(z).astype(np.float64)
        if self.binary is None or self.binary_weight == 0.0:
            return p
        k = self.positive_index
        p_pos = (1.0 - self.binary_weight) * p[:, k] + self.binary_weight * self.binary.predict_proba(z)[:, 1]
        others = np.delete(p, k, axis=1)
        rest = others.sum(axis=1, keepdims=True)
        # The multinomial head gives every class a positive probability (softmax), so rest > 0.
        others = others / rest * (1.0 - p_pos)[:, None]
        return np.insert(others, k, p_pos, axis=1)


class IsotonicCalibrator:
    """One-vs-rest isotonic calibration of the raw positive-class probability, as a classifier.

    Input is the (N, 1) raw probability; ``predict_proba`` returns ``[1 - p_cal, p_cal]``.
    Values outside the fitted range are clipped to its ends.
    """

    def __init__(self, isotonic, positive_class: str):
        if getattr(isotonic, "out_of_bounds", None) != "clip":
            raise ValueError("the isotonic regression must clip out-of-range inputs")
        self.isotonic = isotonic
        self.positive_class = positive_class
        self.classes_ = np.asarray([0, 1])
        self.n_features_in_ = 1

    def predict_proba(self, raw: np.ndarray) -> np.ndarray:
        raw = np.asarray(raw, dtype=np.float64)
        if raw.ndim != 2 or raw.shape[1] != 1:
            raise ValueError(f"the calibrator takes an (N, 1) probability column, got shape {raw.shape}")
        p = np.clip(self.isotonic.predict(raw[:, 0]), 0.0, 1.0)
        return np.column_stack([1.0 - p, p])


@dataclass(frozen=True)
class TileScores:
    classes: list[str]
    probabilities: np.ndarray  # (N, K) raw head probabilities, columns in ``classes`` order
    p_tumor_raw: np.ndarray  # (N,) the positive class's raw probability
    p_tumor_cal: np.ndarray  # (N,) after calibration
    head_record_id: str
    calibrator_record_id: str


def score_tiles(
    embeddings: np.ndarray,
    tile_ids: list[str],
    *,
    head_key: str,
    calibrator_key: str,
    embed_key: str,
    positive_class: str,
    gateway: ModelGateway,
    ctx: DecisionContext,
) -> TileScores:
    """Raw class probabilities and calibrated tumour probability of every tile, through the gateway."""
    if embeddings.shape[0] != len(tile_ids):
        raise ValueError(f"{embeddings.shape[0]} embeddings for {len(tile_ids)} tiles")
    entity = EntityRef(EntityType.TILE_BATCH, "tiles", ids=tuple(tile_ids))
    head = gateway.invoke(
        Task.TUMOR_HEAD, head_key, ModelInputs(features=np.asarray(embeddings, dtype=np.float32), features_producer=embed_key),
        ctx, entity, ClassProbabilities,
    )
    classes = [str(c) for c in head.output.classes]
    if positive_class not in classes:
        raise ValueError(f"{head_key} has no class {positive_class!r}; its classes are {classes}")
    probabilities = np.asarray(head.output.probabilities, dtype=np.float64)
    p_raw = head.output.column(positive_class).astype(np.float64)
    calibrated = gateway.invoke(
        Task.TUMOR_HEAD, calibrator_key, ModelInputs(features=p_raw[:, None], features_producer=head_key),
        ctx, entity, ClassProbabilities,
    )
    return TileScores(
        classes=classes,
        probabilities=probabilities,
        p_tumor_raw=p_raw,
        p_tumor_cal=calibrated.output.column(CALIBRATED_CLASS).astype(np.float64),
        head_record_id=str(head.record_id),
        calibrator_record_id=str(calibrated.record_id),
    )


def tile_raster(i: np.ndarray, j: np.ndarray, values: np.ndarray, n_cols: int, n_rows: int, fill=np.nan) -> np.ndarray:
    """Per-tile values placed on the (n_rows, n_cols) grid; tiles not listed take ``fill``."""
    raster = np.full((n_rows, n_cols), fill, dtype=np.float64)
    raster[np.asarray(j), np.asarray(i)] = values
    return raster


def smooth_tile_probabilities(raster: np.ndarray, sigma_tiles: float) -> np.ndarray:
    """Gaussian smoothing over tissue tiles only (SPEC-05 §4.2 ablation); non-tissue (NaN) stays NaN.

    Normalised convolution: off-tissue tiles neither contribute nor dilute, so a tile at the
    tissue edge is averaged over its tissue neighbours.
    """
    from scipy import ndimage

    if sigma_tiles <= 0:
        raise ValueError(f"sigma_tiles must be positive, got {sigma_tiles}")
    tissue = ~np.isnan(raster)
    num = ndimage.gaussian_filter(np.where(tissue, raster, 0.0), sigma_tiles, mode="constant")
    den = ndimage.gaussian_filter(tissue.astype(np.float64), sigma_tiles, mode="constant")
    out = np.full_like(raster, np.nan)
    out[tissue] = num[tissue] / den[tissue]
    return out
