"""Stage 1 automated QC checks (SPEC-04 §3.7).

Thresholds come from the injected ``QcConfig`` (configs/qc.yaml) and the case's specimen profile
(configs/specimen_profiles.yaml, ``SpecimenQcConfig``); the caller records the pipeline
``config_hash``. Every pixel is read through ``read_region_at_mpp``, tissue comes from the
registered ``TissueMask``, and a check that cannot run raises or says it did not assess anything:
nothing is replaced by a grey thumbnail or a made-up value.
"""
import cv2
import numpy as np

from app.core.pipeline_config import QcConfig, SpecimenQcConfig
from pipeline.slide_io import SlideReader, read_extent, read_region_at_mpp
from pipeline.tissue_mask import TissueMask

# HSV channels have no upper bound when a range leaves s_max or v_max out.
HSV_CHANNEL_MAX = 255
# Millimetres per micrometre.
MM_PER_UM = 1e-3


def check_tissue_coverage(mask: TissueMask, specimen_qc: SpecimenQcConfig) -> dict:
    """
    Check 1: Tissue area
    Absolute tissue area of the registered mask in mm², against the specimen profile's fail and warn areas:
    what matters for grading is how much tissue there is, not how full the glass is.
    """
    fail_area = specimen_qc.tissue_area_fail_mm2
    warn_area = specimen_qc.tissue_area_warn_mm2
    area = mask.area_mm2

    status = "pass"
    if area < fail_area:
        status = "fail"
        msg = f"Critical low tissue area: {area:.2f} mm² (threshold < {fail_area:g} mm²)"
    elif area < warn_area:
        status = "warn"
        msg = f"Low tissue area: {area:.2f} mm² (threshold < {warn_area:g} mm²)"
    else:
        msg = f"Adequate tissue area: {area:.2f} mm²"

    return {
        "name": "tissue_coverage",
        "status": status,
        "metric": round(area, 4),
        "message": msg
    }


def check_focus_sharpness(
    reader: SlideReader,
    mask: TissueMask,
    *,
    config: QcConfig,
    specimen_qc: SpecimenQcConfig,
    seed: int
) -> dict:
    """
    Check 2: Focus Sharpness
    Variance of Laplacian (OpenCV, grayscale) per tile at ``focus.mpp``, on at most
    ``focus.sample_max_tiles`` tiles drawn uniformly (seeded) from the tiles that are mostly tissue.
    """
    cfg = config.focus
    tile_um = cfg.tile_size_px * cfg.mpp
    tiles = list(mask.tiles(tile_um, cfg.min_tissue_fraction))
    if len(tiles) > cfg.sample_max_tiles:
        chosen = np.random.default_rng(seed).choice(len(tiles), size=cfg.sample_max_tiles, replace=False)
        tiles = [tiles[i] for i in sorted(chosen)]

    if not tiles:
        return {
            "name": "focus",
            "status": "warn",
            "metric": None,
            "message": (
                f"Focus not assessed: no {tile_um:g} µm tile is at least {cfg.min_tissue_fraction * 100:.0f}% tissue"
            ),
        }

    blurry = 0
    for tile in tiles:
        rgb = read_region_at_mpp(reader, tile.x_um, tile.y_um, tile_um, tile_um, cfg.mpp).rgb
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        if cv2.Laplacian(gray, cv2.CV_64F).var() < specimen_qc.focus_vol_threshold:
            blurry += 1
    blurry_ratio = blurry / len(tiles)

    status = "pass"
    if blurry_ratio > specimen_qc.focus_fail_ratio:
        status = "fail"
        msg = f"Critical focus blur: {blurry_ratio * 100:.1f}% of tissue tiles blurry (threshold > {specimen_qc.focus_fail_ratio * 100:.0f}%)"
    elif blurry_ratio > specimen_qc.focus_warn_ratio:
        status = "warn"
        msg = f"{blurry_ratio * 100:.1f}% of tissue tiles below sharpness threshold (VoL < {specimen_qc.focus_vol_threshold})"
    else:
        msg = f"Slide focus sharp ({blurry_ratio * 100:.1f}% blurry tiles)"

    return {
        "name": "focus",
        "status": status,
        "metric": round(blurry_ratio, 4),
        "message": msg
    }


