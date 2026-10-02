"""The served tumour head, calibrator and tile-grid helpers (SPEC-05 §4.2-4.3; WP-6.2)."""
import numpy as np
import pytest
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

from pipeline.tumor_head import (
    IsotonicCalibrator,
    TumorHeadModel,
    l2_normalize,
    smooth_tile_probabilities,
    tile_raster,
)

CLASSES = ["invasive_tumor", "in_situ", "stroma"]


def _heads(dim=8, seed=0):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(90, dim))
    y = np.repeat([0, 1, 2], 30)
    x[y == 0] += 2.0
    mean, scale = l2_normalize(x).mean(0), l2_normalize(x).std(0)
    z = (l2_normalize(x) - mean) / scale
    multi = LogisticRegression(max_iter=1000).fit(z, y)
    binary = LogisticRegression(max_iter=1000).fit(z, (y == 0).astype(int))
    return x, mean, scale, multi, binary


def test_l2_normalize_keeps_zero_rows():
    out = l2_normalize(np.array([[3.0, 4.0], [0.0, 0.0]]))
    np.testing.assert_allclose(out, [[0.6, 0.8], [0.0, 0.0]])
    with pytest.raises(ValueError):
        l2_normalize(np.zeros(3))


def test_multinomial_head_alone_is_the_logistic_regression():
    x, mean, scale, multi, _ = _heads()
    head = TumorHeadModel(CLASSES, mean, scale, multi, "invasive_tumor")
    np.testing.assert_allclose(head.predict_proba(x), multi.predict_proba((l2_normalize(x) - mean) / scale))
    assert head.n_features_in_ == 8 and list(head.classes_) == CLASSES


def test_ensemble_mixes_the_positive_class_and_rescales_the_rest():
    x, mean, scale, multi, binary = _heads()
    head = TumorHeadModel(CLASSES, mean, scale, multi, "invasive_tumor", binary=binary, binary_weight=0.5)
    z = (l2_normalize(x) - mean) / scale
    p_multi, p_bin = multi.predict_proba(z), binary.predict_proba(z)[:, 1]
    p = head.predict_proba(x)
    np.testing.assert_allclose(p[:, 0], 0.5 * p_multi[:, 0] + 0.5 * p_bin)
    np.testing.assert_allclose(p.sum(axis=1), 1.0)
    # the other classes keep their multinomial ratio
    np.testing.assert_allclose(p[:, 1] / p[:, 2], p_multi[:, 1] / p_multi[:, 2])


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"positive_class": "dcis"}, "positive class"),
        ({"binary_weight": 0.5}, "binary_weight"),
        ({"scale": np.zeros(8)}, "scale"),
    ],
)
def test_head_refuses_inconsistent_parts(kwargs, message):
    x, mean, scale, multi, _ = _heads()
    args = {"classes": CLASSES, "mean": mean, "scale": scale, "multinomial": multi, "positive_class": "invasive_tumor"}
    args.update(kwargs)
    with pytest.raises(ValueError, match=message):
        TumorHeadModel(**args)


def test_head_refuses_the_wrong_feature_width():
    x, mean, scale, multi, _ = _heads()
    with pytest.raises(ValueError, match="features"):
        TumorHeadModel(CLASSES, mean, scale, multi, "invasive_tumor").predict_proba(np.ones((2, 5)))


def test_calibrator_is_a_two_class_distribution_and_clips():
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1).fit([0.1, 0.4, 0.6, 0.9], [0, 0, 1, 1])
    cal = IsotonicCalibrator(iso, "invasive_tumor")
    p = cal.predict_proba(np.array([[0.0], [0.5], [1.0]]))
    np.testing.assert_allclose(p.sum(axis=1), 1.0)
    assert p[0, 1] == 0.0 and p[2, 1] == 1.0
    with pytest.raises(ValueError):
        cal.predict_proba(np.ones((3, 2)))
    with pytest.raises(ValueError, match="clip"):
        IsotonicCalibrator(IsotonicRegression(), "invasive_tumor")


def test_tile_raster_places_tiles_and_leaves_the_rest_nan():
    r = tile_raster(np.array([0, 2]), np.array([1, 0]), np.array([0.3, 0.7]), n_cols=3, n_rows=2)
    assert r[1, 0] == 0.3 and r[0, 2] == 0.7
    assert np.isnan(r).sum() == 4


def test_smoothing_ignores_off_tissue_tiles():
    r = np.full((5, 5), np.nan)
    r[2, 1:4] = [0.0, 1.0, 0.0]
    s = smooth_tile_probabilities(r, 1.0)
    assert np.isnan(s).sum() == 22
    assert 0.0 < s[2, 1] < s[2, 2] < 1.0
    np.testing.assert_allclose(s[2, 1], s[2, 3])
    flat = np.full((4, 4), 0.4)
    flat[0, 0] = np.nan
    np.testing.assert_allclose(smooth_tile_probabilities(flat, 1.0)[~np.isnan(flat)], 0.4)  # no dilution at edges
    with pytest.raises(ValueError):
        smooth_tile_probabilities(r, 0.0)
