"""Stain normalisation: the single authority (SPEC-04 §3.4).

Stage 2 fits one stain profile per slide (Macenko stain vectors and concentrations of the slide,
and of the colour reference it is mapped to) and persists it. Every later stage applies that
profile through ``StainTransform``, a fixed per-pixel function that estimates nothing from the
image it is given, so a pixel's colour does not depend on which tile it was read in.

Normaliser construction and the transform's ``apply`` belong to this module and to
``pipeline/slide_io.py`` (tests/test_single_authority.py).
"""
import hashlib
from dataclasses import dataclass
from typing import Sequence
from uuid import UUID

import numpy as np

from app.core.pipeline_config import StainFitConfig, StainReference
from pipeline.errors import DegenerateStainProfileError
from pipeline.slide_io import SlideReader, read_region_at_mpp

# Bump on any change that alters a fitted profile or the transform's output (SPEC-04 §5).
FITTER_VERSION = "macenko_np_v2"

RGB_CHANNELS = 3

# 8-bit intensity -> optical density. A table lookup keeps the mapping exactly pointwise.
_OD_LUT = -np.log10(np.maximum(np.arange(256, dtype=np.float64), 1.0) / 255.0)


def rgb_to_od(rgb: np.ndarray) -> np.ndarray:
    """Optical density of an RGB uint8 array, one value per channel."""
    return _OD_LUT[rgb]


@dataclass(frozen=True)
class StainEstimate:
    w: np.ndarray  # 2x3 unit stain vectors, hematoxylin first
    max_conc: np.ndarray  # each stain's concentration at conc_percentile
    n_pixels: int  # stained pixels the estimate rests on


def estimate_stain(rgb: np.ndarray, cfg: StainFitConfig) -> StainEstimate | None:
    """Macenko stain vectors and maximum concentrations of the stained pixels of ``rgb``.

    Stained pixels have a channel optical density of at least ``cfg.od_beta``. Returns None when
    the image cannot fix two stain directions: too few stained pixels, or their angles in the
    plane of the two main OD components span less than ``cfg.min_angle_spread_rad``.
    """
    od = rgb_to_od(rgb.reshape(-1, 3))
    tissue = od[np.any(od >= cfg.od_beta, axis=1)]
    if len(tissue) < cfg.min_tissue_pixels:
        return None
    _, _, vt = np.linalg.svd(tissue, full_matrices=False)
    plane = vt[:2].copy()
    # A singular vector's sign is arbitrary. Make each start positive (stains absorb), so the
    # same pixels always give the same plane.
    plane *= np.where(plane[:, :1] < 0, -1.0, 1.0)
    projected = tissue @ plane.T
    angles = np.arctan2(projected[:, 1], projected[:, 0])
    low, high = np.percentile(angles, [cfg.angle_percentile, 100.0 - cfg.angle_percentile])
    if high - low < cfg.min_angle_spread_rad:
        return None
    v_low = plane.T @ np.array([np.cos(low), np.sin(low)])
    v_high = plane.T @ np.array([np.cos(high), np.sin(high)])
    stains = np.vstack((v_low, v_high) if v_low[0] > v_high[0] else (v_high, v_low))
    stains = stains / np.linalg.norm(stains, axis=1, keepdims=True)
    concentrations = np.linalg.lstsq(stains.T, tissue.T, rcond=None)[0]
    max_conc = np.percentile(concentrations, cfg.conc_percentile, axis=1)
    if not np.all(max_conc > 0):
        return None
    return StainEstimate(stains, max_conc, len(tissue))


def reference_from_patch(rgb: np.ndarray, cfg: StainFitConfig, reference_id: str, source: str) -> StainReference:
    """A colour reference fitted on one image (the v5 reference patch)."""
    estimate = estimate_stain(rgb, cfg)
    if estimate is None:
        raise DegenerateStainProfileError(f"{source}: the image has no two separable stains, so it cannot be a colour reference")
    return StainReference(
        reference_id=reference_id,
        w_tgt=estimate.w.tolist(),
        maxc_tgt=estimate.max_conc.tolist(),
        fitter_version=FITTER_VERSION,
        n_slides=0,
        slide_ids_sha256=None,
        source=source,
    )


@dataclass(frozen=True)
class StainFit:
    """A slide's fitted stain profile, before it is persisted as a ``stain_profiles`` row."""

    fitter_version: str
    reference_id: str
    w_src: list[list[float]]
    maxc_src: list[float]
    w_tgt: list[list[float]]
    maxc_tgt: list[float]
    fit_status: str  # fitted | sparse | degenerate
    n_patches: int
    mosaic_sha256: str


def _mean_saturation(rgb: np.ndarray) -> float:
    """Mean HSV saturation, 0..1."""
    high = rgb.max(axis=2).astype(np.float64)
    low = rgb.min(axis=2).astype(np.float64)
    saturation = np.divide(high - low, high, out=np.zeros_like(high), where=high > 0)
    return float(saturation.mean())


