"""
Known-answer tests for WP-5.1 (SPEC-00 §2, SPEC-02 §7): evaluation metrics.
Expected values are computed by hand in the comments. Do NOT change assertions.
"""
import math

import numpy as np
import pytest

m = pytest.importorskip("eval.metrics")


def pts(*xy):
    return np.array(xy, dtype=float).reshape(-1, 2)


EMPTY = np.zeros((0, 2), dtype=float)


# ---------------------------------------------------------------------------
# match_points — 1:1 Hungarian within radius (inclusive)
# ---------------------------------------------------------------------------

def test_match_basic_and_inclusive_radius():
    gt = pts((0, 0), (100, 0))
    pred = pts((3, 4), (100, 7.5), (50, 50))       # d=5 (TP), d=7.5 exactly (TP, inclusive), far (FP)
    r = m.match_points(gt, pred, radius_um=7.5)
    assert (r.tp, r.fp, r.fn) == (2, 1, 0)
    assert sorted(r.pairs) == [(0, 0), (1, 1)]     # (gt_index, pred_index)


def test_match_just_outside_radius():
    r = m.match_points(pts((0, 0)), pts((7.5001, 0)), radius_um=7.5)
    assert (r.tp, r.fp, r.fn) == (0, 1, 1)


def test_match_is_one_to_one():
    r = m.match_points(pts((0, 0)), pts((1, 0), (2, 0)), radius_um=7.5)
    assert (r.tp, r.fp, r.fn) == (1, 1, 0)


def test_match_optimal_not_greedy():
    # A=(6,0) is 6 from g1 and 4 from g2; B=(13,0) is 3 from g2 and 13 from g1 (> R).
    # Greedy-by-prediction-order gives A->g2 and leaves B unmatched (TP=1).
    # The optimal 1:1 assignment is A->g1, B->g2 (TP=2).
    r = m.match_points(pts((0, 0), (10, 0)), pts((6, 0), (13, 0)), radius_um=7.5)
    assert (r.tp, r.fp, r.fn) == (2, 0, 0)


def test_match_empty_inputs():
    assert (lambda r: (r.tp, r.fp, r.fn))(m.match_points(EMPTY, EMPTY)) == (0, 0, 0)
    assert (lambda r: (r.tp, r.fp, r.fn))(m.match_points(pts((0, 0)), EMPTY)) == (0, 0, 1)
    assert (lambda r: (r.tp, r.fp, r.fn))(m.match_points(EMPTY, pts((0, 0)))) == (0, 1, 0)


def test_match_default_radius_is_7_5():
    r = m.match_points(pts((0, 0)), pts((7.5, 0)))
    assert r.tp == 1


# ---------------------------------------------------------------------------
# mitosis_f1 — micro-aggregated over regions and cases
# ---------------------------------------------------------------------------

def _cases():
    c1 = m.CaseMitosis(case_id="c1", regions=[
        m.RegionPoints(gt_um=pts((0, 0)), pred_um=pts((1, 1))),          # TP
        m.RegionPoints(gt_um=EMPTY, pred_um=pts((5, 5))),                 # FP
    ])
    c2 = m.CaseMitosis(case_id="c2", regions=[
        m.RegionPoints(gt_um=pts((0, 0), (20, 20)), pred_um=EMPTY),       # FN, FN
    ])
    return [c1, c2]


def test_mitosis_f1_micro():
    # TP=1, FP=1, FN=2 -> P=0.5, R=1/3, F1=2/(2+1+2)=0.4
    r = m.mitosis_f1(_cases())
    assert (r.tp, r.fp, r.fn) == (1, 1, 2)
    assert r.precision == pytest.approx(0.5)
    assert r.recall == pytest.approx(1 / 3)
    assert r.f1 == pytest.approx(0.4)


def test_mitosis_f1_midog_compat_counts_duplicates_once():
    # two predictions within R of one GT: strict 1:1 -> TP1 FP1; MIDOG-compat -> TP1 FP0
    cases = [m.CaseMitosis("c", [m.RegionPoints(gt_um=pts((0, 0)), pred_um=pts((1, 0), (2, 0)))])]
    strict = m.mitosis_f1(cases)
    compat = m.mitosis_f1(cases, midog_compat=True)
    assert (strict.tp, strict.fp, strict.fn) == (1, 1, 0)
    assert (compat.tp, compat.fp, compat.fn) == (1, 0, 0)
    assert compat.f1 == pytest.approx(1.0)


