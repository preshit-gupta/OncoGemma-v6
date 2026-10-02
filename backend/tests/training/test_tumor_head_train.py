"""Tumour-head training, calibration and reproducibility (SPEC-05 §4.2, AC7; WP-6.2). Synthetic data."""
import json

import joblib
import numpy as np
import pandas as pd
import pytest

from pipeline.tumor_head import IsotonicCalibrator, TumorHeadModel
from training.tumor_head.train import (
    FitSettings,
    HeldOutAccessRefused,
    best_threshold,
    coefficients,
    f1_score,
    held_out_test_metrics,
    train,
    write_artifacts,
)

CLASSES = ("invasive_tumor", "in_situ", "benign_epithelium", "stroma", "inflammatory", "necrosis", "adipose_background")
SETTINGS = FitSettings(c_grid=(0.1, 1.0), cv_folds=3, max_iter=5000, tol=1e-10, binary_weights=(0.0, 0.5),
                       smoothing_sigma_tiles=(None, 1.0), bootstrap_resamples=200, seed=7)


def synthetic(n_patients=18, tiles_per_patient=30, dim=24, seed=0, noise=2.0):
    """Tiles whose embedding is a class centroid plus noise; patients split 2:1:1 train/val/test."""
    rng = np.random.default_rng(seed)
    centroids = rng.normal(size=(len(CLASSES), dim)) * 2.0
    rows, x = [], []
    for p in range(n_patients):
        split = ("train", "train", "val", "test")[p % 4]
        for t in range(tiles_per_patient):
            k = (p + t) % len(CLASSES) if t % 3 else 0  # a third of the tiles are invasive tumour
            rows.append({"slide_id": f"S{p:02d}", "patient_id": f"P{p:02d}", "split": split, "label": CLASSES[k],
                         "i": t % 6, "j": t // 6, "od_sum": float(rng.uniform(0.2, 1.5))})
            x.append(centroids[k] + rng.normal(size=dim) * noise + 5.0)  # offset: L2 normalisation matters
    return pd.DataFrame(rows), np.asarray(x, dtype=np.float32)


@pytest.fixture(scope="module")
def fitted():
    frame, x = synthetic()
    return frame, x, train(frame, x, CLASSES, "invasive_tumor", SETTINGS)


def test_head_outputs_distributions_over_the_seven_classes(fitted):
    frame, x, result = fitted
    p = result["model"].predict_proba(x)
    assert p.shape == (len(x), 7)
    np.testing.assert_allclose(p.sum(axis=1), 1.0, atol=1e-9)
    assert list(result["model"].classes_) == list(CLASSES)


def test_the_head_learns_the_synthetic_classes(fitted):
    _, _, result = fitted
    assert result["report"]["val"]["f1"]["point"] > 0.8


def test_retraining_from_the_same_data_and_seed_is_identical(fitted):
    """AC7: coefficients equal within 1e-10."""
    frame, x, result = fitted
    again = train(frame, x, CLASSES, "invasive_tumor", SETTINGS)
    np.testing.assert_allclose(coefficients(again["model"]), coefficients(result["model"]), rtol=0, atol=1e-10)
    assert again["report"]["tau"] == result["report"]["tau"]


def test_calibration_and_threshold_come_from_val_only(fitted):
    frame, x, result = fitted
    va = (frame["split"] == "val").to_numpy()
    model, calibrator = result["model"], result["calibrator"]
    p_cal = calibrator.predict_proba(model.predict_proba(x[va])[:, [model.positive_index]])[:, 1]
    y = (frame.loc[va, "label"] == "invasive_tumor").to_numpy()
    if result["report"]["smoothing_sigma_tiles"] is None:
        assert f1_score(y, p_cal >= result["report"]["tau"]) == pytest.approx(result["report"]["val"]["f1"]["point"])


def test_v5_fusion_row_is_reported_when_the_probe_is_given(tmp_path):
    from tests.fakes.probe import train_default_probe

    frame, x = synthetic(dim=384)
    probe = joblib.load(train_default_probe(str(tmp_path)))
    report = train(frame, x, CLASSES, "invasive_tumor", SETTINGS, probe=probe)["report"]
    row = report["ablations"]["od_fusion_v5"]
    assert {"tau", "val_f1", "delta_head_minus_v5", "head_beats_v5"} <= set(row)


def test_missing_train_class_raises():
    frame, x = synthetic()
    keep = ~((frame["split"] == "train") & (frame["label"] == "necrosis")).to_numpy()
    with pytest.raises(ValueError, match="no train tile"):
        train(frame[keep].reset_index(drop=True), x[keep], CLASSES, "invasive_tumor", SETTINGS)


def test_test_metrics_need_a_reason(fitted):
    frame, x, result = fitted
    with pytest.raises(HeldOutAccessRefused):
        held_out_test_metrics(frame, x, result["model"], result["calibrator"], result["report"], SETTINGS, None)
    out = held_out_test_metrics(frame, x, result["model"], result["calibrator"], result["report"], SETTINGS, "unit test")
    assert out["reason"] == "unit test" and 0.0 <= out["f1"]["point"] <= 1.0


def test_best_threshold_maximises_f1_and_prefers_the_larger_tie():
    y = np.array([True, True, False, False])
    p = np.array([0.9, 0.8, 0.7, 0.1])
    assert best_threshold(y, p) == (0.8, 1.0)
    y2 = np.array([True, False])
    tau, f1 = best_threshold(y2, np.array([0.5, 0.5]))
    assert (tau, f1) == (0.5, pytest.approx(2 / 3))


def test_artifacts_round_trip_and_card(fitted, tmp_path):
    _, x, result = fitted
    hashes = write_artifacts(tmp_path, "0.0.1-test", result, {"seed": SETTINGS.seed, "splits_lock_sha256": "0" * 64})
    model = joblib.load(tmp_path / "model.joblib")
    calibrator = joblib.load(tmp_path / "calibrator.joblib")
    assert isinstance(model, TumorHeadModel) and isinstance(calibrator, IsotonicCalibrator)
    np.testing.assert_array_equal(model.predict_proba(x[:5]), result["model"].predict_proba(x[:5]))
    card = json.loads((tmp_path / "card.json").read_text())
    assert card["artifacts_sha256"] == hashes
    assert card["classes"] == list(CLASSES) and card["seed"] == SETTINGS.seed
    assert {"train", "val", "test"} == set(card["class_counts"])
