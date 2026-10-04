"""
Stage 5 aggregation in deterministic code (SPEC-07 §5.2, §7.1; WP-8.6). No model computes a number.

- T% is the area-weighted mean of the tubule percentages over the samples with tumour present,
  weighted by each sample's tumour area; a pathologist's value replaces the estimate.
- P is the mode of the field scores; a tie takes ``scoring.grading.pleo_tie_break`` (owner
  decision 2026-10-04: the highest).
- M comes from Stage 4 through ``pipeline/scoring.py`` and is passed in.
- The grade exists only when T, P and M all exist. There are no confidence weights.

Thresholds come from the injected ``ScoringConfig`` (configs/scoring.yaml), SPEC-01 §3.8.
"""

from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.pipeline_config import ScoringConfig

# Marks a gradings.machine written by the v6 worker; anything else is a v5 grading (404 in the API).
MACHINE_SCHEMA = "grading_v6"


def sample_blob(case_id: str, kind: str, sample_id: str) -> str:
    """The stored image of a Stage 5 sample: ``kind`` is ``tubule`` or ``pleo``."""
    return f"cases/{case_id}/grading/{kind}/{sample_id}.png"


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


def tubule_percent_area_weighted(samples: Sequence[Dict[str, Any]]) -> Tuple[Optional[float], int]:
    """T% over the samples with ``tumor_present`` and a percentage, weighted by ``tumor_area_um2``.

    Each sample carries its effective ``tumor_present`` and ``tubule_percent`` (review, else
    estimate). A sample with no tumour area inside the box has no weight. Returns (T% or None, n used).
    """
    used = [
        s for s in samples
        if s.get("tumor_present") is True and s.get("tubule_percent") is not None and s["tumor_area_um2"] > 0
    ]
    total_area = sum(s["tumor_area_um2"] for s in used)
    if not used or total_area <= 0:
        return None, 0
    percent = sum(s["tumor_area_um2"] * float(s["tubule_percent"]) for s in used) / total_area
    return round(percent, 1), len(used)


def pleomorphism_mode(scores: Sequence[int], tie_break: str) -> Tuple[Optional[int], bool]:
    """The mode of the field scores and whether it was a tie (resolved by ``tie_break``: max or min)."""
    if not scores:
        return None, False
    counts = Counter(scores)
    top = max(counts.values())
    tied = sorted(score for score, n in counts.items() if n == top)
    pick = tied[-1] if tie_break == "max" else tied[0]
    return pick, len(tied) > 1


def near_grade_boundary(total: int, cfg: ScoringConfig) -> bool:
    """The sum lies on either side of a grade cut-off (5/6 or 7/8 with the Elston-Ellis bands)."""
    g = cfg.nottingham_grading
    return total in {g.grade1_max_sum, g.grade1_max_sum + 1, g.grade2_max_sum, g.grade2_max_sum + 1}


def aggregate_components(
    tubule_score: Optional[int], pleo_score: Optional[int], mitotic_score: Optional[int], cfg: ScoringConfig
) -> Dict[str, Any]:
    """Total, grade and the boundary flag; all None unless the three components exist (SPEC-07 §7.1)."""
    if tubule_score is None or pleo_score is None or mitotic_score is None:
        return {"total": None, "grade": None, "near_grade_boundary": False}
    total, grade = calculate_nottingham_grade(tubule_score, pleo_score, mitotic_score, cfg)
    validate_grading_invariants(tubule_score, pleo_score, mitotic_score, total, grade, cfg=cfg)
    return {"total": total, "grade": grade, "near_grade_boundary": near_grade_boundary(total, cfg)}


def _effective(sample: Dict[str, Any], review: Optional[Dict[str, Any]], key: str) -> Any:
    """The pathologist's value when the review sets ``key``, else the estimate's (None if it failed)."""
    if review and review.get(key) is not None:
        return review[key]
    estimate = sample.get("estimate")
    return estimate.get(key) if estimate else None


def stage5_result(machine: Dict[str, Any], overrides: Dict[str, Any], cfg: ScoringConfig) -> Dict[str, Any]:
    """Scores, grade and flags of a v6 grading from its machine output and the pathologist's edits.

    ``machine`` is never changed. ``overrides`` holds ``reviews`` ({"tubule"|"pleo": {sample_id: review}}),
    component overrides (``tubule_score``, ``pleo_score``, ``histotype``) and their ``reasons``.
    """
    reviews = overrides.get("reviews", {})
    t_reviews, p_reviews = reviews.get("tubule", {}), reviews.get("pleo", {})
    samples = machine["tubule"]["samples"]
    fields = machine["pleomorphism"]["fields"]

    effective_t = [
        {"tumor_present": _effective(s, t_reviews.get(s["id"]), "tumor_present"),
         "tubule_percent": _effective(s, t_reviews.get(s["id"]), "tubule_percent"),
         "tumor_area_um2": s["tumor_area_um2"]}
        for s in samples
    ]
    tubule_percent, n_used = tubule_percent_area_weighted(effective_t)
    tubule_score = calculate_tubule_score(tubule_percent, cfg) if tubule_percent is not None else None

    pleo_scores = [
        v for v in (_effective(f, p_reviews.get(f["id"]), "pleomorphism_score") for f in fields) if v is not None
    ]
    pleo_score, pleo_tie = pleomorphism_mode(pleo_scores, cfg.grading.pleo_tie_break)
    mitotic_score = machine["mitotic"]["score"]

    eff_t = overrides.get("tubule_score", tubule_score)
    eff_p = overrides.get("pleo_score", pleo_score)
    combined = aggregate_components(eff_t, eff_p, mitotic_score, cfg)

    unreviewed_failures = (
        any(s["estimate"] is None and s["id"] not in t_reviews for s in samples)
        or any(f["estimate"] is None and f["id"] not in p_reviews for f in fields)
    )
    needs_human = (
        unreviewed_failures
        or combined["grade"] is None
        or bool(set(machine.get("flags", [])) & {"needs_human"})
    )
    flags: List[str] = []
    if needs_human:
        flags.append("needs_human")
    if combined["near_grade_boundary"]:
        flags.append("near_grade_boundary")
    flags += [f for f in machine["mitotic"].get("flags", []) if f not in flags]
    return {
        "tubule_percent": tubule_percent,
        "tubule_score": tubule_score,
        "n_used": n_used,
        "pleo_score": pleo_score,
        "pleo_tie": pleo_tie,
        "mitotic_score": mitotic_score,
        "effective_tubule_score": eff_t,
        "effective_pleo_score": eff_p,
        "total": combined["total"],
        "grade": combined["grade"],
        "flags": flags,
    }
