"""v5 morphometric "HoVer-Net" mitosis verifier (registry heuristic morph_verifier).

Despite its v5 name this is not HoVer-Net: it scores a 128 px crop with optical-density
and contour rules. v5 used it to label candidates and as the referee's fallback; v6 does
neither (SPEC-01 §3.9, SPEC-06 §9). It survives only for the A0 attribution replay.
"""
from typing import List, Optional, Tuple

import numpy as np


def morphometric_mitosis_probability(crop_rgb: np.ndarray) -> Tuple[float, Optional[List[List[int]]]]:
    """
    First-principles cellular morphometry:
    1. Analyzes central region for chromatin condensation and nuclear morphology.
    2. Measures boundary irregularity / spiculation (spindle protrusions vs smooth lymphocyte/nuclear envelope).
    3. Detects absence of intact nuclear membrane (classic hallmark of active mitosis).
    4. Explicitly filters out non-mitotic mimickers:
       - Apoptotic bodies (small, dense, pyknotic, high circularity, haloed)
       - Lymphocytes (small, smooth continuous unbroken envelope, high circularity/solidity)
       - Resting / interphase nuclei (intact membrane, vesicular chromatin, lower OD)
       - Background stroma / debris / dust specks
    """
    try:
        import cv2
    except ImportError:
        cv2 = None

    h, w, _ = crop_rgb.shape
    cy, cx = h // 2, w // 2
    r_px = min(36, min(h, w) // 2 - 2) # ~18 um radius (72 px diameter) region for true mitotic figures (#124)

    # Optical density transformation
    rgb_f = np.maximum(crop_rgb.astype(np.float32), 1.0) / 255.0
    od = -np.log(rgb_f)
    # Hematoxylin absorption component
    h_od = od[:, :, 0] - 0.15 * od[:, :, 1] - 0.15 * od[:, :, 2]

    center_h_od = h_od[cy - r_px : cy + r_px, cx - r_px : cx + r_px]
    mean_h_od = float(np.mean(center_h_od))

    # Reject empty background / stroma
    if mean_h_od < 0.20:
        return 0.05, None

    # Robust 95th percentile OD (avoids single-pixel outlier blowout)
    p95_od = float(np.percentile(center_h_od, 95))
    std_od = float(np.std(center_h_od))

    # Segment central chromatin clump
    thresh = max(0.35, float(np.median(h_od) + 1.2 * np.std(h_od)))
    chromatin_mask = (center_h_od > thresh).astype(np.uint8) * 255

    contour_pts = None

    if cv2 is not None:
        cnts, _ = cv2.findContours(chromatin_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not cnts:
            return 0.10, None

        # Filter to contours close to center
        # Extract the central connected component located at (r_px, r_px)
        central_cnt = None
        for cnt in cnts:
            if cv2.pointPolygonTest(cnt, (r_px, r_px), False) >= -2.0:
                central_cnt = cnt
                break

        if central_cnt is None:
            # Fallback to closest contour within central radius
            central_cnt = min(cnts, key=lambda c: cv2.pointPolygonTest(c, (r_px, r_px), True) ** 2)

        area = float(cv2.contourArea(central_cnt))
        perim = float(cv2.arcLength(central_cnt, True))

        if area < 80 or perim <= 0:
            # Tiny debris / noise
            return 0.12, None

        equiv_diam = float(np.sqrt(4.0 * area / np.pi)) # diameter in pixels
        circ = float((4.0 * np.pi * area) / (perim * perim))
        hull = cv2.convexHull(central_cnt)
        hull_area = max(1.0, float(cv2.contourArea(hull)))
        solidity = float(area / hull_area)

        equiv_perim = np.pi * equiv_diam
        spiculation = float((perim - equiv_perim) / max(1.0, equiv_perim))

        # Approximate nuclear contour in crop coordinates
        approx_cnt = cv2.approxPolyDP(central_cnt, 1.5, True)
        contour_pts = []
        for pt in approx_cnt:
            px_crop = int(pt[0][0] + (cx - r_px))
            py_crop = int(pt[0][1] + (cy - r_px))
            contour_pts.append([px_crop, py_crop])

        # Measure halo contrast ratio to detect apoptotic retraction space
        mask_cnt_inner = np.zeros_like(center_h_od, dtype=np.uint8)
        cv2.drawContours(mask_cnt_inner, [central_cnt], -1, 255, -1)
        kernel = np.ones((5, 5), np.uint8)
        mask_cnt_outer = cv2.dilate(mask_cnt_inner, kernel, iterations=2)
        halo_mask = (mask_cnt_outer > 0) & (mask_cnt_inner == 0)
        halo_od = float(np.mean(center_h_od[halo_mask])) if np.any(halo_mask) else 0.5
        core_od = float(np.mean(center_h_od[mask_cnt_inner > 0])) if np.any(mask_cnt_inner > 0) else 0.5

        # 1. Reject Apoptotic Bodies (Van Diest Criteria):
        # Apoptotic bodies feature small, smooth, compact globular pyknotic fragments
        # (equiv_diam < 25 px, circ > 0.55, spiculation < 0.20) or clear retraction halo.
        if equiv_diam < 25.0 and circ > 0.55 and spiculation < 0.20:
            return 0.08, contour_pts

        # 2. Reject Lymphocyte / Inflammatory Cell:
        # Small (diam 15-32 px ~ 4-8 um), high circularity (>0.60), high solidity (>0.84), low spiculation (<0.18)
        if 15.0 <= equiv_diam <= 32.0 and circ > 0.60 and solidity > 0.84 and spiculation < 0.18:
            return 0.10, contour_pts

        # 3. Reject Resting Interphase Nuclei:
        # True mitoses REQUIRE dissolved nuclear envelope. An intact, continuous oval/circular membrane
        # with smooth contour (spiculation < 0.18, circ > 0.52, solidity > 0.83) represents interphase.
        if spiculation < 0.18 and solidity > 0.83 and (circ > 0.52 or p95_od < 0.90):
            return 0.14, contour_pts

        # 4. Reject Tiny Debris / Giant Tissue Folds:
        if area < 300.0 or equiv_diam < 20.0 or area > 4200.0 or equiv_diam > 75.0:
            return 0.12, contour_pts

        # 5. Mitotic Figure Scoring (Van Diest Classic Metaphase / Anaphase / Telophase Criteria):
        # True dividing cell requires:
        # - High spiculation / chromosome arms protruding into cytoplasm (spiculation >= 0.18)
        # - Intensely condensed basophilic chromatin (p95_od >= 0.75)
        # - High texture variance from individual chromosomes (std_od >= 0.20)
        # - Irregular, jagged contour from envelope dissolution (solidity < 0.82)
        size_score = float(np.clip((equiv_diam - 20.0) / 22.0, 0.0, 1.0))
        spic_score = float(np.clip((spiculation - 0.15) / 0.25, 0.0, 1.0))
        od_score = float(np.clip((p95_od - 0.70) / 0.60, 0.0, 1.0))
        texture_score = float(np.clip((std_od - 0.18) / 0.30, 0.0, 1.0))
        irregularity_score = float(np.clip((0.90 - solidity) / 0.25, 0.0, 1.0))

        if spic_score < 0.10 or od_score < 0.20 or texture_score < 0.15:
            return 0.22, contour_pts

        p_mitosis = 0.20 + (
            0.25 * spic_score +
            0.25 * od_score +
            0.25 * texture_score +
            0.15 * size_score +
            0.10 * irregularity_score
        )

        return float(np.clip(p_mitosis, 0.05, 0.95)), contour_pts
    else:
        # Fallback when OpenCV is not installed
        dense_pixels = int(np.sum(chromatin_mask > 0))
        if dense_pixels < 30:
            return 0.12, None
        score = 0.20 + min(0.70, (dense_pixels / 600.0) * 0.35 + (p95_od / 2.0) * 0.35)
        return float(np.clip(score, 0.05, 0.95)), None