def fit_stain_profile(
    reader: SlideReader,
    origins_um: Sequence[tuple[float, float]],
    cfg: StainFitConfig,
    reference: StainReference,
) -> StainFit:
    """Fit the slide's stain profile on patches read at ``origins_um`` (candidates, in priority order).

    Patches are read raw at ``cfg.fit_mpp`` and kept when their mean saturation reaches
    ``cfg.min_sat_mean``, up to ``cfg.n_patches``. The stain is estimated on their mosaic. A slide
    with no valid patch or no separable stains is 'degenerate': its source values repeat the
    reference's and StainTransform refuses it.
    """
    patches = []
    for x_um, y_um in origins_um:
        if len(patches) >= cfg.n_patches:
            break
        rgb = read_region_at_mpp(reader, x_um, y_um, cfg.patch_um, cfg.patch_um, cfg.fit_mpp).rgb
        if _mean_saturation(rgb) >= cfg.min_sat_mean:
            patches.append(rgb)

    mosaic = np.concatenate(patches, axis=0) if patches else np.empty((0, 0, 3), dtype=np.uint8)
    estimate = estimate_stain(mosaic, cfg) if patches else None
    if estimate is None:
        status, w_src, maxc_src = "degenerate", reference.w_tgt, reference.maxc_tgt
    else:
        status = "fitted" if len(patches) >= cfg.sparse_below else "sparse"
        w_src, maxc_src = estimate.w.tolist(), estimate.max_conc.tolist()
    return StainFit(
        fitter_version=FITTER_VERSION,
        reference_id=reference.reference_id,
        w_src=w_src,
        maxc_src=maxc_src,
        w_tgt=reference.w_tgt,
        maxc_tgt=reference.maxc_tgt,
        fit_status=status,
        n_patches=len(patches),
        mosaic_sha256=hashlib.sha256(mosaic.tobytes()).hexdigest(),
    )


def _matrix(value, shape: tuple[int, ...], name: str) -> np.ndarray:
    array = np.asarray(value, dtype=np.float64)
    if array.shape != shape or not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must be finite with shape {shape}, got {array.shape}")
    return array


class StainTransform:
    """A slide's persisted stain profile as a fixed per-pixel colour mapping.

    OD = -log10(max(rgb, 1) / 255); C = pinv(W_src^T) OD; C = max(C, 0) * maxC_tgt / maxC_src;
    OD' = W_tgt^T C; rgb' = 255 * 10^(-OD'). Pixels whose largest OD is below ``od_beta`` pass
    through unchanged. Nothing is estimated from the image, so ``apply(concat(A, B))`` equals
    ``concat(apply(A), apply(B))`` exactly.
    """

    def __init__(self, w_src, maxc_src, w_tgt, maxc_tgt, *, od_beta: float, profile_id: UUID | None = None):
        self.w_src = _matrix(w_src, (2, 3), "w_src")
        self.maxc_src = _matrix(maxc_src, (2,), "maxc_src")
        self.w_tgt = _matrix(w_tgt, (2, 3), "w_tgt")
        self.maxc_tgt = _matrix(maxc_tgt, (2,), "maxc_tgt")
        if np.any(self.maxc_src <= 0) or np.any(self.maxc_tgt <= 0):
            raise ValueError("maximum concentrations must be positive")
        if not np.isfinite(od_beta) or od_beta <= 0:
            raise ValueError(f"od_beta must be positive, got {od_beta!r}")
        self.od_beta = float(od_beta)
        self.profile_id = profile_id
        self._unmix = np.linalg.pinv(self.w_src.T)  # 2x3
        self._scale = self.maxc_tgt / self.maxc_src

    @classmethod
    def from_profile(cls, profile, *, od_beta: float) -> "StainTransform":
        """The transform of a ``stain_profiles`` row (or a ``StainFit``). A degenerate profile is refused."""
        if profile.fit_status == "degenerate":
            raise DegenerateStainProfileError(
                f"the slide's stain fit is degenerate ({profile.n_patches} valid patches); it cannot be normalised"
            )
        return cls(
            profile.w_src, profile.maxc_src, profile.w_tgt, profile.maxc_tgt,
            od_beta=od_beta, profile_id=getattr(profile, "id", None),
        )

    def apply(self, rgb: np.ndarray) -> np.ndarray:
        """Map RGB uint8 pixels (any leading shape, last axis 3) to the reference colour space."""
        rgb = np.asarray(rgb)
        if rgb.dtype != np.uint8 or rgb.shape[-1] != RGB_CHANNELS:
            raise ValueError(f"expected a uint8 array ending in 3 channels, got {rgb.dtype} {rgb.shape}")
        flat = rgb.reshape(-1, 3)
        od = rgb_to_od(flat)
        # Contiguous per-channel arrays and explicit sums, not a matrix product: BLAS can round
        # one column differently from another, which would make a pixel depend on its neighbours.
        d0, d1, d2 = (np.ascontiguousarray(od[:, k]) for k in range(3))
        p = self._unmix
        conc_h = np.maximum(p[0, 0] * d0 + p[0, 1] * d1 + p[0, 2] * d2, 0.0) * self._scale[0]
        conc_e = np.maximum(p[1, 0] * d0 + p[1, 1] * d1 + p[1, 2] * d2, 0.0) * self._scale[1]
        w = self.w_tgt
        channels = [
            np.clip(np.rint(255.0 * np.power(10.0, -(w[0, k] * conc_h + w[1, k] * conc_e))), 0, 255).astype(np.uint8)
            for k in range(3)
        ]
        out = np.stack(channels, axis=1)
        unstained = np.maximum(np.maximum(d0, d1), d2) < self.od_beta
        out[unstained] = flat[unstained]
        return out.reshape(rgb.shape)
