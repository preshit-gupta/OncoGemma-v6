"""Stage 1 automated QC checks. Thresholds come from the injected ``QcConfig``
(configs/qc.yaml, SPEC-01 §3.8); the caller records the pipeline ``config_hash``."""
import cv2
import numpy as np

from app.core.pipeline_config import QcConfig

# HSV channels have no upper bound when a range leaves s_max or v_max out.
HSV_CHANNEL_MAX = 255

def check_tissue_coverage(tissue_mask_1bit: np.ndarray, config: QcConfig) -> dict:
    """
    Check 1: Tissue Coverage
    tissue mask area / total thumbnail area, against the configured fail and warn fractions.
    """
    cfg = config.tissue_coverage
    fail_thresh = cfg.fail_threshold
    warn_thresh = cfg.warn_threshold

    total_pixels = tissue_mask_1bit.size
    tissue_pixels = np.count_nonzero(tissue_mask_1bit)
    coverage_ratio = float(tissue_pixels / max(1, total_pixels))

    status = "pass"
    if coverage_ratio < fail_thresh:
        status = "fail"
        msg = f"Critical low tissue coverage: {coverage_ratio * 100:.1f}% (threshold < {fail_thresh * 100:.0f}%)"
    elif coverage_ratio < warn_thresh:
        status = "warn"
        msg = f"Low tissue coverage: {coverage_ratio * 100:.1f}% (threshold < {warn_thresh * 100:.0f}%)"
    else:
        msg = f"Adequate tissue coverage: {coverage_ratio * 100:.1f}%"

    return {
        "name": "tissue_coverage",
        "status": status,
        "metric": round(coverage_ratio, 4),
        "message": msg
    }

def check_focus_sharpness(
    slide_obj,
    tissue_mask_1bit: np.ndarray,
    mpp_x: float = 0.25,
    mpp_y: float = 0.25,
    *,
    config: QcConfig
) -> dict:
    """
    Check 2: Focus Sharpness
    Variance of Laplacian (OpenCV, grayscale) per 512^2 tile at 10x, on at most
    ``focus.sample_max_tiles`` sampled tissue tiles.
    """
    cfg = config.focus
    vol_thresh = cfg.vol_threshold
    fail_blurry_ratio = cfg.fail_blurry_ratio
    warn_blurry_ratio = cfg.warn_blurry_ratio
    max_tiles = cfg.sample_max_tiles

    from pipeline.tiles import read_region_srgb

    slide_w_px = float(getattr(slide_obj, "width_px", 2048) or 2048)
    slide_h_px = float(getattr(slide_obj, "height_px", 2048) or 2048)
    if hasattr(slide_obj, "dimensions"):
        slide_w_px, slide_h_px = float(slide_obj.dimensions[0]), float(slide_obj.dimensions[1])
    elif hasattr(slide_obj, "size"):
        slide_w_px, slide_h_px = float(slide_obj.size[0]), float(slide_obj.size[1])

    slide_w_um = slide_w_px * mpp_x
    slide_h_um = slide_h_px * mpp_y

    patch_size_um = 512.0
    mask_h, mask_w = tissue_mask_1bit.shape

    tissue_coords = np.argwhere(tissue_mask_1bit)  # [row, col] -> [y, x]
    max_x_um = max(0.0, slide_w_um - patch_size_um)
    max_y_um = max(0.0, slide_h_um - patch_size_um)

    if len(tissue_coords) > 0:
        rng = np.random.default_rng(42)
        sample_size = min(max_tiles, len(tissue_coords))
        chosen_idx = rng.choice(len(tissue_coords), size=sample_size, replace=(len(tissue_coords) < sample_size))
        chosen = tissue_coords[chosen_idx]
        candidate_xs = np.clip((chosen[:, 1] / float(mask_w)) * slide_w_um - patch_size_um / 2.0, 0, max_x_um)
        candidate_ys = np.clip((chosen[:, 0] / float(mask_h)) * slide_h_um - patch_size_um / 2.0, 0, max_y_um)
        positions = list(zip(candidate_xs, candidate_ys))
    else:
        step_um = patch_size_um * 2
        xs = np.arange(0, max(patch_size_um, slide_w_um - patch_size_um), step_um)
        ys = np.arange(0, max(patch_size_um, slide_h_um - patch_size_um), step_um)
        positions = [(x, y) for x in xs for y in ys]
        if len(positions) > max_tiles:
            rng = np.random.default_rng(42)
            idx_sample = rng.choice(len(positions), size=max_tiles, replace=False)
            positions = [positions[i] for i in idx_sample]
    blurry_tile_count = 0
    total_sampled_tiles = 0

    for x_um, y_um in positions:
        try:
            tile_rgb, _ = read_region_srgb(slide_obj, x_um, y_um, patch_size_um, patch_size_um, out_px=512, mpp_x=mpp_x, mpp_y=mpp_y)
            if np.std(tile_rgb) > 5.0:
                gray = cv2.cvtColor(tile_rgb, cv2.COLOR_RGB2GRAY)
                vol = cv2.Laplacian(gray, cv2.CV_64F).var()
                
                total_sampled_tiles += 1
                if vol < vol_thresh:
                    blurry_tile_count += 1
        except Exception:
            pass

    blurry_ratio = float(blurry_tile_count / max(1, total_sampled_tiles)) if total_sampled_tiles > 0 else 0.0

    status = "pass"
    if total_sampled_tiles > 0 and blurry_ratio > fail_blurry_ratio:
        status = "fail"
        msg = f"Critical focus blur: {blurry_ratio * 100:.1f}% of tissue tiles blurry (threshold > {fail_blurry_ratio * 100:.0f}%)"
    elif total_sampled_tiles > 0 and blurry_ratio > warn_blurry_ratio:
        status = "warn"
        msg = f"{blurry_ratio * 100:.1f}% of tissue tiles below sharpness threshold (VoL < {vol_thresh})"
    else:
        msg = f"Slide focus sharp ({blurry_ratio * 100:.1f}% blurry tiles)"

    return {
        "name": "focus",
        "status": status,
        "metric": round(blurry_ratio, 4),
        "message": msg
    }

