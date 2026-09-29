"""
OncoGemma Stage v4.3 - Pure Nottingham Mitotic Scoring Engine.
Computes point-in-circle containment of mitotic figures within virtual HPFs,
calculates standardized area-normalized density (mitoses/mm²), and assigns
Elston-Ellis Nottingham Mitotic Scores (Score 1, 2, or 3).

Thresholds come from the injected ``MitosisScoringConfig`` (configs/mitosis.yaml, SPEC-01 §3.8).
"""
import math
from typing import List, Dict, Any, Tuple, Optional

from app.core.pipeline_config import MitosisScoringConfig


def calculate_hpf_mitosis_counts(
    candidates: List[Dict[str, Any]],
    hpfs: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Computes which mitotic candidates fall inside each virtual HPF circle.
    Updates the 'count' field on each HPF and returns updated HPF list + total count.
    
    A candidate is considered a confirmed mitosis if label == "mitosis".
    """
    updated_hpfs = []
    mitoses_in_any_hpf = set()

    for hpf in hpfs:
        hpf_copy = dict(hpf)
        cx, cy = hpf_copy["center_um"]
        r = float(hpf_copy["radius_um"])
        r_sq = r * r

        hpf_mitosis_count = 0
        for cand in candidates:
            if cand.get("label") != "mitosis":
                continue

            cand_x, cand_y = cand["centroid_um"]
            dist_sq = (cand_x - cx) ** 2 + (cand_y - cy) ** 2
            if dist_sq <= r_sq:
                hpf_mitosis_count += 1
                mitoses_in_any_hpf.add(cand.get("id"))

        hpf_copy["count"] = hpf_mitosis_count
        updated_hpfs.append(hpf_copy)

    total_count = len(mitoses_in_any_hpf)
    return updated_hpfs, total_count


def compute_nottingham_mitotic_score(
    count_total: int,
    n_hpf: int,
    radius_um: Optional[float],
    scoring: MitosisScoringConfig,
    hpfs: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Computes Nottingham Mitotic Score based on standardized mm² area normalization.

    ``scoring.thresholds`` give the mitoses/mm² at which the score becomes 2 and 3
    (Elston-Ellis: 3.65 and 7.30 per mm², classic 10 and 20 per 2.74 mm²).
    With ``hpfs`` the area is the sum of their own radii; otherwise ``n_hpf`` fields of ``radius_um``.
    """
    score2_min = scoring.thresholds.score2_min
    score3_min = scoring.thresholds.score3_min

    # Calculate actual cumulative HPF inspection area summing per-HPF radius (#764)
    if hpfs:
        n_fields = len(hpfs)
        area_mm2 = sum(math.pi * ((float(h["radius_um"]) / 1000.0) ** 2) for h in hpfs)
    elif n_hpf > 0:
        n_fields = n_hpf
        single_hpf_area_mm2 = math.pi * ((radius_um / 1000.0) ** 2)
        area_mm2 = float(n_fields * single_hpf_area_mm2)
    else:
        n_fields, area_mm2 = 0, 0.0

    # Clean zero-HPF state handling (#373)
    if n_fields <= 0 or area_mm2 <= 0.0:
        return {
            "count_total": int(count_total),
            "n_hpf": 0,
            "area_mm2": 0.0,
            "per_mm2": 0.0,
            "mitoses_per_mm2": 0.0,
            "classic_per_10hpf": 0.0,
            "mitotic_score": 1,
            "score": 1
        }

    density = float(count_total) / area_mm2
    classic_per_10hpf = density * scoring.classic_area_mm2

    # Determine score
    if density >= score3_min:
        mitotic_score = 3
    elif density >= score2_min:
        mitotic_score = 2
    else:
        mitotic_score = 1

    return {
        "count_total": int(count_total),
        "n_hpf": int(n_fields),
        "area_mm2": round(area_mm2, 3),
        "per_mm2": round(density, 2),
        "mitoses_per_mm2": round(density, 2),
        "classic_per_10hpf": round(classic_per_10hpf, 1),
        "mitotic_score": mitotic_score,
        "score": mitotic_score
    }