def check_pen_marks(overview_rgb: np.ndarray, overview_mpp: float, *, config: QcConfig) -> dict:
    """
    Check 3: Pen Marks Detection
    Detects surgical/pathologist pen ink marks (green, blue, black) using HSV thresholding
    and connected component analysis. Warns if any pen mark component exceeds min_component_area_mm2.
    ``overview_rgb`` is the whole slide at ``overview_mpp`` µm/px.
    """
    cfg = config.pen_marks
    min_area_mm2 = cfg.min_component_area_mm2
    hsv_ranges = cfg.hsv_ranges.model_dump()

    hsv = cv2.cvtColor(overview_rgb, cv2.COLOR_RGB2HSV)
    pixel_area_mm2 = (overview_mpp * MM_PER_UM) ** 2

    combined_pen_mask = np.zeros(overview_rgb.shape[:2], dtype=np.uint8)

    for color, rng_cfg in hsv_ranges.items():
        h_min, h_max = rng_cfg["h_min"], rng_cfg["h_max"]
        s_min, v_min = rng_cfg["s_min"], rng_cfg["v_min"]
        s_max = HSV_CHANNEL_MAX if rng_cfg["s_max"] is None else rng_cfg["s_max"]
        v_max = HSV_CHANNEL_MAX if rng_cfg["v_max"] is None else rng_cfg["v_max"]

        lower = np.array([h_min, s_min, v_min], dtype=np.uint8)
        upper = np.array([h_max, s_max, v_max], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower, upper)
        combined_pen_mask = cv2.bitwise_or(combined_pen_mask, mask)

    # Morphological open to prune single-pixel noise
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    combined_pen_mask = cv2.morphologyEx(combined_pen_mask, cv2.MORPH_OPEN, kernel)

    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(combined_pen_mask)
    max_component_area_mm2 = 0.0

    for i in range(1, num_labels):
        area_px = stats[i, cv2.CC_STAT_AREA]
        area_mm2 = area_px * pixel_area_mm2
        if area_mm2 > max_component_area_mm2:
            max_component_area_mm2 = area_mm2

    status = "pass"
    if max_component_area_mm2 >= min_area_mm2:
        status = "warn"
        msg = f"Pen markings detected: largest mark {max_component_area_mm2:.2f} mm² (warning threshold >= {min_area_mm2:.1f} mm²)"
    else:
        msg = f"No significant pen markings detected (largest mark {max_component_area_mm2:.2f} mm²)"

    return {
        "name": "pen_marks",
        "status": status,
        "metric": round(max_component_area_mm2, 4),
        "message": msg
    }


def check_tissue_folds(overview_rgb: np.ndarray, overview_tissue: np.ndarray, overview_mpp: float, *, config: QcConfig) -> dict:
    """
    Check 4: Tissue Fold Detection
    Detects dark, high-saturation overlapping tissue ridges (folds) within the tissue area.
    Warns if connected fold ridge length exceeds min_skeleton_length_mm.
    ``overview_tissue`` is the registered mask on the overview's pixel grid.
    """
    cfg = config.folds
    min_length_mm = cfg.min_skeleton_length_mm
    sat_min = cfg.saturation_min
    bright_max = cfg.brightness_max

    hsv = cv2.cvtColor(overview_rgb, cv2.COLOR_RGB2HSV)
    px_mm = overview_mpp * MM_PER_UM

    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]

    fold_candidates = (sat >= sat_min) & (val <= bright_max) & overview_tissue
    fold_mask = fold_candidates.astype(np.uint8) * HSV_CHANNEL_MAX

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    fold_clean = cv2.morphologyEx(fold_mask, cv2.MORPH_OPEN, kernel)

    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(fold_clean)
    max_skeleton_length_mm = 0.0

    for i in range(1, num_labels):
        w_px = stats[i, cv2.CC_STAT_WIDTH]
        h_px = stats[i, cv2.CC_STAT_HEIGHT]
        length_mm = float(np.hypot(w_px * px_mm, h_px * px_mm))
        if length_mm > max_skeleton_length_mm:
            max_skeleton_length_mm = length_mm

    status = "pass"
    if max_skeleton_length_mm >= min_length_mm:
        status = "warn"
        msg = f"Tissue fold detected: ridge length {max_skeleton_length_mm:.2f} mm (warning threshold >= {min_length_mm:.1f} mm)"
    else:
        msg = f"No significant tissue folds detected (max length {max_skeleton_length_mm:.2f} mm)"

    return {
        "name": "folds",
        "status": status,
        "metric": round(max_skeleton_length_mm, 4),
        "message": msg
    }


