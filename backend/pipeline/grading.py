"""
Pure Zero-LLM Nottingham Histologic Grading Aggregation Engine.

All arithmetic, median/mode voting, tie-breaking, and Nottingham grade synthesis
are strictly calculated in pure deterministic Python code. The LLM never computes
any numbers or aggregates.

Thresholds and weights come from the injected ``ScoringConfig`` (configs/scoring.yaml) and
mitotic thresholds from ``MitosisScoringConfig`` (configs/mitosis.yaml), SPEC-01 §3.8.
"""

from typing import List, Dict, Any, Tuple, Optional

from app.core.pipeline_config import MitosisScoringConfig, ScoringConfig


def weighted_median(values: List[float], weights: List[float]) -> float:
    """
    Compute deterministic weighted median for continuous values.
    
    Args:
        values: List of numeric values (e.g., tubule percentages).
        weights: Corresponding positive weights.
        
    Returns:
        Weighted median value as float.
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    
    # Pair and sort by value ascending
    paired = sorted(zip(values, weights), key=lambda x: x[0])
    total_weight = sum(w for _, w in paired)
    if total_weight <= 0:
        return float(values[len(values) // 2])
    
    half_weight = total_weight / 2.0
    cumulative_weight = 0.0
    
    for i, (val, w) in enumerate(paired):
        cumulative_weight += w
        if cumulative_weight >= half_weight:
            # Check for exact midpoint tie across distinct values
            if cumulative_weight == half_weight and i + 1 < len(paired):
                return float((val + paired[i + 1][0]) / 2.0)
            return float(val)
            
    return float(paired[-1][0])


def weighted_mode(values: List[int], weights: List[float], tie_breaker=max) -> Tuple[Optional[int], float]:
    """
    Compute deterministic weighted mode for discrete categories (e.g. pleomorphism scores 1, 2, 3).
    Ties resolve via tie_breaker (default max: conservative clinical rule favoring worse grade).
    
    Args:
        values: List of discrete scores (e.g. [1, 2, 3]).
        weights: Corresponding positive weights.
        tie_breaker: Function to resolve ties among candidate scores with equal max weight.
        
    Returns:
        Tuple of (winning_score, disagreement_ratio). If values is empty, returns (None, 1.0).
    """
    if not values:
        return None, 1.0
        
    weight_totals: Dict[int, float] = {}
    for val, w in zip(values, weights):
        weight_totals[val] = weight_totals.get(val, 0.0) + w
        
    total_weight = sum(weights)
    if total_weight <= 0:
        return tie_breaker(values), 0.0
        
    max_w = max(weight_totals.values())
    candidates = [val for val, w in weight_totals.items() if abs(w - max_w) < 1e-9]
    
    winning_score = tie_breaker(candidates)
    winning_weight = weight_totals[winning_score]
    disagreement_ratio = 1.0 - (winning_weight / total_weight)
    
    return winning_score, disagreement_ratio


def calculate_tubule_score(tubule_percent: float, cfg: ScoringConfig) -> int:
    """
    Map tubule formation percentage to Elston-Ellis Nottingham score
    (configured thresholds; Elston-Ellis: > 75% is 1, 10% - 75% is 2, < 10% is 3).
    """
    thresholds = cfg.tubule_formation.thresholds
    score1_min = thresholds.score1_min_percent
    score2_min = thresholds.score2_min_percent

    if tubule_percent > score1_min:
        return 1
    elif tubule_percent >= score2_min:
        return 2
    else:
        return 3


def calculate_nottingham_grade(
    tubule_score: int,
    pleo_score: int,
    mitotic_score: int,
    cfg: ScoringConfig
) -> Tuple[int, int]:
    """
    Calculate Nottingham sum and final Nottingham Histological Grade
    (configured sum bounds; Elston-Ellis: 3-5 is Grade 1, 6-7 Grade 2, 8-9 Grade 3).

    Returns:
        (nottingham_sum, grade)
    """
    nottingham_sum = tubule_score + pleo_score + mitotic_score

    g1_max = cfg.nottingham_grading.grade1_max_sum
    g2_max = cfg.nottingham_grading.grade2_max_sum

    if nottingham_sum <= g1_max:
        grade = 1
    elif nottingham_sum <= g2_max:
        grade = 2
    else:
        grade = 3
        
    return nottingham_sum, grade


def validate_grading_invariants(
    tubule_score: int,
    pleo_score: int,
    mitotic_score: int,
    nottingham_sum: int,
    grade: int,
    cfg: ScoringConfig
) -> None:
    """
    The v3/v4 Guard: Ensure all mathematical invariants hold strictly before DB write.
    Dynamically respects configured Nottingham boundaries.
    Raises ValueError if any invariant is violated.
    """
    for name, val in [("tubule_score", tubule_score), ("pleo_score", pleo_score), ("mitotic_score", mitotic_score)]:
        if val not in (1, 2, 3):
            raise ValueError(f"Invariant Violation: {name} must be in [1, 2, 3], got {val}")
            
    expected_sum = tubule_score + pleo_score + mitotic_score
    if nottingham_sum != expected_sum:
        raise ValueError(f"Invariant Violation: nottingham_sum ({nottingham_sum}) != sum of sub-scores ({expected_sum})")
        
    g1_max = cfg.nottingham_grading.grade1_max_sum
    g2_max = cfg.nottingham_grading.grade2_max_sum

    expected_grade = 1 if expected_sum <= g1_max else (2 if expected_sum <= g2_max else 3)
    if grade != expected_grade:
        raise ValueError(f"Invariant Violation: grade ({grade}) does not match expected Nottingham Grade ({expected_grade}) for sum {expected_sum}")


def calculate_mitotic_score_from_hpfs(
    hpf_mitotic_counts: List[int],
    scoring: MitosisScoringConfig,
    radius_um: float
) -> Tuple[int, int]:
    """
    Calculate total mitoses and Nottingham Mitotic Score (1, 2, or 3) across HPFs of
    ``radius_um`` using area-normalized density (mitoses/mm²) and ``scoring.thresholds``.

    Returns:
        (total_mitoses, mitotic_score)
    """
    total_mitoses = sum(hpf_mitotic_counts)
    from pipeline.scoring import compute_nottingham_mitotic_score
    summary = compute_nottingham_mitotic_score(
        count_total=total_mitoses,
        n_hpf=len(hpf_mitotic_counts),
        radius_um=radius_um,
        scoring=scoring
    )
    return total_mitoses, summary["mitotic_score"]


def calculate_mitotic_score_from_detections_and_hpfs(
    detections: List[Dict[str, Any]],
    hpfs: List[Dict[str, Any]],
    scoring: MitosisScoringConfig
) -> Tuple[int, int]:
    """
    Calculate total mitoses and Nottingham Mitotic Score (1, 2, or 3) across virtual HPFs,
    ensuring that mitoses falling inside overlapping HPF circles are counted ONCE (no double counting).
    Uses standardized area-normalized density (mitoses/mm²).
    
    Returns:
        (unique_total_mitoses, mitotic_score)
    """
    from pipeline.scoring import calculate_hpf_mitosis_counts, compute_nottingham_mitotic_score
    updated_hpfs, unique_total = calculate_hpf_mitosis_counts(detections, hpfs)
    # Each HPF's own radius gives the area; with no HPFs the score is the zero-field state.
    summary = compute_nottingham_mitotic_score(
        count_total=unique_total,
        n_hpf=len(updated_hpfs),
        radius_um=None,
        scoring=scoring,
        hpfs=updated_hpfs
    )
    return unique_total, summary["mitotic_score"]


def aggregate_grading_findings(
    tubule_responses: List[Dict[str, Any]],
    pleo_responses: List[Dict[str, Any]],
    mitotic_score: Optional[int],
    cfg: ScoringConfig
) -> Dict[str, Any]:
    """
    Full end-to-end pure code aggregation pipeline.
    Handles empty evidence sets gracefully with needs_human flag rather than fabricating scores.
    
    Args:
        tubule_responses: List of per-patch dicts with {tubule_percent, tumor_present, confidence, [user_tubule_percent], [user_tumor_present]}
        pleo_responses: List of per-patch dicts with {pleomorphism_score, rationale, confidence, [user_pleo_score]}
        mitotic_score: Confirmed mitotic score (1, 2, or 3), or None when there is none (needs_human)
        cfg: The scoring configuration (configs/scoring.yaml)
        
    Returns:
        Dict containing:
            tubule_percent, tubule_score, pleo_score, mitotic_score,
            nottingham_sum, grade, flags, patch_counts
    """
    weights = cfg.grading.confidence_weights
    min_tumor_patches = cfg.grading.min_tumor_patches
    max_disp = cfg.grading.max_disp

    def _weight(r: Dict[str, Any], user_key: str) -> float:
        # A pathologist's value gets the highest weight. A model estimate weighs as its stated
        # confidence; the v6 estimators state none, so theirs weigh as "medium".
        if r.get(user_key) is not None:
            return weights.high
        confidence = r.get("confidence")
        return weights.medium if confidence is None else getattr(weights, str(confidence).lower())

    # 0. Gracefully handle empty evidence sets without fabricating scores (#366)
    if not tubule_responses or not pleo_responses:
        return {
            "tubule_percent": None,
            "tubule_score": None,
            "pleo_score": None,
            "mitotic_score": mitotic_score,
            "nottingham_sum": None,
            "grade": None,
            "flags": ["empty_evidence_set", "needs_human"],
            "tumor_patch_count": 0,
            "total_patch_count": max(len(tubule_responses), len(pleo_responses)),
            "pleo_dispersion": 1.0,
            "needs_human": True
        }

    # 1. Filter tumor-containing patches for Tubule assessment (accounting for pathologist overrides).
    # A patch whose estimate failed (None) and has no pathologist value is left out, never defaulted.
    def _effective(r: Dict[str, Any], user_key: str, model_key: str) -> Any:
        return r.get(user_key) if r.get(user_key) is not None else r.get(model_key)

    tumor_tubule = [
        r for r in tubule_responses
        if _effective(r, "user_tumor_present", "tumor_present") and _effective(r, "user_tubule_percent", "tubule_percent") is not None
    ]

    if tumor_tubule:
        tubule_vals = [float(_effective(r, "user_tubule_percent", "tubule_percent")) for r in tumor_tubule]
        # Pathologist-reviewed/modified patches receive highest confidence weight
        tubule_w = [_weight(r, "user_tubule_percent") for r in tumor_tubule]
        derived_tubule_percent = round(weighted_median(tubule_vals, tubule_w), 1)
        tubule_score = calculate_tubule_score(derived_tubule_percent, cfg)
    else:
        derived_tubule_percent = None
        tubule_score = None
        
    # 2. Pleomorphism mode calculation across all valid responses (accounting for pathologist overrides)
    assessed_pleo = [r for r in pleo_responses if _effective(r, "user_pleo_score", "pleomorphism_score") is not None]
    pleo_vals = [int(_effective(r, "user_pleo_score", "pleomorphism_score")) for r in assessed_pleo]
    pleo_w = [_weight(r, "user_pleo_score") for r in assessed_pleo]

    pleo_score, pleo_dispersion = weighted_mode(pleo_vals, pleo_w, tie_breaker=max)

    # 3. If no tumor patches exist, pleo could not be assessed or there is no mitotic score,
    # flag for human review
    if tubule_score is None or pleo_score is None or mitotic_score is None:
        flags: List[str] = ["needs_human"]
        if mitotic_score is None:
            flags.append("no_mitotic_score")
        if len(tumor_tubule) == 0:
            flags.append("no_tumor_patches")
        elif len(tumor_tubule) < min_tumor_patches:
            flags.append("insufficient_tumor_patches")
        if pleo_dispersion > max_disp:
            flags.append("pleo_high_variance")

        return {
            "tubule_percent": derived_tubule_percent,
            "tubule_score": tubule_score,
            "pleo_score": pleo_score,
            "mitotic_score": mitotic_score,
            "nottingham_sum": None,
            "grade": None,
            "flags": flags,
            "tumor_patch_count": len(tumor_tubule),
            "total_patch_count": max(len(tubule_responses), len(pleo_responses)),
            "pleo_dispersion": round(pleo_dispersion, 3),
            "needs_human": True
        }

    # 4. Overall Nottingham Grade Calculation
    nottingham_sum, grade = calculate_nottingham_grade(tubule_score, pleo_score, mitotic_score, cfg)
    
    # 5. Quality & Consistency Flags
    flags: List[str] = []
    if len(tumor_tubule) < min_tumor_patches:
        flags.append("insufficient_tumor_patches")
    if pleo_dispersion > max_disp:
        flags.append("pleo_high_variance")
        
    # Validate invariants before returning
    validate_grading_invariants(tubule_score, pleo_score, mitotic_score, nottingham_sum, grade, cfg=cfg)
    
    return {
        "tubule_percent": derived_tubule_percent,
        "tubule_score": tubule_score,
        "pleo_score": pleo_score,
        "mitotic_score": mitotic_score,
        "nottingham_sum": nottingham_sum,
        "grade": grade,
        "flags": flags,
        "tumor_patch_count": len(tumor_tubule),
        "total_patch_count": max(len(tubule_responses), len(pleo_responses)),
        "pleo_dispersion": round(pleo_dispersion, 3)
    }
