"""Train, calibrate and document the tumour head (SPEC-05 §4.2; WP-6.2).

Steps, all on the BCSS tile set (``dataset.py``) with the committed patient splits:

1. Features: PF embedding, L2-normalised, z-scored with train statistics.
2. Multinomial logistic regression (lbfgs, ``class_weight='balanced'``); ``C`` from ``c_grid`` by
   ``GroupKFold`` on patients within train, scored by mean log loss (threshold-free; F1 is chosen
   later on val). A collapsed binary head (invasive vs not) is fitted the same way.
3. Candidates: the multinomial head alone and its ensembles with the binary head
   (``binary_weights``). Each is calibrated on val (isotonic, one-vs-rest for the positive class)
   and gets ``τ = argmax F1(val)``; the candidate with the best val F1 wins (ties: the simpler).
   The binary head stands in for SPEC-05's BCNB ``non_tumor`` term, which is deferred with BCNB (D20).
4. Ablations on val, each with its own τ: Gaussian smoothing (σ = 1 tile) of the calibrated
   probability on each slide's tile grid, and the v5 fusion ``od_fusion_v5``. A variant is adopted
   only if its paired patient-bootstrap ΔF1 has lower bound > 0. AC1 requires the head to beat
   ``od_fusion_v5`` that way.
5. Test metrics only with ``--confirm-test-access "<reason>"`` (SPEC-02 §5.2 test lock), at the
   val τ, never used for any choice.

Outputs (``--out``): ``model.joblib``, ``calibrator.joblib``, ``scaler.json`` and ``card.json`` for
``models/tumor_head/<version>/``. The same dataset, config and seed give the same coefficients
(AC7): lbfgs is deterministic and nothing is shuffled.
"""
from __future__ import annotations

import hashlib
import json
import platform
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from pipeline.tumor_head import (
    IsotonicCalibrator,
    TumorHeadModel,
    l2_normalize,
    smooth_tile_probabilities,
    tile_raster,
)

V5_FUSION = {"w_probe": 0.35, "w_cell": 0.65, "margin_base": 0.40, "margin_gain": 0.60, "gain": 1.25, "clip": (0.05, 0.98)}
V5_PROBE_CLASS = 1


class HeldOutAccessRefused(PermissionError):
    """Test metrics need an explicit, recorded reason (SPEC-02 §5.2)."""


@dataclass(frozen=True)
class FitSettings:
    c_grid: tuple[float, ...]
    cv_folds: int
    max_iter: int
    tol: float
    binary_weights: tuple[float, ...]
    smoothing_sigma_tiles: tuple[float | None, ...]
    bootstrap_resamples: int
    seed: int

    @classmethod
    def from_config(cls, config: dict) -> FitSettings:
        m = config["model"]
        return cls(
            c_grid=tuple(float(c) for c in m["c_grid"]), cv_folds=int(m["cv_folds"]), max_iter=int(m["max_iter"]),
            tol=float(m["tol"]), binary_weights=tuple(float(w) for w in m["binary_weights"]),
            smoothing_sigma_tiles=tuple(None if s is None else float(s) for s in m["smoothing_sigma_tiles"]),
            bootstrap_resamples=int(m["bootstrap_resamples"]), seed=int(m["seed"]),
        )


# --- metrics ---------------------------------------------------------------------------------------------------


def f1_score(y: np.ndarray, pred: np.ndarray) -> float:
    tp = int(np.sum(y & pred))
    fp = int(np.sum(~y & pred))
    fn = int(np.sum(y & ~pred))
    return 0.0 if tp == 0 else 2 * tp / (2 * tp + fp + fn)


def best_threshold(y: np.ndarray, p: np.ndarray) -> tuple[float, float]:
    """``(τ, F1)`` maximising F1 of ``p >= τ`` over the distinct values of ``p`` (ties: the larger τ)."""
    order = np.argsort(-p, kind="stable")
    p_sorted, y_sorted = p[order], y[order]
    tp = np.cumsum(y_sorted)
    fp = np.cumsum(~y_sorted)
    positives = int(y.sum())
    last_of_value = np.r_[p_sorted[1:] != p_sorted[:-1], True]  # p >= τ takes every tile with that value
    tp, fp, taus = tp[last_of_value], fp[last_of_value], p_sorted[last_of_value]
    f1 = np.where(tp > 0, 2 * tp / (tp + fp + positives), 0.0)
    best = int(np.argmax(f1))  # first maximum = largest τ
    return float(taus[best]), float(f1[best])


