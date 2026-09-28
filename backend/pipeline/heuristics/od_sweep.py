"""v5 optical-density "hyperchromatic" mitosis candidate sweep (registry heuristic od_hyperchromatic_sweep).

Moved verbatim from pipeline/detect.py (v5 YoloMitosisDetector). v5 substituted its output
whenever the detector returned no candidates; v6 never does (SPEC-01 §3.9). It survives only
for the attribution study's A0 replay (SPEC-06 §6.2), which must record it as a heuristic.
"""
import math
from typing import List, Optional, Tuple

import numpy as np


def detect_hyperchromatic_features(
    tile_rgb: np.ndarray,
    conf_threshold: float,
    max_candidates_per_tile: Optional[int] = None,
) -> List[Tuple[float, float, float]]:
    """
    First-principles hematoxylin optical density & morphological candidate sweep.
    Adaptive chromatin thresholding + van Diest morphometric gating.
    Dynamically adapts to slide stain intensity so hyperchromatic slides are not flooded
    with resting interphase nuclei, while ensuring high recall of true mitoses.
    """
    h, w, _ = tile_rgb.shape
    if h < 32 or w < 32:
        return []

    # Convert to optical density
    rgb_norm = np.maximum(tile_rgb.astype(np.float32), 1.0) / 255.0
    od = -np.log(rgb_norm)
    # Hematoxylin OD component
    h_od = od[:, :, 0] - 0.15 * od[:, :, 1] - 0.15 * od[:, :, 2]

    # Detect tissue pixels (exclude bright glass background)
    tissue_mask = (tile_rgb[:, :, 0] < 235) | (tile_rgb[:, :, 1] < 235) | (tile_rgb[:, :, 2] < 235)
    if not np.any(tissue_mask):
        return []

    tissue_h_od = h_od[tissue_mask]

    # Adaptive chromatin threshold:
    # Mitotic chromosomes have significantly higher optical density than interphase nuclei in the same tile.
    # For lightly stained tissue (p85 ~ 0.60): threshold is max(0.92, 0.60 + 0.14) = 0.92.
    # For darkly stained tissue (p85 ~ 1.05): threshold is max(0.92, 1.05 + 0.14) = 1.19.
    # This prevents thousands of resting interphase nuclei from being false-alarmed on hyperchromatic slides.
    p85_od = float(np.percentile(tissue_h_od, 85))
    chromatin_thresh = max(0.92, p85_od + 0.14)
    dense_mask = (h_od > chromatin_thresh).astype(np.uint8) * 255

    try:
        import cv2
        cnts, _ = cv2.findContours(dense_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for cnt in cnts:
            area = float(cv2.contourArea(cnt))
            # Mitotic chromatin clusters typically occupy 400 to 3800 pixels (equivalent diameter ~22-70 px / 5.5-17.5 um)
            if not (400 <= area <= 3800):
                continue

            perim = float(cv2.arcLength(cnt, True))
            if perim <= 0:
                continue

            circ = float((4.0 * np.pi * area) / (perim * perim))
            equiv_diam = float(np.sqrt(4.0 * area / np.pi))

            # Calculate local peak OD within contour using bounding-box sub-mask
            bx, by, bw, bh = cv2.boundingRect(cnt)
            sub_mask = np.zeros((bh, bw), dtype=np.uint8)
            cnt_shifted = cnt - [bx, by]
            cv2.drawContours(sub_mask, [cnt_shifted], -1, 255, -1)
            sub_od = h_od[by:by + bh, bx:bx + bw]
            pixels_inside = sub_od[sub_mask > 0]
            if len(pixels_inside) == 0:
                continue

            p95_od = float(np.percentile(pixels_inside, 95))

            # Exclude small round bodies (lymphocytes and apoptotic fragments)
            if equiv_diam < 28.0 and circ > 0.62:
                continue

            # Bona fide mitotic chromosomes require high peak optical density
            if p95_od < 1.00:
                continue

            M = cv2.moments(cnt)
            if M["m00"] > 0:
                cx = float(M["m10"] / M["m00"])
                cy = float(M["m01"] / M["m00"])

                # Mitotic saliency score based on chromatin condensation and contrast
                contrast = p95_od - chromatin_thresh
                raw_conf = 0.30 + min(0.35, max(0.0, contrast * 0.40)) + min(0.15, (area / 1500.0) * 0.15)
                conf = float(np.clip(raw_conf, 0.10, 0.80))
                if conf >= conf_threshold:
                    candidates.append((cx, cy, conf, contrast))

        # Apply intra-tile NMS (radius 80 px = 20 um at 0.25 um/px)
        # Sort by contrast and confidence
        candidates.sort(key=lambda c: (c[3], c[2]), reverse=True)
        suppressed = []
        for c in candidates:
            if not any(math.hypot(c[0] - s[0], c[1] - s[1]) < 80.0 for s in suppressed):
                suppressed.append((c[0], c[1], c[2]))

        # Keep top candidates per tile to prevent runaway false candidates on dense sheets
        # In breast cancer hotspots, 3-5 mitoses per 40x tile is clinically high grade
        max_per_tile = max_candidates_per_tile or 4
        if len(suppressed) > max_per_tile:
            return suppressed[:max_per_tile]
        return suppressed
    except ImportError:
        # Fallback if OpenCV is not available
        stride = 48
        candidates = []
        for y in range(stride // 2, h - stride // 2, stride):
            for x in range(stride // 2, w - stride // 2, stride):
                patch = h_od[y - stride // 2 : y + stride // 2, x - stride // 2 : x + stride // 2]
                max_val = float(np.max(patch))
                if max_val > chromatin_thresh:
                    py, px = np.unravel_index(np.argmax(patch), patch.shape)
                    actual_x = float(x - stride // 2 + px)
                    actual_y = float(y - stride // 2 + py)
                    raw_conf = 0.25 + (max_val - chromatin_thresh) * 0.50
                    conf = float(np.clip(raw_conf, 0.05, 0.95))
                    if conf >= conf_threshold:
                        candidates.append((actual_x, actual_y, conf))
        candidates.sort(key=lambda c: c[2], reverse=True)
        suppressed = []
        for c in candidates:
            if not any(math.hypot(c[0] - s[0], c[1] - s[1]) < 80.0 for s in suppressed):
                suppressed.append(c)
        if max_candidates_per_tile is not None and len(suppressed) > max_candidates_per_tile:
            return suppressed[:max_candidates_per_tile]
        return suppressed