def check_stain_sanity(stain_profile, config: QcConfig) -> dict:
    """
    Check 5: Stain Sanity Check
    Validates the slide's persisted stain profile (a ``StainProfile`` row):
    1. A degenerate fit (no valid patches, or two stains that cannot be told apart) is a warning,
       ``stain_fit_degenerate``: stages that need normalised colour then refuse the slide.
    2. Enforces minimum stain concentrations (faded H&E detection), on the slide's own maxima.
    3. Validates Hematoxylin-to-Eosin concentration ratio bounds.
    """
    cfg = config.stain_sanity
    min_conc = cfg.min_concentration
    he_ratio_min = cfg.he_ratio_min
    he_ratio_max = cfg.he_ratio_max

    if stain_profile.fit_status == "degenerate":
        return {
            "name": "stain_sanity",
            "status": "warn",
            "metric": 0.0,
            "message": (
                "stain_fit_degenerate: the stain fit is degenerate "
                f"({stain_profile.n_patches} valid patches); colour normalisation is unavailable"
            ),
        }

    h_conc, e_conc = (float(c) for c in stain_profile.maxc_src)
    he_ratio = h_conc / e_conc

    status = "pass"
    if h_conc < min_conc or e_conc < min_conc:
        status = "warn"
        msg = f"Faded stain detected: H={h_conc:.2f}, E={e_conc:.2f} below min concentration {min_conc:.2f}"
    elif he_ratio < he_ratio_min or he_ratio > he_ratio_max:
        status = "warn"
        msg = f"Abnormal H:E stain concentration ratio: {he_ratio:.2f} (expected {he_ratio_min} - {he_ratio_max})"
    else:
        msg = f"Stain profile verified (H={h_conc:.2f}, E={e_conc:.2f}, H:E ratio={he_ratio:.2f}, {stain_profile.fit_status} fit)"

    return {
        "name": "stain_sanity",
        "status": status,
        "metric": round(he_ratio, 4),
        "message": msg
    }


def check_resolution(native_mpp: float, config: QcConfig) -> dict:
    """
    Check 6: Native resolution
    Warns when the slide's finest resolution is coarser than ``warn_native_mpp`` (it will be upsampled
    for mitosis detection) and fails beyond ``fail_native_mpp`` (mitosis counting is not supported).
    """
    cfg = config.resolution
    status = "pass"
    if native_mpp > cfg.fail_native_mpp:
        status = "fail"
        msg = f"Resolution too coarse for mitosis counting: {native_mpp:.3f} µm/px (threshold > {cfg.fail_native_mpp:g})"
    elif native_mpp > cfg.warn_native_mpp:
        status = "warn"
        msg = f"Coarse resolution: {native_mpp:.3f} µm/px will be upsampled for mitosis detection (threshold > {cfg.warn_native_mpp:g})"
    else:
        msg = f"Native resolution {native_mpp:.3f} µm/px"

    return {
        "name": "resolution",
        "status": status,
        "metric": round(native_mpp, 4),
        "message": msg
    }


def run_all_qc_checks(
    reader: SlideReader,
    mask: TissueMask,
    stain_profile,
    *,
    config: QcConfig,
    specimen_qc: SpecimenQcConfig,
    seed: int,
    config_hash: str
) -> dict:
    """Execute the QC check suite (PRD 02 §3.1, SPEC-04 §3.7).

    ``stain_profile`` is the slide's persisted ``StainProfile``, ``seed`` seeds the focus tile sample,
    and ``config_hash`` is the pipeline configuration hash the result is stamped with.
    """
    overview_mpp = config.overview.mpp
    overview_rgb = read_extent(reader, overview_mpp)
    height_px, width_px = overview_rgb.shape[:2]
    overview_tissue = mask.at_mpp(overview_mpp, width_px, height_px)

    checks = [
        check_tissue_coverage(mask, specimen_qc),
        check_focus_sharpness(reader, mask, config=config, specimen_qc=specimen_qc, seed=seed),
        check_pen_marks(overview_rgb, overview_mpp, config=config),
        check_tissue_folds(overview_rgb, overview_tissue, overview_mpp, config=config),
        check_stain_sanity(stain_profile, config=config),
        check_resolution(reader.native_mpp, config=config),
    ]

    statuses = [c["status"] for c in checks]
    if "fail" in statuses:
        overall_verdict = "fail"
    elif "warn" in statuses:
        overall_verdict = "warn"
    else:
        overall_verdict = "pass"

    return {
        "verdict": overall_verdict,
        "checks": checks,
        "native_mpp": reader.native_mpp,
        "config_hash": config_hash
    }