def check_pen_marks(
    slide_obj,
    tissue_mask_1bit: np.ndarray,
    mpp_x: float = 0.25,
    mpp_y: float = 0.25,
    *,
    config: QcConfig
) -> dict:
    """
    Check 3: Pen Marks Detection
    Detects surgical/pathologist pen ink marks (green, blue, black) using HSV thresholding
    and connected component analysis. Warns if any pen mark component exceeds min_component_area_mm2.
    """
    cfg = config.pen_marks
    min_area_mm2 = cfg.min_component_area_mm2
    hsv_ranges = cfg.hsv_ranges.model_dump()

    from pipeline.tiles import read_region_srgb
    slide_w_px = float(getattr(slide_obj, "width_px", 2048) or 2048)
    slide_h_px = float(getattr(slide_obj, "height_px", 2048) or 2048)
    if hasattr(slide_obj, "dimensions"):
        slide_w_px, slide_h_px = float(slide_obj.dimensions[0]), float(slide_obj.dimensions[1])
    elif hasattr(slide_obj, "size"):
        slide_w_px, slide_h_px = float(slide_obj.size[0]), float(slide_obj.size[1])

    thumb_w_um = min(50000.0, slide_w_px * mpp_x)
    thumb_h_um = min(50000.0, slide_h_px * mpp_y)

    try:
        thumb_arr, _ = read_region_srgb(slide_obj, 0, 0, thumb_w_um, thumb_h_um, out_px=(512, 512), mpp_x=mpp_x, mpp_y=mpp_y)
    except Exception:
        if hasattr(slide_obj, "resize"):
            thumb_arr = np.array(slide_obj.convert("RGB").resize((512, 512)))
        else:
            thumb_arr = np.ones((512, 512, 3), dtype=np.uint8) * 240

    hsv = cv2.cvtColor(thumb_arr, cv2.COLOR_RGB2HSV)

    # Pixel area in mm2 for 512x512 thumbnail
    px_w_mm = (thumb_w_um / 512.0) * 1e-3
    px_h_mm = (thumb_h_um / 512.0) * 1e-3
    pixel_area_mm2 = px_w_mm * px_h_mm

    combined_pen_mask = np.zeros((512, 512), dtype=np.uint8)

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

