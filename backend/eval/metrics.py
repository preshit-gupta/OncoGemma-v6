"""
Evaluation Metrics Module for OncoGemma v6.

Normative definitions from SPEC-00 §2 and SPEC-02 §7:
- SPEC-00 §2.1: Primary detection F1 (Hungarian matching within radius_um)
- SPEC-00 §2.2: Band-specific accuracy and Nottingham grade metrics
- SPEC-00 §2.3: Quadratic Weighted Kappa (QWK)
- SPEC-00 §2.4: Bootstrap confidence intervals and statistical testing
- SPEC-00 §2.5: Macro F1 over clinical grades with missingness penalties

Pure functions only: no I/O, no network, and no global state.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable, Sequence

import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.stats import binom
from sklearn.metrics import cohen_kappa_score


@dataclass(frozen=True)
class MatchResult:
    """1:1 Hungarian matching result (SPEC-00 §2.1)."""
    pairs: list[tuple[int, int]]    # (gt_index, pred_index) of matched pairs
    tp: int
    fp: int
    fn: int


@dataclass(frozen=True)
class PRF:
    """Precision, Recall, and F1 counts and scores (SPEC-00 §2.1)."""
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float   # math.nan when denominator is 0


@dataclass
class RegionPoints:
    """Spatial points and prediction confidence scores for an image region."""
    gt_um: np.ndarray                         # (n, 2) micrometres
    pred_um: np.ndarray                       # (m, 2)
    pred_scores: np.ndarray | None = None     # (m,) final scores, required for thresholds and PR curves


@dataclass
class CaseMitosis:
    """Case-level container of evaluated regions."""
    case_id: str
    regions: list[RegionPoints]


@dataclass(frozen=True)
class PRCurve:
    """Precision-Recall curve and Average Precision (SPEC-00 §2.1)."""
    thresholds: np.ndarray    # unique prediction scores, descending
    precision: np.ndarray     # at each threshold (predictions with score >= t)
    recall: np.ndarray
    f1: np.ndarray
    ap: float                 # sum_k (R_k - R_{k-1}) * P_k, with R_0 = 0


@dataclass(frozen=True)
class MacroF1:
    """Macro-averaged F1 score over present classes with coverage tracking (SPEC-00 §2.5)."""
    per_class: dict[int, float]   # classes present in y_true or y_pred (never 'none')
    macro: float
    coverage: float               # fraction of cases with a non-'none' prediction
    n: int


@dataclass(frozen=True)
class BandMetrics:
    """Targeted Nottingham band-level performance metrics (SPEC-00 §2.2)."""
    f1_high: float
    macro_f1_lm: float
    sum_mae: float
    n_high: int
    n_lm: int


@dataclass(frozen=True)
class CI:
    """Bootstrap confidence interval (SPEC-00 §2.4)."""
    point: float
    low: float
    high: float
    B: int
    seed: int


@dataclass(frozen=True)
class DeltaCI:
    """Paired bootstrap confidence interval for difference delta = b - a (SPEC-00 §2.4)."""
    delta: float
    low: float
    high: float
    B: int
    seed: int


def match_points(
    gt_um: np.ndarray,
    pred_um: np.ndarray,
    radius_um: float = 7.5
) -> MatchResult:
    """
    Perform 1:1 Hungarian matching between GT and predicted points within radius_um (SPEC-00 §2.1).
    Distance <= radius_um is considered a valid match (inclusive).
    """
    n = len(gt_um)
    m = len(pred_um)

    if n == 0 and m == 0:
        return MatchResult(pairs=[], tp=0, fp=0, fn=0)
    if n == 0:
        return MatchResult(pairs=[], tp=0, fp=m, fn=0)
    if m == 0:
        return MatchResult(pairs=[], tp=0, fp=0, fn=n)

    # Compute Euclidean distance matrix (n, m)
    diff = gt_um[:, np.newaxis, :] - pred_um[np.newaxis, :, :]
    dist = np.hypot(diff[:, :, 0], diff[:, :, 1])

    # Cost matrix with 1e6 penalty for distances exceeding radius_um
    cost = np.where(dist <= radius_um, dist, 1e6)
    row_ind, col_ind = linear_sum_assignment(cost)

    valid_pairs = [
        (int(i), int(j))
        for i, j in zip(row_ind, col_ind)
        if dist[i, j] <= radius_um
    ]
    tp = len(valid_pairs)
    fp = m - tp
    fn = n - tp

    return MatchResult(pairs=valid_pairs, tp=tp, fp=fp, fn=fn)


def mitosis_f1(
    cases: list[CaseMitosis],
    radius_um: float = 7.5,
    midog_compat: bool = False,
    score_threshold: float | None = None
) -> PRF:
    """
    Micro-aggregated mitotic figure precision, recall, and F1 across cases and regions (SPEC-00 §2.1).
    """
    total_tp = 0
    total_fp = 0
    total_fn = 0

    for case in cases:
        for region in case.regions:
            gt = region.gt_um
            pred = region.pred_um

            if score_threshold is not None:
                if region.pred_scores is None:
                    raise ValueError("pred_scores required when score_threshold is set")
                mask = region.pred_scores >= score_threshold
                pred = pred[mask]

            res = match_points(gt, pred, radius_um=radius_um)

            if midog_compat and len(gt) > 0 and len(pred) > 0:
                matched_preds = {j for _, j in res.pairs}
                unmatched_preds = [j for j in range(len(pred)) if j not in matched_preds]
                midog_fp_reduction = 0
                for j in unmatched_preds:
                    diff = gt - pred[j]
                    min_d = np.min(np.hypot(diff[:, 0], diff[:, 1]))
                    if min_d <= radius_um:
                        midog_fp_reduction += 1
                actual_fp = res.fp - midog_fp_reduction
            else:
                actual_fp = res.fp

            total_tp += res.tp
            total_fp += actual_fp
            total_fn += res.fn

    # Compute PRF
    p_denom = total_tp + total_fp
    r_denom = total_tp + total_fn
    f1_denom = 2 * total_tp + total_fp + total_fn

    precision = total_tp / p_denom if p_denom > 0 else math.nan
    recall = total_tp / r_denom if r_denom > 0 else math.nan
    f1 = (2 * total_tp) / f1_denom if f1_denom > 0 else math.nan

    return PRF(
        tp=total_tp,
        fp=total_fp,
        fn=total_fn,
        precision=precision,
        recall=recall,
        f1=f1,
    )


def mitosis_pr_curve(
    cases: list[CaseMitosis],
    radius_um: float = 7.5
) -> PRCurve:
    """
    Compute full PR curve and un-interpolated Average Precision (AP) (SPEC-00 §2.1).
    AP = sum_k (R_k - R_{k-1}) * P_k with R_0 = 0.
    """
    all_scores = []
    for case in cases:
        for region in case.regions:
            if region.pred_scores is not None and len(region.pred_scores) > 0:
                all_scores.extend(region.pred_scores.tolist())

    if not all_scores:
        return PRCurve(
            thresholds=np.array([], dtype=float),
            precision=np.array([], dtype=float),
            recall=np.array([], dtype=float),
            f1=np.array([], dtype=float),
            ap=0.0,
        )

    thresholds = np.array(sorted(list(set(all_scores)), reverse=True), dtype=float)
    precisions = []
    recalls = []
    f1s = []

    for t in thresholds:
        prf = mitosis_f1(cases, radius_um=radius_um, score_threshold=float(t))
        precisions.append(0.0 if math.isnan(prf.precision) else prf.precision)
        recalls.append(0.0 if math.isnan(prf.recall) else prf.recall)
        f1s.append(0.0 if math.isnan(prf.f1) else prf.f1)

    p_arr = np.array(precisions, dtype=float)
    r_arr = np.array(recalls, dtype=float)
    f1_arr = np.array(f1s, dtype=float)

    # Compute AP = sum_k (R_k - R_{k-1}) * P_k with R_{-1} = 0
    prev_r = 0.0
    ap = 0.0
    for p_k, r_k in zip(p_arr, r_arr):
        delta_r = r_k - prev_r
        ap += delta_r * p_k
        prev_r = r_k

    return PRCurve(
        thresholds=thresholds,
        precision=p_arr,
        recall=r_arr,
        f1=f1_arr,
        ap=float(ap),
    )


def macro_f1(
    y_true: Sequence[Any],
    y_pred: Sequence[Any],
    labels: tuple[int, ...] = (1, 2, 3),
    none_label: str = "none"
) -> MacroF1:
    """
    Compute macro-averaged F1 across active classes with coverage tracking (SPEC-00 §2.5).
    Classes present in y_true or y_pred (excluding none_label) are evaluated.
    """
    n = len(y_true)
    pred_classes = {p for p in y_pred if p != none_label and p in labels}
    true_classes = {t for t in y_true if t in labels}
    active_classes = sorted(true_classes | pred_classes)

    per_class: dict[int, float] = {}
    for c in active_classes:
        tp = sum(1 for yt, yp in zip(y_true, y_pred) if yt == c and yp == c)
        fp = sum(1 for yt, yp in zip(y_true, y_pred) if yt != c and yp == c)
        fn = sum(1 for yt, yp in zip(y_true, y_pred) if yt == c and yp != c)
        denom = 2 * tp + fp + fn
        per_class[c] = (2 * tp) / denom if denom > 0 else 0.0

    macro = float(np.mean(list(per_class.values()))) if per_class else 0.0
    non_none_count = sum(1 for p in y_pred if p != none_label)
    coverage = float(non_none_count / n) if n > 0 else 0.0

    return MacroF1(
        per_class=per_class,
        macro=macro,
        coverage=coverage,
        n=n,
    )


def band_metrics(
    gt_total: Sequence[int],
    pred_total: Sequence[int | None],
    pred_grade: Sequence[int | None]
) -> BandMetrics:
    """
    Targeted Nottingham band-level performance metrics (SPEC-00 §2.2).
    - f1_high: binary F1 for total in {8, 9}. Missing prediction is counted as negative.
    - macro_f1_lm: restricted to cases with gt_total in {3..6}, true G1 for 3-5, G2 for 6.
    - sum_mae: MAE over cases where both totals are present.
    """
    # 1. High band (8, 9)
    gt_high = [g in (8, 9) for g in gt_total]
    pred_high = [p in (8, 9) if p is not None else False for p in pred_total]

    tp_high = sum(1 for gh, ph in zip(gt_high, pred_high) if gh and ph)
    fp_high = sum(1 for gh, ph in zip(gt_high, pred_high) if not gh and ph)
    fn_high = sum(1 for gh, ph in zip(gt_high, pred_high) if gh and not ph)
    denom_high = 2 * tp_high + fp_high + fn_high
    f1_high = (2 * tp_high) / denom_high if denom_high > 0 else 0.0
    n_high = sum(1 for gh in gt_high if gh)

    # 2. Low-to-Moderate band (3..6)
    lm_indices = [i for i, g in enumerate(gt_total) if g in (3, 4, 5, 6)]
    n_lm = len(lm_indices)

    # True classes in LM band: 1 for 3-5, 2 for 6
    lm_gt_classes = [1 if gt_total[i] in (3, 4, 5) else 2 for i in lm_indices]
    active_lm_classes = sorted(set(lm_gt_classes))

    lm_per_class: dict[int, float] = {}
    for c in active_lm_classes:
        tp = 0
        fp = 0
        fn = 0
        for i, true_c in zip(lm_indices, lm_gt_classes):
            pg = pred_grade[i]
            if true_c == c and pg == c:
                tp += 1
            elif true_c != c and pg == c:
                fp += 1
            elif true_c == c and pg != c:
                fn += 1
            # Note: G3 or None prediction when true_c != c is a miss for its true class, not a FP for c
        denom = 2 * tp + fp + fn
        lm_per_class[c] = (2 * tp) / denom if denom > 0 else 0.0

    macro_f1_lm = float(np.mean(list(lm_per_class.values()))) if lm_per_class else 0.0

    # 3. Sum MAE
    mae_diffs = [
        abs(g - p)
        for g, p in zip(gt_total, pred_total)
        if p is not None
    ]
    sum_mae = float(np.mean(mae_diffs)) if mae_diffs else 0.0

    return BandMetrics(
        f1_high=f1_high,
        macro_f1_lm=macro_f1_lm,
        sum_mae=sum_mae,
        n_high=n_high,
        n_lm=n_lm,
    )


def qwk(
    y_true: Sequence[int],
    y_pred: Sequence[int | None | str],
    labels: tuple[int, ...] = (1, 2, 3)
) -> float:
    """
    Quadratic-Weighted Cohen's Kappa over cases with a valid prediction (SPEC-00 §2.3).
    Equivalent to sklearn.metrics.cohen_kappa_score(..., weights="quadratic").
    """
    filtered_true = []
    filtered_pred = []
    for yt, yp in zip(y_true, y_pred):
        if yp is not None and yp != "none" and yp in labels and yt in labels:
            filtered_true.append(yt)
            filtered_pred.append(yp)

    if not filtered_true:
        return 0.0

    return float(cohen_kappa_score(
        filtered_true,
        filtered_pred,
        labels=list(labels),
        weights="quadratic"
    ))


def bootstrap(
    metric_fn: Callable[[Sequence[Any]], float],
    units: Sequence[Any],
    B: int = 2000,
    seed: int = 0,
    alpha: float = 0.05
) -> CI:
    """
    Percentile bootstrap confidence interval for a metric (SPEC-00 §2.4).
    """
    point = float(metric_fn(units))
    n = len(units)
    if n == 0:
        return CI(point=point, low=point, high=point, B=B, seed=seed)

    rng = np.random.default_rng(seed)
    boot_stats = []
    is_list = isinstance(units, list)

    for _ in range(B):
        idx = rng.choice(n, size=n, replace=True)
        if is_list:
            resampled = [units[i] for i in idx]
        else:
            resampled = units[idx]
        boot_stats.append(metric_fn(resampled))

    low = float(np.percentile(boot_stats, 100 * (alpha / 2), method="linear"))
    high = float(np.percentile(boot_stats, 100 * (1.0 - alpha / 2), method="linear"))

    return CI(point=point, low=low, high=high, B=B, seed=seed)


def paired_bootstrap_delta(
    metric_fn: Callable[[Sequence[Any]], float],
    units_a: Sequence[Any],
    units_b: Sequence[Any],
    B: int = 2000,
    seed: int = 0,
    alpha: float = 0.05
) -> DeltaCI:
    """
    Paired bootstrap confidence interval for delta = metric_fn(b) - metric_fn(a) (SPEC-00 §2.4).
    Uses identical index resamples across both arms.
    """
    if len(units_a) != len(units_b):
        raise ValueError(f"Lengths differ: len(a)={len(units_a)}, len(b)={len(units_b)}")

    delta = float(metric_fn(units_b)) - float(metric_fn(units_a))
    n = len(units_a)
    if n == 0:
        return DeltaCI(delta=delta, low=delta, high=delta, B=B, seed=seed)

    rng = np.random.default_rng(seed)
    diffs = []
    is_list_a = isinstance(units_a, list)
    is_list_b = isinstance(units_b, list)

    for _ in range(B):
        idx = rng.choice(n, size=n, replace=True)
        if is_list_a:
            resampled_a = [units_a[i] for i in idx]
        else:
            resampled_a = units_a[idx]

        if is_list_b:
            resampled_b = [units_b[i] for i in idx]
        else:
            resampled_b = units_b[idx]

        diffs.append(float(metric_fn(resampled_b)) - float(metric_fn(resampled_a)))

    low = float(np.percentile(diffs, 100 * (alpha / 2), method="linear"))
    high = float(np.percentile(diffs, 100 * (1.0 - alpha / 2), method="linear"))

    return DeltaCI(delta=delta, low=low, high=high, B=B, seed=seed)


def mcnemar_exact(
    correct_a: Sequence[int | bool],
    correct_b: Sequence[int | bool]
) -> float:
    """
    Two-sided exact McNemar test using Binomial CDF (SPEC-00 §2.4).
    p = min(1, 2 * BinomCDF(min(b, c); b + c, 0.5)).
    Returns 1.0 when b + c == 0.
    """
    b = sum(1 for a, b_val in zip(correct_a, correct_b) if bool(a) and not bool(b_val))
    c = sum(1 for a, b_val in zip(correct_a, correct_b) if not bool(a) and bool(b_val))

    total = b + c
    if total == 0:
        return 1.0

    k = min(b, c)
    cdf = binom.cdf(k, total, 0.5)
    return float(min(1.0, 2.0 * cdf))