def patient_units(frame: pd.DataFrame, y: np.ndarray, pred: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    """One bootstrap unit per patient: its tiles' truth and prediction."""
    units = []
    for idx in frame.groupby("patient_id", sort=True).indices.values():
        units.append((y[idx], pred[idx]))
    return units


def pooled_f1(units) -> float:
    if len(units) == 0:
        return 0.0
    return f1_score(np.concatenate([u[0] for u in units]), np.concatenate([u[1] for u in units]))


# --- fitting ---------------------------------------------------------------------------------------------------


def standardiser(x_l2: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mean = x_l2.mean(axis=0)
    scale = x_l2.std(axis=0)
    if np.any(scale <= 0):
        raise ValueError(f"{int(np.sum(scale <= 0))} embedding dimensions are constant on train; cannot z-score them")
    return mean, scale


def _logreg(c: float, settings: FitSettings):
    from sklearn.linear_model import LogisticRegression

    return LogisticRegression(C=c, solver="lbfgs", class_weight="balanced", max_iter=settings.max_iter, tol=settings.tol)


def _fit_logreg(x: np.ndarray, y: np.ndarray, c: float, settings: FitSettings):
    import warnings

    from sklearn.exceptions import ConvergenceWarning

    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)  # an unconverged fit is not a model
        return _logreg(c, settings).fit(x, y)


def choose_c(x_l2: np.ndarray, y: np.ndarray, groups: np.ndarray, n_classes: int, settings: FitSettings) -> tuple[float, list[dict]]:
    """``C`` with the lowest mean log loss over patient-grouped folds of train; and the per-C table."""
    from sklearn.metrics import log_loss
    from sklearn.model_selection import GroupKFold

    folds = list(GroupKFold(n_splits=settings.cv_folds).split(x_l2, y, groups))
    table = []
    for c in settings.c_grid:
        losses = []
        for train_idx, val_idx in folds:
            mean, scale = standardiser(x_l2[train_idx])
            model = _fit_logreg((x_l2[train_idx] - mean) / scale, y[train_idx], c, settings)
            proba = np.zeros((val_idx.size, n_classes))
            proba[:, model.classes_] = model.predict_proba((x_l2[val_idx] - mean) / scale)
            losses.append(log_loss(y[val_idx], np.clip(proba, 1e-15, 1.0), labels=list(range(n_classes))))
        table.append({"C": c, "cv_log_loss": float(np.mean(losses)), "fold_log_loss": [float(v) for v in losses]})
    best = min(table, key=lambda row: (row["cv_log_loss"], row["C"]))
    return best["C"], table


@dataclass
class Candidate:
    binary_weight: float
    model: TumorHeadModel
    p_raw_val: np.ndarray
    calibrator: IsotonicCalibrator
    p_cal_val: np.ndarray
    tau: float
    f1_val: float


def fit_calibrator(p_raw: np.ndarray, y_bin: np.ndarray, positive_class: str) -> IsotonicCalibrator:
    from sklearn.isotonic import IsotonicRegression

    iso = IsotonicRegression(y_min=0.0, y_max=1.0, increasing=True, out_of_bounds="clip").fit(p_raw, y_bin.astype(float))
    return IsotonicCalibrator(iso, positive_class)


def smoothed(frame: pd.DataFrame, p: np.ndarray, sigma: float) -> np.ndarray:
    """``p`` smoothed on each slide's tile grid (labelled tiles only; others are off-grid)."""
    out = np.empty_like(p)
    for idx in frame.groupby("slide_id", sort=True).indices.values():
        i = frame["i"].to_numpy()[idx]
        j = frame["j"].to_numpy()[idx]
        i0, j0 = i.min(), j.min()
        raster = tile_raster(i - i0, j - j0, p[idx], int(i.max() - i0 + 1), int(j.max() - j0 + 1))
        out[idx] = smooth_tile_probabilities(raster, sigma)[j - j0, i - i0]
    return out


def od_fusion_v5(frame: pd.DataFrame, x: np.ndarray, probe) -> np.ndarray:
    """The v5 fused score (SPEC-05 §1.1) on the BCSS tiles, for the AC1 comparison.

    ``p_probe`` is the v5 probe on the L2-normalised embedding. ``c`` normalises each tile's
    ``od_sum`` by the 10th/90th percentiles of its ROI's tiles (v5: of the slide's tissue cells).
    ``m`` = 1: the ROIs lie inside tissue, and v5's edge damping (m < 1) can only lower its score
    near ROI borders that are not tissue edges, so m = 1 is the variant most favourable to v5.
    """
    p_probe = probe.predict_proba(l2_normalize(x).astype(np.float32))[:, list(probe.classes_).index(V5_PROBE_CLASS)]
    od = frame["od_sum"].to_numpy(np.float64)
    c = np.empty_like(od)
    for idx in frame.groupby("slide_id", sort=True).indices.values():
        p10, p90 = np.percentile(od[idx], 10), np.percentile(od[idx], 90)
        c[idx] = np.clip((od[idx] - p10) / max(p90 - p10, 1e-4), 0.0, 1.0)
    f = V5_FUSION
    fused = (f["w_probe"] * p_probe + f["w_cell"] * c) * (f["margin_base"] + f["margin_gain"] * 1.0) * f["gain"]
    return np.clip(fused, *f["clip"])


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def train(
    frame: pd.DataFrame,
    x: np.ndarray,
    classes: tuple[str, ...],
    positive_class: str,
    settings: FitSettings,
    *,
    probe=None,
) -> dict:
    """Fit, select and calibrate on train/val. Returns the chosen artefacts and every number the card records."""
    y_all = frame["label"].map({c: k for k, c in enumerate(classes)}).to_numpy()
    if np.any(pd.isna(y_all)):
        raise ValueError(f"labels outside the head classes: {sorted(set(frame['label']) - set(classes))}")
    y_all = y_all.astype(np.int64)
    k_pos = classes.index(positive_class)
    tr = (frame["split"] == "train").to_numpy()
    va = (frame["split"] == "val").to_numpy()
    if not tr.any() or not va.any():
        raise ValueError("training needs train and val tiles")
    absent = [classes[k] for k in range(len(classes)) if not np.any(y_all[tr] == k)]
    if absent:
        raise ValueError(f"classes with no train tile: {absent}")

    x_l2 = l2_normalize(x)
    groups = frame["patient_id"].to_numpy()
    c_multi, cv_multi = choose_c(x_l2[tr], y_all[tr], groups[tr], len(classes), settings)
    y_bin = y_all == k_pos
    c_bin, cv_bin = choose_c(x_l2[tr], y_bin[tr].astype(np.int64), groups[tr], 2, settings)
    mean, scale = standardiser(x_l2[tr])
    z_tr = (x_l2[tr] - mean) / scale
    multinomial = _fit_logreg(z_tr, y_all[tr], c_multi, settings)
    binary = _fit_logreg(z_tr, y_bin[tr].astype(np.int64), c_bin, settings)

    yv = y_bin[va]
    candidates = []
    for w in settings.binary_weights:
        model = TumorHeadModel(classes, mean, scale, multinomial, positive_class, binary=binary if w > 0 else None, binary_weight=w)
        p_raw = model.predict_proba(x[va])[:, k_pos]
        calibrator = fit_calibrator(p_raw, yv, positive_class)
        p_cal = calibrator.predict_proba(p_raw[:, None])[:, 1]
        tau, f1 = best_threshold(yv, p_cal)
        candidates.append(Candidate(w, model, p_raw, calibrator, p_cal, tau, f1))
    chosen = max(candidates, key=lambda c: (c.f1_val, -c.binary_weight))

    val_frame = frame[va].reset_index(drop=True)
    B, seed = settings.bootstrap_resamples, settings.seed
    from eval.metrics import bootstrap, paired_bootstrap_delta

    head_units = patient_units(val_frame, yv, chosen.p_cal_val >= chosen.tau)
    report = {
        "C": {"multinomial": c_multi, "binary": c_bin},
        "cv": {"multinomial": cv_multi, "binary": cv_bin},
        "candidates": [{"binary_weight": c.binary_weight, "tau": c.tau, "val_f1": c.f1_val} for c in candidates],
        "chosen_binary_weight": chosen.binary_weight,
        "tau": chosen.tau,
        "val": {"f1": vars(bootstrap(pooled_f1, head_units, B=B, seed=seed)), "n_tiles": int(va.sum()),
                "n_positive": int(yv.sum()), "n_patients": len(head_units)},
        "ablations": {},
    }

    smoothing = None
    for sigma in settings.smoothing_sigma_tiles:
        if sigma is None:
            continue
        p_s = smoothed(val_frame, chosen.p_cal_val, sigma)
        tau_s, f1_s = best_threshold(yv, p_s)
        delta = paired_bootstrap_delta(pooled_f1, head_units, patient_units(val_frame, yv, p_s >= tau_s), B=B, seed=seed)
        adopted = delta.low > 0
        report["ablations"][f"smoothing_sigma_{sigma:g}"] = {"tau": tau_s, "val_f1": f1_s, "delta_vs_head": vars(delta), "adopted": adopted}
        if adopted and (smoothing is None or f1_s > smoothing["val_f1"]):
            smoothing = {"sigma_tiles": sigma, "tau": tau_s, "val_f1": f1_s}
    if smoothing is not None:
        report["tau"] = smoothing["tau"]
    report["smoothing_sigma_tiles"] = None if smoothing is None else smoothing["sigma_tiles"]

    if probe is not None:
        p_v5 = od_fusion_v5(val_frame, x[va], probe)
        tau_v5, f1_v5 = best_threshold(yv, p_v5)
        delta = paired_bootstrap_delta(pooled_f1, patient_units(val_frame, yv, p_v5 >= tau_v5), head_units, B=B, seed=seed)
        report["ablations"]["od_fusion_v5"] = {"tau": tau_v5, "val_f1": f1_v5, "delta_head_minus_v5": vars(delta),
                                                "head_beats_v5": delta.low > 0}
    report["class_counts"] = {
        split: {c: int(np.sum((frame["split"] == split).to_numpy() & (y_all == k))) for k, c in enumerate(classes)}
        for split in ("train", "val", "test")
    }
    report["patients"] = {split: int(frame.loc[frame["split"] == split, "patient_id"].nunique()) for split in ("train", "val", "test")}
    return {"model": chosen.model, "calibrator": chosen.calibrator, "report": report}


def held_out_test_metrics(frame: pd.DataFrame, x: np.ndarray, model: TumorHeadModel, calibrator: IsotonicCalibrator, report: dict,
                 settings: FitSettings, reason: str | None) -> dict:
    """F1 with 95% CI on test at the val τ (and val smoothing). Refused without a reason."""
    from eval.metrics import bootstrap

    if not reason or not reason.strip():
        raise HeldOutAccessRefused("test metrics need --confirm-test-access \"<reason>\" (SPEC-02 §5.2)")
    te = (frame["split"] == "test").to_numpy()
    test_frame = frame[te].reset_index(drop=True)
    y = (test_frame["label"] == model.positive_class).to_numpy()
    p = calibrator.predict_proba(model.predict_proba(x[te])[:, [model.positive_index]])[:, 1]
    if report["smoothing_sigma_tiles"] is not None:
        p = smoothed(test_frame, p, report["smoothing_sigma_tiles"])
    units = patient_units(test_frame, y, p >= report["tau"])
    ci = bootstrap(pooled_f1, units, B=settings.bootstrap_resamples, seed=settings.seed)
    return {"reason": reason, "f1": vars(ci), "n_tiles": int(te.sum()), "n_patients": len(units)}


def coefficients(model: TumorHeadModel) -> np.ndarray:
    """Every learned number of the head, flattened (AC7 compares two fits with this)."""
    parts = [model.mean_, model.scale_, model.multinomial.coef_.ravel(), model.multinomial.intercept_]
    if model.binary is not None:
        parts += [model.binary.coef_.ravel(), model.binary.intercept_]
    return np.concatenate(parts)


def write_artifacts(out: Path, version: str, result: dict, meta: dict) -> dict:
    """``model.joblib``, ``calibrator.joblib``, ``scaler.json``, ``card.json``; returns the artefact hashes."""
    import sklearn

    out.mkdir(parents=True, exist_ok=True)
    model, calibrator, report = result["model"], result["calibrator"], result["report"]
    joblib.dump(model, out / "model.joblib")
    joblib.dump(calibrator, out / "calibrator.joblib")
    (out / "scaler.json").write_text(json.dumps(
        {"input": "l2-normalised Path Foundation embedding", "mean": model.mean_.tolist(), "scale": model.scale_.tolist()}
    ))
    hashes = {name: sha256_bytes((out / name).read_bytes()) for name in ("model.joblib", "calibrator.joblib", "scaler.json")}
    card = {
        "name": "tumor_head",
        "version": version,
        "spec": "SPEC-05 §4",
        "classes": list(model.classes_),
        "positive_class": model.positive_class,
        "artifacts_sha256": hashes,
        **meta,
        **report,
        "software": {"python": platform.python_version(), "sklearn": sklearn.__version__, "numpy": np.__version__},
    }
    (out / "card.json").write_text(json.dumps(card, indent=1, default=float))
    return hashes


def locked_splits(frame: pd.DataFrame, splits_path: Path, lock_path: Path, root: Path) -> pd.Series:
    """Each tile's split from the BCSS split file, after the lock check (SPEC-02 §5.2). Unassigned slides raise."""
    from eval.splits import LockMismatchError, verify_lock

    verify_lock(lock_path, root)
    locked = json.loads(Path(lock_path).read_text())
    if Path(splits_path).resolve() not in {(Path(root) / rel).resolve() for rel in locked}:
        raise LockMismatchError(f"{splits_path} is not listed in {lock_path}")
    split_of = pd.read_parquet(splits_path).set_index("slide_id")["split"]
    missing = sorted(set(frame["slide_id"]) - set(split_of.index))
    if missing:
        raise KeyError(f"slides without a locked split: {missing}")
    return frame["slide_id"].map(split_of)


def run(args) -> int:
    import pyarrow.parquet as pq

    from training.tumor_head.labels import label_space, load_config

    config = load_config()
    space = label_space(config)
    settings = FitSettings.from_config(config)
    table = pq.read_table(args.dataset)
    x = np.asarray(table.column("emb").combine_chunks().flatten().to_numpy(zero_copy_only=False), dtype=np.float32)
    frame = table.drop(["emb"]).to_pandas()
    x = x.reshape(len(frame), -1)
    frame["split"] = locked_splits(frame, args.splits, args.splits_lock, args.splits_root)
    dataset_sha = hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest()
    probe = joblib.load(args.v5_probe) if args.v5_probe else None

    result = train(frame, x, space.classes, space.positive_class, settings, probe=probe)
    if args.confirm_test_access:
        result["report"]["test"] = held_out_test_metrics(frame, x, result["model"], result["calibrator"], result["report"], settings,
                                                args.confirm_test_access)
    meta = {
        "train_snapshot_id": f"bcss_tiles@{dataset_sha[:12]}",  # SPEC-09 snapshots do not exist yet: the dataset hash stands in
        "dataset_sha256": dataset_sha,
        "splits_lock_sha256": hashlib.sha256(Path(args.splits_lock).read_bytes()).hexdigest(),
        "splits_file_sha256": hashlib.sha256(Path(args.splits).read_bytes()).hexdigest(),
        "seed": settings.seed,
        "config": config,
        "data": json.loads(Path(args.dataset_meta).read_text()) if args.dataset_meta else None,
    }
    hashes = write_artifacts(args.out, args.version, result, meta)
    print(json.dumps({"artifacts": hashes, "tau": result["report"]["tau"], "val": result["report"]["val"],
                      "ablations": result["report"]["ablations"]}, indent=1, default=float))
    return 0