def test_mitosis_f1_score_threshold():
    cases = [m.CaseMitosis("c", [m.RegionPoints(
        gt_um=pts((0, 0), (20, 0)),
        pred_um=pts((0, 0), (20, 0), (50, 0)),
        pred_scores=np.array([0.9, 0.4, 0.8]),
    )])]
    r = m.mitosis_f1(cases, score_threshold=0.5)   # keeps (0,0)@.9 TP and (50,0)@.8 FP; GT (20,0) FN
    assert (r.tp, r.fp, r.fn) == (1, 1, 1)
    assert r.f1 == pytest.approx(0.5)


def test_mitosis_f1_undefined_is_nan():
    r = m.mitosis_f1([m.CaseMitosis("c", [m.RegionPoints(gt_um=EMPTY, pred_um=EMPTY)])])
    assert (r.tp, r.fp, r.fn) == (0, 0, 0)
    assert math.isnan(r.f1) and math.isnan(r.precision) and math.isnan(r.recall)


def test_mitosis_pr_curve_average_precision():
    # thresholds (unique scores desc): .9 -> TP1 P1 R.5 ; .8 -> TP2 P1 R1 ; .1 -> TP2 FP1 P2/3 R1
    # AP = sum (R_k - R_{k-1}) * P_k = .5*1 + .5*1 + 0*(2/3) = 1.0
    cases = [m.CaseMitosis("c", [m.RegionPoints(
        gt_um=pts((0, 0), (20, 0)),
        pred_um=pts((0, 0), (20, 0), (50, 0)),
        pred_scores=np.array([0.9, 0.8, 0.1]),
    )])]
    pr = m.mitosis_pr_curve(cases)
    assert pr.ap == pytest.approx(1.0)
    assert list(pr.thresholds) == pytest.approx([0.9, 0.8, 0.1])
    assert list(pr.precision) == pytest.approx([1.0, 1.0, 2 / 3])
    assert list(pr.recall) == pytest.approx([0.5, 1.0, 1.0])


def test_mitosis_pr_curve_imperfect():
    # scores desc: FP@.9, TP@.8  -> t=.9: P0 R0 ; t=.8: P.5 R1   -> AP = 0*0 + 1*.5 = 0.5
    cases = [m.CaseMitosis("c", [m.RegionPoints(
        gt_um=pts((0, 0)), pred_um=pts((50, 0), (0, 0)), pred_scores=np.array([0.9, 0.8]))])]
    assert m.mitosis_pr_curve(cases).ap == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# macro_f1 with 'none'
# ---------------------------------------------------------------------------

def test_macro_f1_with_none_label():
    # G1: TP1 FP0 FN1 -> 2/3 ; G2: TP2 FP1 FN0 -> 0.8 ; G3: TP1 FP0 FN1 -> 2/3
    y_true = [1, 1, 2, 2, 3, 3]
    y_pred = [1, 2, 2, 2, 3, "none"]
    r = m.macro_f1(y_true, y_pred)
    assert r.per_class[1] == pytest.approx(2 / 3)
    assert r.per_class[2] == pytest.approx(0.8)
    assert r.per_class[3] == pytest.approx(2 / 3)
    assert r.macro == pytest.approx((2 / 3 + 0.8 + 2 / 3) / 3)
    assert r.coverage == pytest.approx(5 / 6)
    assert r.n == 6


def test_macro_f1_includes_spurious_predicted_class_and_skips_absent():
    # classes present in y_true or y_pred: {1, 3}; class 2 absent from both -> excluded
    # G1: TP1 FN1 -> 2/3 ; G3: FP1 -> 0  -> macro = 1/3
    r = m.macro_f1([1, 1], [1, 3])
    assert set(r.per_class) == {1, 3}
    assert r.macro == pytest.approx(1 / 3)


def test_macro_f1_none_is_never_a_class():
    r = m.macro_f1([1, 2], ["none", "none"])
    assert set(r.per_class) == {1, 2}
    assert r.macro == pytest.approx(0.0)
    assert r.coverage == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# band_metrics — SPEC-00 §2.2
