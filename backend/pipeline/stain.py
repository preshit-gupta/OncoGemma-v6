import hashlib
import os
from dataclasses import dataclass
from typing import Sequence
from uuid import UUID

import numpy as np
from PIL import Image

from app.core.pipeline_config import StainFitConfig, StainReference
from pipeline.errors import DegenerateStainProfileError
from pipeline.slide_io import SlideReader, read_region_at_mpp

class PureNumpyMacenkoNormalizer:
    """
    Pure NumPy implementation of Macenko & Reinhard Stain Normalization, matching Tiatoolbox API.
    Transforms source slide H&E RGB images to match target reference stain profile.
    """
    def __init__(self):
        self.stain_matrix_target = None
        self.max_conc_target = None
        self.stain_matrix_src = None
        self.max_conc_src = None
        self.ref_arr = None
        self._target_mean = None
        self._target_std = None

    @property
    def stain_matrix(self):
        return self.stain_matrix_target

    @property
    def max_concentrations(self):
        return self.max_conc_target

    @staticmethod
    def _rgb_to_od(rgb_arr: np.ndarray) -> np.ndarray:
        """Convert RGB image array [0, 255] to Optical Density (OD) space."""
        rgb = np.maximum(rgb_arr.astype(np.float64), 1.0)
        return -np.log10(rgb / 255.0)

    @staticmethod
    def _od_to_rgb(od_arr: np.ndarray) -> np.ndarray:
        """Convert Optical Density (OD) array back to RGB uint8 array [0, 255]."""
        od_clamped = np.maximum(od_arr, 0.0)
        rgb = 255.0 * np.power(10.0, -od_clamped)
        return np.clip(np.round(rgb), 0, 255).astype(np.uint8)

    def _get_stain_params(self, img_rgb: np.ndarray, beta: float = 0.15, alpha: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
        """Extract Macenko stain matrix (2x3) and 99th percentile max concentrations (2)."""
        od = self._rgb_to_od(img_rgb).reshape(-1, 3)
        mask = np.any(od >= beta, axis=1)
        od_tissue = od[mask]
        
        if len(od_tissue) < 10:
            od_tissue = od

        _, _, V = np.linalg.svd(od_tissue, full_matrices=False)
        V = V[:2]
        
        T_hat = np.dot(od_tissue, V.T)
        angles = np.arctan2(T_hat[:, 1], T_hat[:, 0])
        
        min_angle = np.percentile(angles, alpha)
        max_angle = np.percentile(angles, 100.0 - alpha)
        
        v_min = np.dot(V.T, np.array([np.cos(min_angle), np.sin(min_angle)]))
        v_max = np.dot(V.T, np.array([np.cos(max_angle), np.sin(max_angle)]))

        if np.abs(max_angle - min_angle) < 0.25:
            v_min = np.array([0.65, 0.70, 0.29])
            v_max = np.array([0.07, 0.99, 0.11])

        if v_min[0] > v_max[0]:
            stain_matrix = np.vstack((v_min, v_max))
        else:
            stain_matrix = np.vstack((v_max, v_min))
            
        stain_matrix /= np.linalg.norm(stain_matrix, axis=1, keepdims=True) + 1e-8
        
        concentrations = np.linalg.lstsq(stain_matrix.T, od.T, rcond=None)[0]
        max_conc = np.percentile(concentrations, 99.0, axis=1)
        max_conc = np.maximum(max_conc, 1e-4)

        return stain_matrix, max_conc

    @staticmethod
    def _rgb_to_lab(rgb_arr: np.ndarray) -> np.ndarray:
        """Vectorized RGB [0, 255] uint8/float to CIE-Lab float array."""
        rgb = np.maximum(rgb_arr.astype(np.float32) / 255.0, 1e-4)
        M = np.array([
            [0.412453, 0.357580, 0.180423],
            [0.212671, 0.715160, 0.072169],
            [0.019334, 0.119193, 0.950227]
        ], dtype=np.float32)
        xyz = rgb @ M.T
        xyz[:, :, 0] /= 0.950456
        xyz[:, :, 1] /= 1.000000
        xyz[:, :, 2] /= 1.088754

        delta = 6.0 / 29.0
        f_xyz = np.where(xyz > delta**3, np.cbrt(xyz), xyz / (3 * delta**2) + 4.0 / 29.0)
        lab = np.zeros_like(f_xyz)
        lab[:, :, 0] = 116.0 * f_xyz[:, :, 1] - 16.0
        lab[:, :, 1] = 500.0 * (f_xyz[:, :, 0] - f_xyz[:, :, 1])
        lab[:, :, 2] = 200.0 * (f_xyz[:, :, 1] - f_xyz[:, :, 2])
        return lab

    @staticmethod
    def _lab_to_rgb(lab_arr: np.ndarray) -> np.ndarray:
        """Vectorized CIE-Lab float array to RGB [0, 255] uint8 array."""
        fY = (lab_arr[:, :, 0] + 16.0) / 116.0
        fX = lab_arr[:, :, 1] / 500.0 + fY
        fZ = fY - lab_arr[:, :, 2] / 200.0

        delta = 6.0 / 29.0
        X = np.where(fX > delta, fX**3, 3 * delta**2 * (fX - 4.0 / 29.0)) * 0.950456
        Y = np.where(fY > delta, fY**3, 3 * delta**2 * (fY - 4.0 / 29.0)) * 1.000000
        Z = np.where(fZ > delta, fZ**3, 3 * delta**2 * (fZ - 4.0 / 29.0)) * 1.088754

        xyz = np.stack([X, Y, Z], axis=-1)
        M = np.array([
            [0.412453, 0.357580, 0.180423],
            [0.212671, 0.715160, 0.072169],
            [0.019334, 0.119193, 0.950227]
        ], dtype=np.float32)
        M_inv = np.linalg.inv(M)
        rgb = np.clip(xyz @ M_inv.T, 0.0, 1.0)
        return (rgb * 255.0).astype(np.uint8)

    def fit(self, target_rgb: np.ndarray, source_rgb: np.ndarray = None, beta: float = 0.15, alpha: float = 1.0):
        """Fit target reference and optional source slide stain parameters."""
        self.ref_arr = target_rgb
        self.stain_matrix_target, self.max_conc_target = self._get_stain_params(target_rgb, beta=beta, alpha=alpha)
        if source_rgb is not None:
            self.stain_matrix_src, self.max_conc_src = self._get_stain_params(source_rgb, beta=beta, alpha=alpha)
        return self

    def transform(self, source_rgb: np.ndarray, beta: float = 0.12) -> np.ndarray:
        """
        Calibrated clinical H&E stain normalization for digital pathology:
        1. Deconvolves optical density into Hematoxylin and Eosin stain channels using fitted source matrix if available.
        2. Normalizes concentrations to standard reference bounds.
        3. Re-projects onto target clinical H&E absorption vectors (deep purple nuclei, vibrant pink cytoplasm).
        4. Preserves clear glass background stroma.
        """
        rgb = np.maximum(source_rgb.astype(np.float64), 1.0)
        od = -np.log10(rgb / 255.0)
        orig_shape = source_rgb.shape

        # Reference target absorption vectors and max concentrations
        if self.stain_matrix_target is not None:
            W_target = np.array(self.stain_matrix_target, dtype=np.float64)
        else:
            # Standard reference H&E absorption vectors (WHO / tiatoolbox clinical profile)
            W_target = np.array([
                [0.644, 0.717, 0.267],   # Hematoxylin (deep royal violet/purple)
                [0.093, 0.954, 0.283]    # Eosin (rich vibrant pink/magenta)
            ], dtype=np.float64)
        W_target = W_target / (np.linalg.norm(W_target, axis=1, keepdims=True) + 1e-8)

        if self.max_conc_target is not None:
            maxC_target = np.array(self.max_conc_target, dtype=np.float64)
        else:
            maxC_target = np.array([1.85, 1.05], dtype=np.float64)

        od_flat = od.reshape(-1, 3)

        # Deconvolution: use fitted slide source matrix if available, else fall back to tile SVD
        if self.stain_matrix_src is not None and self.max_conc_src is not None:
            W_src = np.array(self.stain_matrix_src, dtype=np.float64)
            W_src = W_src / (np.linalg.norm(W_src, axis=1, keepdims=True) + 1e-8)
            maxC_src = np.array(self.max_conc_src, dtype=np.float64)
            maxC_src = np.maximum(maxC_src, 0.15)
            C = np.linalg.lstsq(W_src.T, od_flat.T, rcond=None)[0]
            C = np.maximum(C, 0.0)
        else:
            od_tissue = od_flat[np.any(od_flat > beta, axis=1)]
            if len(od_tissue) < 50:
                od_tissue = od_flat

            _, _, V = np.linalg.svd(od_tissue, full_matrices=False)
            V = V[:2]
            That = np.dot(od_tissue, V.T)
            phi = np.arctan2(That[:, 1], That[:, 0])

            min_phi = np.percentile(phi, 1.0)
            max_phi = np.percentile(phi, 99.0)

            v1 = np.dot(V.T, np.array([np.cos(min_phi), np.sin(min_phi)]))
            v2 = np.dot(V.T, np.array([np.cos(max_phi), np.sin(max_phi)]))

            if np.abs(max_phi - min_phi) < 0.25 or np.dot(v1, v2) > 0.95:
                W_src = np.array([[0.65, 0.70, 0.29], [0.07, 0.99, 0.11]], dtype=np.float64)
            else:
                if v1[0] > v2[0]:
                    W_src = np.array([v1, v2], dtype=np.float64)
                else:
                    W_src = np.array([v2, v1], dtype=np.float64)

            W_src = W_src / (np.linalg.norm(W_src, axis=1, keepdims=True) + 1e-8)

            C = np.linalg.lstsq(W_src.T, od_flat.T, rcond=None)[0]
            C = np.maximum(C, 0.0)

            if np.any(od_flat > beta):
                maxC_src = np.percentile(C[:, np.any(od_flat > beta, axis=1)], 99.0, axis=1)
            else:
                maxC_src = np.array([1.0, 1.0], dtype=np.float64)
            maxC_src = np.maximum(maxC_src, 0.15)

        scale = np.clip(maxC_target[:, None] / maxC_src[:, None], 0.75, 1.35)
        C_norm = C * scale

        od_norm = (W_target.T @ C_norm).T
        od_norm = np.maximum(od_norm, 0.0)

        rgb_norm = 255.0 * np.power(10.0, -od_norm)
        res_uint8 = np.clip(np.round(rgb_norm), 0, 255).astype(np.uint8).reshape(orig_shape)

        # Background transparency (clean glass)
        bg_mask = np.all(od_flat < 0.10, axis=1).reshape(orig_shape[:2])
        res_uint8[bg_mask] = source_rgb[bg_mask]

        return res_uint8

MacenkoNormalizer = PureNumpyMacenkoNormalizer

def get_macenko_normalizer_class():
    """Import tiatoolbox MacenkoNormalizer if available, else return PureNumpyMacenkoNormalizer."""
    try:
        from tiatoolbox.tools.stainnorm import MacenkoNormalizer as TiatoolboxNormalizer
        return TiatoolboxNormalizer
    except Exception as e:
        print(f"[Stain Normalizer Note] Using pure NumPy MacenkoNormalizer fallback ({e})")
        return PureNumpyMacenkoNormalizer

def resolve_config_path(path: str) -> str:
    """Resolve config file path relative to repo root or backend parent directory."""
    if os.path.isabs(path) and os.path.exists(path):
        return path
    if os.path.exists(path):
        return os.path.abspath(path)

    parent_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../", path))
    if os.path.exists(parent_path):
        return parent_path

    return os.path.abspath(path)

def fit_macenko_stain(
    slide_obj,
    checksum_sha256: str,
    ref_image_path: str = "configs/stain_reference.png",
    mpp_x: float = 0.25,
    mpp_y: float = 0.25
) -> tuple[object, dict, np.ndarray]:
    """
    Fits Macenko stain normalizer per-slide based on PRD §2.2 specs.
    
    1. Tissue mask at 1.25x: Otsu tissue masker on thumbnail.
    2. Seeded random sampling (seed = slide checksum int) of up to 50 512x512 patches at 10x.
    3. Rejects patches with saturation-mean < 0.05.
    4. Fits normalizer on target reference patch and source slide mosaic.
    """
    ref_image_path = resolve_config_path(ref_image_path)
    if not os.path.exists(ref_image_path):
        raise FileNotFoundError(f"Stain reference patch not found at {ref_image_path}")

    ref_img = Image.open(ref_image_path).convert("RGB")
    ref_arr = np.array(ref_img, dtype=np.uint8)

    normalizer_cls = get_macenko_normalizer_class()
    normalizer = normalizer_cls()

    # Seeded RNG from slide checksum
    seed_int = int(checksum_sha256[:8], 16) if checksum_sha256 and checksum_sha256 != "default_checksum" else 42
    rng = np.random.default_rng(seed_int)

    from pipeline.tiles import read_region_srgb

    slide_w_px = float(getattr(slide_obj, "width_px", 2048) or 2048)
    slide_h_px = float(getattr(slide_obj, "height_px", 2048) or 2048)
    if hasattr(slide_obj, "dimensions"):
        slide_w_px, slide_h_px = float(slide_obj.dimensions[0]), float(slide_obj.dimensions[1])

    thumb_w_um = min(50000.0, slide_w_px * mpp_x)
    thumb_h_um = min(50000.0, slide_h_px * mpp_y)

    thumb_arr, _ = read_region_srgb(slide_obj, 0, 0, thumb_w_um, thumb_h_um, out_px=(512, 512), mpp_x=mpp_x, mpp_y=mpp_y)

    # Otsu tissue mask at 1.25x
    gray_thumb = np.mean(thumb_arr, axis=2).astype(np.uint8)
    hist, bin_edges = np.histogram(gray_thumb, bins=256, range=(0, 256))
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2.0
    
    weight1 = np.cumsum(hist)
    weight2 = np.cumsum(hist[::-1])[::-1]
    mean1 = np.cumsum(hist * bin_centers) / np.maximum(1, weight1)
    mean2 = (np.cumsum((hist * bin_centers)[::-1]) / np.maximum(1, weight2[::-1]))[::-1]
    variance12 = weight1[:-1] * weight2[1:] * (mean1[:-1] - mean2[1:]) ** 2
    otsu_thresh = float(bin_centers[np.argmax(variance12)]) if len(variance12) > 0 else 220.0

    sat_thumb = (np.max(thumb_arr, axis=2).astype(np.int16) - np.min(thumb_arr, axis=2).astype(np.int16))
    effective_thresh = max(215.0, min(235.0, otsu_thresh))
    tissue_mask_1bit = (gray_thumb <= effective_thresh) | (sat_thumb > 12)

    # Sample up to 50 random 512x512 tissue patches at 10x (~512 um x 512 um)
    patch_size_um = 512.0
    valid_patches = []
    
    max_x_um = max(patch_size_um, thumb_w_um - patch_size_um)
    max_y_um = max(patch_size_um, thumb_h_um - patch_size_um)

    tissue_coords = np.argwhere(tissue_mask_1bit)  # [row, col] -> [y, x]
    mask_h, mask_w = tissue_mask_1bit.shape

    if len(tissue_coords) > 0:
        sample_size = min(150, len(tissue_coords))
        chosen_indices = rng.choice(len(tissue_coords), size=sample_size, replace=(len(tissue_coords) < sample_size))
        chosen_coords = tissue_coords[chosen_indices]
        candidate_xs = np.clip((chosen_coords[:, 1] / float(mask_w)) * thumb_w_um - patch_size_um / 2.0, 0, max_x_um)
        candidate_ys = np.clip((chosen_coords[:, 0] / float(mask_h)) * thumb_h_um - patch_size_um / 2.0, 0, max_y_um)
    else:
        candidate_xs = rng.uniform(0, max_x_um, size=100)
        candidate_ys = rng.uniform(0, max_y_um, size=100)

    for x_um, y_um in zip(candidate_xs, candidate_ys):
        if len(valid_patches) >= 50:
            break

        patch_rgb, _ = read_region_srgb(slide_obj, x_um, y_um, patch_size_um, patch_size_um, out_px=512, mpp_x=mpp_x, mpp_y=mpp_y)
        pil_patch = Image.fromarray(patch_rgb)
        hsv_patch = pil_patch.convert("HSV")
        sat_channel = np.array(hsv_patch)[:, :, 1] / 255.0
        
        if np.mean(sat_channel) >= 0.05:
            valid_patches.append(patch_rgb)

    fit_status = "fitted"
    if not valid_patches:
        fit_status = "degenerate"
        valid_patches.append(thumb_arr)
    elif len(valid_patches) < 5:
        fit_status = "sparse"

    mosaic = np.concatenate(valid_patches, axis=0)
    
    fit_success = False
    if hasattr(normalizer, "fit"):
        try:
            normalizer.fit(ref_arr, mosaic)
            fit_success = True
        except Exception as fit_err:
            import logging
            logging.warning(f"[Stain Normalizer Warning] Stain fit on mosaic failed ({fit_err}); retrying on target reference only.")
            try:
                normalizer.fit(ref_arr)
                fit_success = True
            except Exception as ref_err:
                logging.warning(f"[Stain Normalizer Warning] Target reference fit failed ({ref_err}); using default clinical vectors.")

    stain_mat = getattr(normalizer, "stain_matrix", None)
    if stain_mat is None:
        stain_mat = np.array([[0.65, 0.70, 0.29], [0.07, 0.99, 0.11]])
    
    max_conc = getattr(normalizer, "max_concentrations", None)
    if max_conc is None:
        max_conc = np.array([1.95, 1.10])

    stain_mat_src = getattr(normalizer, "stain_matrix_src", None)
    max_conc_src = getattr(normalizer, "max_conc_src", None)

    stain_params_dict = {
        "stain_matrix": np.array(stain_mat).tolist(),
        "max_concentrations": np.array(max_conc).tolist(),
        "stain_matrix_src": np.array(stain_mat_src).tolist() if stain_mat_src is not None else None,
        "max_conc_src": np.array(max_conc_src).tolist() if max_conc_src is not None else None,
        "ref_image_path": ref_image_path,
        "patches_sampled": len(valid_patches),
        "seed_int": seed_int,
        "fit_status": fit_status,
        "fit_success": fit_success
    }

    return normalizer, stain_params_dict, tissue_mask_1bit


# ---------------------------------------------------------------------------------------------
# Stain profiles and the pointwise transform (SPEC-04 §3.4, WP-3.2). Stage 2 fits a profile once
# per slide and persists it; every later stage applies it with StainTransform. The v5 normaliser
# above is deleted with its last callers in WP-3.4.
# ---------------------------------------------------------------------------------------------

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