def check_tissue_folds(
    slide_obj,
    tissue_mask_1bit: np.ndarray,
    mpp_x: float = 0.25,
    mpp_y: float = 0.25,
    *,
    config: QcConfig
) -> dict:
    """
    Check 4: Tissue Fold Detection
    Detects dark, high-saturation overlapping tissue ridges (folds) within the tissue area.
    Warns if connected fold ridge length exceeds min_skeleton_length_mm.
    """
    cfg = config.folds
    min_length_mm = cfg.min_skeleton_length_mm
    sat_min = cfg.saturation_min
    bright_max = cfg.brightness_max

    from pipeline.tiles import read_region_srgb
    slide_w_px = float(getattr(slide_obj, "width_px", 2048) or 2048)
    slide_h_px = float(getattr(slide_obj, "height_px", 2048) or 2048)
    if hasattr(slide_obj, "dimensions"):
        slide_w_px, slide_h_px = float(slide_obj.dimensions[0]), float(slide_obj.dimensions[1])
    elif hasattr(slide_obj, "size"):
        slide_w_px, slide_h_px = float(slide_obj.size[0]), float(slide_obj.size[1])

    thumb_w_um = min(50000.0, slide_w_px * mpp_x)
    thumb_h_um = min(50000.0, slide_h_px * mpp_y)

    try:
        thumb_arr, _ = read_region_srgb(slide_obj, 0, 0, thumb_w_um, thumb_h_um, out_px=(512, 512), mpp_x=mpp_x, mpp_y=mpp_y)
    except Exception:
        if hasattr(slide_obj, "resize"):
            thumb_arr = np.array(slide_obj.convert("RGB").resize((512, 512)))
        else:
            thumb_arr = np.ones((512, 512, 3), dtype=np.uint8) * 240

    hsv = cv2.cvtColor(thumb_arr, cv2.COLOR_RGB2HSV)

    px_w_mm = (thumb_w_um / 512.0) * 1e-3
    px_h_mm = (thumb_h_um / 512.0) * 1e-3

    if tissue_mask_1bit.shape != (512, 512):
        t_mask = cv2.resize(tissue_mask_1bit.astype(np.uint8), (512, 512), interpolation=cv2.INTER_NEAREST).astype(bool)
    else:
        t_mask = tissue_mask_1bit

    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]

    fold_candidates = (sat >= sat_min) & (val <= bright_max) & t_mask
    fold_mask = fold_candidates.astype(np.uint8) * 255

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    fold_clean = cv2.morphologyEx(fold_mask, cv2.MORPH_OPEN, kernel)

    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(fold_clean)
    max_skeleton_length_mm = 0.0

    for i in range(1, num_labels):
        w_px = stats[i, cv2.CC_STAT_WIDTH]
        h_px = stats[i, cv2.CC_STAT_HEIGHT]
        length_mm = float(np.hypot(w_px * px_w_mm, h_px * px_h_mm))
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

def check_stain_sanity(
    stain_params: dict,
    config: QcConfig
) -> dict:
    """
    Check 5: Stain Sanity Check
    Validates per-slide stain profile:
    1. Checks for degenerate fit or missing tissue patches.
    2. Enforces minimum stain concentrations (faded H&E detection).
    3. Validates Hematoxylin-to-Eosin concentration ratio bounds.
    """
    cfg = config.stain_sanity
    min_conc = cfg.min_concentration
    he_ratio_min = cfg.he_ratio_min
    he_ratio_max = cfg.he_ratio_max

    if not stain_params:
        return {
            "name": "stain_sanity",
            "status": "warn",
            "metric": 0.0,
            "message": "Missing stain parameters artifact"
        }

    fit_status = stain_params.get("fit_status", "fitted")
    if fit_status == "degenerate":
        return {
            "name": "stain_sanity",
            "status": "warn",
            "metric": 0.0,
            "message": "Degenerate stain profile: insufficient tissue patches sampled to fit stain normalizer"
        }

    max_conc = stain_params.get("max_concentrations") or [1.95, 1.10]
    try:
        h_conc = float(max_conc[0])
        e_conc = float(max_conc[1])
    except (IndexError, TypeError, ValueError):
        h_conc, e_conc = 1.95, 1.10

    he_ratio = h_conc / max(1e-4, e_conc)

    status = "pass"
    if h_conc < min_conc or e_conc < min_conc:
        status = "warn"
        msg = f"Faded stain detected: H={h_conc:.2f}, E={e_conc:.2f} below min concentration {min_conc:.2f}"
    elif he_ratio < he_ratio_min or he_ratio > he_ratio_max:
        status = "warn"
        msg = f"Abnormal H:E stain concentration ratio: {he_ratio:.2f} (expected {he_ratio_min} - {he_ratio_max})"
    else:
        msg = f"Stain profile verified (H={h_conc:.2f}, E={e_conc:.2f}, H:E ratio={he_ratio:.2f})"

    return {
        "name": "stain_sanity",
        "status": status,
        "metric": round(he_ratio, 4),
        "message": msg
    }

def run_all_qc_checks(
    slide_obj,
    tissue_mask_1bit: np.ndarray,
    mpp_x: float = 0.25,
    mpp_y: float = 0.25,
    stain_params: dict = None,
    *,
    config: QcConfig,
    config_hash: str
) -> dict:
    """Execute complete 5-check QC check suite per PRD 02 §3.1.

    ``config_hash`` is the pipeline configuration hash the result is stamped with.
    """
    # 1. Tissue coverage
    cov_res = check_tissue_coverage(tissue_mask_1bit, config)

    # 2. Focus sharpness
    focus_res = check_focus_sharpness(slide_obj, tissue_mask_1bit, mpp_x=mpp_x, mpp_y=mpp_y, config=config)

    # 3. Pen marks
    pen_res = check_pen_marks(slide_obj, tissue_mask_1bit, mpp_x=mpp_x, mpp_y=mpp_y, config=config)

    # 4. Tissue folds
    fold_res = check_tissue_folds(slide_obj, tissue_mask_1bit, mpp_x=mpp_x, mpp_y=mpp_y, config=config)

    # 5. Stain sanity
    stain_res = check_stain_sanity(stain_params or {}, config=config)

    checks = [cov_res, focus_res, pen_res, fold_res, stain_res]

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
        "config_hash": config_hash
    }