# ---------------------------------------------------------------------------

def test_band_metrics_known_answer():
    # rows: (gt_total, pred_total, pred_grade)
    rows = [
        (9, 8, 3),        # a: high TP
        (8, 7, 2),        # b: high FN
        (6, 8, 3),        # c: high FP ; LM band true G2, pred G3 -> miss
        (5, 5, 1),        # d: LM G1 TP
        (4, 6, 2),        # e: LM true G1 pred G2 -> G1 FN, G2 FP
        (3, None, None),  # f: LM true G1, no prediction -> G1 FN ; excluded from MAE
        (7, 7, 2),        # g: neither band
    ]
    gt, pt, pg = zip(*rows)
    r = m.band_metrics(list(gt), list(pt), list(pg))
    assert r.f1_high == pytest.approx(0.5)          # TP1 FP1 FN1
    assert r.macro_f1_lm == pytest.approx(0.25)     # G1: TP1 FP0 FN2 -> .5 ; G2: TP0 FP1 FN1 -> 0
    assert r.sum_mae == pytest.approx(1.0)          # |1|+|1|+|2|+|0|+|2|+|0| over 6
    assert (r.n_high, r.n_lm) == (2, 4)


# ---------------------------------------------------------------------------
# qwk
# ---------------------------------------------------------------------------

def test_qwk_perfect_and_matches_sklearn():
    from sklearn.metrics import cohen_kappa_score
    assert m.qwk([1, 2, 3, 2], [1, 2, 3, 2]) == pytest.approx(1.0)
    a = [1, 1, 2, 2, 3, 3, 2, 1]
    b = [1, 2, 2, 3, 3, 2, 2, 1]
    assert m.qwk(a, b) == pytest.approx(cohen_kappa_score(a, b, weights="quadratic"))


# ---------------------------------------------------------------------------
# bootstrap / paired bootstrap / McNemar
# ---------------------------------------------------------------------------

def _mean_metric(units):
    return float(np.mean(units))


def test_bootstrap_deterministic_and_contains_point():
    units = [0.1, 0.4, 0.35, 0.8, 0.5, 0.2, 0.9, 0.6]
    a = m.bootstrap(_mean_metric, units, B=500, seed=7)
    b = m.bootstrap(_mean_metric, units, B=500, seed=7)
    assert (a.low, a.high) == (b.low, b.high)
    assert a.point == pytest.approx(np.mean(units))
    assert a.low <= a.point <= a.high
    assert (a.B, a.seed) == (500, 7)


def test_bootstrap_constant_units_zero_width():
    ci = m.bootstrap(_mean_metric, [0.3] * 10, B=200, seed=1)
    assert ci.low == pytest.approx(0.3) and ci.high == pytest.approx(0.3)


def test_paired_bootstrap_identical_is_zero():
    units = [0.2, 0.5, 0.9, 0.4]
    d = m.paired_bootstrap_delta(_mean_metric, units, units, B=300, seed=3)
    assert d.delta == pytest.approx(0.0)
    assert d.low == pytest.approx(0.0) and d.high == pytest.approx(0.0)


def test_paired_bootstrap_uses_same_indices():
    # B is A + 0.1 unit-wise -> every resample differs by exactly 0.1
    a = [0.1, 0.2, 0.3, 0.4, 0.5]
    b = [x + 0.1 for x in a]
    d = m.paired_bootstrap_delta(_mean_metric, a, b, B=300, seed=11)
    assert d.delta == pytest.approx(0.1)
    assert d.low == pytest.approx(0.1) and d.high == pytest.approx(0.1)


def test_paired_bootstrap_requires_equal_length():
    with pytest.raises(ValueError):
        m.paired_bootstrap_delta(_mean_metric, [1, 2], [1, 2, 3], B=10, seed=0)


def test_mcnemar_exact():
    assert m.mcnemar_exact([0] * 5, [1] * 5) == pytest.approx(0.0625)     # b=0, c=5 -> 2 * 0.5**5
    assert m.mcnemar_exact([1, 0, 1, 0], [0, 1, 0, 1]) == pytest.approx(1.0)  # b=c=2
    assert m.mcnemar_exact([1, 1, 0], [1, 1, 0]) == pytest.approx(1.0)        # no discordant pairs
