"""WP-7.8: the locked MIDOG++ breast split and the multi-image mitosis baseline (fake detector, synthetic images)."""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml
from PIL import Image

import eval.mitosis_baseline as mb
from app.core.pipeline_config import get_pipeline_config
from eval.datasets.base import load_registry
from eval.make_splits import (
    MIDOGPP_PINNED_VAL,
    MIDOGppSplitError,
    main as make_splits_main,
    midogpp_breast_frame,
)
from eval.metrics import CaseMitosis, RegionPoints, mitosis_f1
from eval.splits import verify_lock, write_lock

BACKEND = Path(mb.__file__).parents[1]
SPLITS = BACKEND / "eval" / "splits"
# The WP-6.2 entries; adding MIDOG++ must not change them.
TCGA_BCSS_LOCK = {
    "eval/splits/bcss.parquet": "a7747269842b1925093aece714052ab87599b588b4c3b0a4513ec52afeda0d81",
    "eval/splits/tcga_brca_dx.parquet": "fa5859cf38f858a841b3e01f5d31935a1737c89ec5eaeabbc6d4751455fd40ef",
}
SCANNERS = ("Hamammatsu XR", "Hamamatsu S360", "Aperio CS2")
MPP = 0.25


# -- the committed split -------------------------------------------------------------------------------------------------------------

def test_the_committed_midogpp_split_is_locked_case_level_and_by_scanner():
    lock = json.loads((SPLITS / "SPLITS.lock").read_text(encoding="utf-8"))
    assert {k: lock[k] for k in TCGA_BCSS_LOCK} == TCGA_BCSS_LOCK
    assert "eval/splits/midogpp_breast.parquet" in lock
    verify_lock(SPLITS / "SPLITS.lock", BACKEND)
    frame = pd.read_parquet(SPLITS / "midogpp_breast.parquet")
    assert len(frame) == 150 and frame["image_id"].is_unique and frame["patient_id"].is_unique  # one row per case
    assert set(frame["split"]) == {"val", "test"} and set(frame["scanner"]) == set(SCANNERS)
    assert frame.set_index("file_name").loc[list(MIDOGPP_PINNED_VAL), "split"].eq("val").all()
    drawn = frame[~frame["file_name"].isin(MIDOGPP_PINNED_VAL)]
    for _, per_scanner in drawn.groupby("scanner"):
        counts = per_scanner["split"].value_counts()
        assert abs(counts["val"] - counts["test"]) <= 1
    assert (frame["seed"] == 20260928).all()


def test_the_registry_cites_the_ground_truth_convention_and_scanner_source():
    entry = load_registry()["midogpp_breast"]
    assert "no harmonisation" in entry["gt_dividing_cell_convention"]
    assert "datasets_xvalidation.csv" in entry["scanner_source"]
    assert "figshare" in entry["source_url"]


# -- making the split ------------------------------------------------------------------------------------------------------------------

def synthetic_labels(slides: range, mf_per_image: int = 2) -> tuple[dict, pd.DataFrame]:
    """A MIDOG++-shaped label file and scanner table: ``slides`` are breast, a canine image is not."""
    images, annotations = [], []
    for k, slide in enumerate(slides):
        images.append({"id": slide, "file_name": f"{slide:03d}.tiff", "width": 400, "height": 300,
                       "tumor_type": "human breast cancer"})
        for j in range(mf_per_image):
            annotations.append({"id": len(annotations), "image_id": slide, "category_id": 1,
                                "bbox": [50 + 100 * j, 50, 100 + 100 * j, 100]})
        annotations.append({"id": len(annotations), "image_id": slide, "category_id": 2, "bbox": [300, 200, 350, 250]})
    images.append({"id": 999, "file_name": "999.tiff", "width": 400, "height": 300, "tumor_type": "canine lymphosarcoma"})
    labels = {"images": images, "annotations": annotations,
              "categories": [{"id": 1, "name": "mitotic figure"}, {"id": 2, "name": "not mitotic figure"}]}
    table = pd.DataFrame({"Slide": list(slides), "Dataset": "train", "Tumor": "human breast cancer",
                          "Scanner": [SCANNERS[k % 3] for k in range(len(slides))]})
    table.loc[len(table)] = [999, "train", "canine lymphosarcoma", "Aperio CS2"]
    return labels, table


def test_the_midogpp_subcommand_adds_its_split_to_the_lock_and_keeps_the_others(tmp_path):
    labels, table = synthetic_labels(range(80, 110))  # includes 094
    (tmp_path / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
    table.to_csv(tmp_path / "scanners.csv", sep=";", index=False)
    out = tmp_path / "eval" / "splits"
    out.mkdir(parents=True)
    other = out / "tcga_brca_dx.parquet"
    pd.DataFrame({"patient_id": ["p1"], "split": ["train"]}).to_parquet(other)
    before = write_lock([other], out / "SPLITS.lock", tmp_path)
    argv = ["midogpp", "--labels", str(tmp_path / "labels.json"), "--scanners", str(tmp_path / "scanners.csv"),
            "--out-dir", str(out), "--lock-root", str(tmp_path), "--seed", "7"]
    assert make_splits_main(argv) == 0
    lock = json.loads((out / "SPLITS.lock").read_text(encoding="utf-8"))
    assert lock["eval/splits/tcga_brca_dx.parquet"] == before["eval/splits/tcga_brca_dx.parquet"]
    verify_lock(out / "SPLITS.lock", tmp_path)
    frame = pd.read_parquet(out / "midogpp_breast.parquet")
    assert len(frame) == 30 and 999 not in set(frame["image_id"])  # breast only
    assert frame.set_index("file_name").loc["094.tiff", "split"] == "val"
    assert set(frame["split"]) <= {"val", "test"} and (frame["n_mf"] == 2).all() and (frame["n_imposter"] == 1).all()
    again = tmp_path / "again"
    again.mkdir()
    write_lock([other], again / "SPLITS.lock", tmp_path)
    assert make_splits_main(argv[:5] + ["--out-dir", str(again), "--lock-root", str(tmp_path), "--seed", "7"]) == 0
    assert pd.read_parquet(again / "midogpp_breast.parquet")["split"].tolist() == frame["split"].tolist()  # deterministic


def test_the_lock_must_already_match_before_midogpp_is_added(tmp_path):
    labels, table = synthetic_labels(range(80, 110))
    (tmp_path / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
    table.to_csv(tmp_path / "scanners.csv", sep=";", index=False)
    out = tmp_path / "eval" / "splits"
    out.mkdir(parents=True)
    other = out / "tcga_brca_dx.parquet"
    other.write_bytes(b"one")
    write_lock([other], out / "SPLITS.lock", tmp_path)
    other.write_bytes(b"changed")
    with pytest.raises(Exception, match="mismatch"):
        make_splits_main(["midogpp", "--labels", str(tmp_path / "labels.json"), "--scanners", str(tmp_path / "scanners.csv"),
                          "--out-dir", str(out), "--lock-root", str(tmp_path), "--seed", "7"])


def test_a_scanner_table_slide_missing_from_the_labels_raises():
    labels, table = synthetic_labels(range(80, 110))
    labels["images"] = [i for i in labels["images"] if i["id"] != 81]
    with pytest.raises(MIDOGppSplitError, match="not in the labels"):
        midogpp_breast_frame(labels, table)


# -- the multi-image baseline ----------------------------------------------------------------------------------------------------------

def det_cfg():
    return get_pipeline_config().mitosis.detector


def write_image(path: Path, size=(400, 300)) -> None:
    """A blank TIFF with resolution tags at MPP µm/px (what eval.datasets.midogpp.image_mpp reads)."""
    Image.new("RGB", size, "white").save(path, tiffinfo={282: 1e4 / MPP, 283: 1e4 / MPP, 296: 3})


def point(x_um, y_um, prob):
    return {"x_um": x_um, "y_um": y_um, "prob": prob, "tile_id": "t", "record_id": "r"}


def gt_um(labels: dict, image_id: int) -> np.ndarray:
    boxes = [a["bbox"] for a in labels["annotations"] if a["image_id"] == image_id and a["category_id"] == 1]
    return np.array([((x0 + x1) / 2 * MPP, (y0 + y1) / 2 * MPP) for x0, y0, x1, y1 in boxes]).reshape(-1, 2)


class FakeDetector:
    """Stage-A points per image: the first figure found twice (one NMS survivor); odd images also find the second,
    even images get it below τ plus a confident false positive."""

    def __init__(self, labels: dict):
        self.labels = labels
        self.calls: list[str] = []

    def __call__(self, path: Path) -> list[dict]:
        self.calls.append(path.name)
        image_id = int(path.stem)
        tau, r = det_cfg().det_threshold, det_cfg().nms_radius_um
        (x1, y1), (x2, y2) = gt_um(self.labels, image_id)
        points = [point(x1, y1, 0.95), point(x1 + r / 2, y1, 0.90)]  # one figure, two points closer than the NMS radius
        if image_id % 2:
            points.append(point(x2 + 1.0, y2, 0.99))                    # odd images find the second figure
        else:
            points += [point(x2, y2, tau - 0.01),                       # even images miss it: below τ
                       point(80.0, 60.0, tau + 0.05)]                   # and get a false positive
        return points


@pytest.fixture
def midog_set(tmp_path):
    labels, table = synthetic_labels(range(80, 110))
    frame = midogpp_breast_frame(labels, table)
    for name in frame["file_name"]:
        write_image(tmp_path / name)
    return labels, frame, tmp_path


def test_pooled_metrics_equal_the_per_image_sums(midog_set):
    labels, frame, images = midog_set
    detector = FakeDetector(labels)
    results = mb.run_validation(frame, images, labels, detector, det_cfg())
    assert len(results) == len(frame) == len(detector.calls)
    pooled = mb.pooled(results)
    for k in ("tp", "fp", "fn"):
        assert pooled[k] == sum(getattr(r, k) for r in results)
    odd = [r for r in results if int(r.file_name[:3]) % 2]
    assert all((r.tp, r.fp, r.fn, r.n_pred) == (2, 0, 0, 2) for r in odd)
    assert all((r.tp, r.fp, r.fn, r.n_pred) == (1, 1, 1, 2) for r in results if r not in odd)
    cases = [CaseMitosis(r.file_name, [RegionPoints(
        gt_um(labels, int(r.file_name[:3])),
        np.array([c["centroid_um"] for c in mb.production_candidates(detector(images / r.file_name), det_cfg())]).reshape(-1, 2),
    )]) for r in results]
    reference = mitosis_f1(cases, radius_um=mb.MATCH_RADIUS_UM)
    assert (reference.tp, reference.fp, reference.fn) == (pooled["tp"], pooled["fp"], pooled["fn"])
    assert math.isclose(reference.f1, pooled["f1"]) and math.isclose(reference.precision, pooled["precision"])


def test_count_error_is_per_2_mm2_of_the_image(midog_set):
    labels, frame, images = midog_set
    results = mb.run_validation(frame, images, labels, FakeDetector(labels), det_cfg())
    area = 400 * 300 * MPP * MPP / 1e6
    assert all(math.isclose(r.area_mm2, area) for r in results)
    assert math.isclose(results[1].count_error, (results[1].n_pred - results[1].n_gt) * 2.0 / area)
    summary = mb.summarise(results)
    assert math.isclose(summary["count_signed"].point, float(np.mean([r.count_error for r in results])))
    assert summary["f1_ci"].low <= summary["f1"] <= summary["f1_ci"].high


def test_settings_are_read_from_configs_mitosis_yaml_and_cannot_be_overridden():
    raw = yaml.safe_load((BACKEND.parent / "configs" / "mitosis.yaml").read_text(encoding="utf-8"))["detector"]
    cfg = det_cfg()
    assert (cfg.det_threshold, cfg.nms_radius_um) == (raw["det_threshold"], raw["nms_radius_um"])
    tau, r = cfg.det_threshold, cfg.nms_radius_um
    kept = mb.production_candidates([point(0.0, 0.0, tau), point(r * 0.9, 0.0, tau + 0.1),  # closer than r: one survives
                                     point(100.0, 0.0, tau - 1e-6), point(200.0, 0.0, 0.99)], cfg)
    assert sorted(c["p_a"] for c in kept) == [tau + 0.1, 0.99]  # NMS keeps the higher p_a; below τ is dropped
    options = [o for action in mb.build_parser()._actions for o in action.option_strings]
    assert not [o for o in options if any(w in o for w in ("thresh", "tau", "nms", "radius", "config"))]


def test_the_test_split_needs_the_owners_approval():
    with pytest.raises(SystemExit, match="owner approves"):
        mb.split_images("test", owner_approved_test=False)
    val = mb.split_images("val", owner_approved_test=False)
    assert set(val["split"]) == {"val"} and "094.tiff" in set(val["file_name"])


def test_a_split_file_not_in_the_lock_is_refused(tmp_path):
    frame = pd.read_parquet(SPLITS / "midogpp_breast.parquet")
    frame.assign(split="val").to_parquet(tmp_path / "midogpp_breast.parquet")
    with pytest.raises(SystemExit, match="is not in"):
        mb.split_images("val", False, split_file=tmp_path / "midogpp_breast.parquet")


def test_missing_images_fail_before_any_detector_call(midog_set):
    labels, frame, images = midog_set
    (images / frame["file_name"].iloc[3]).unlink()
    detector = FakeDetector(labels)
    with pytest.raises(FileNotFoundError, match="not in"):
        mb.run_validation(frame, images, labels, detector, det_cfg())
    assert detector.calls == []


def test_an_image_whose_size_disagrees_with_the_labels_is_refused(midog_set):
    labels, frame, images = midog_set
    write_image(images / frame["file_name"].iloc[0], size=(401, 300))
    with pytest.raises(ValueError, match="the labels say"):
        mb.run_validation(frame, images, labels, FakeDetector(labels), det_cfg())


def test_the_report_says_regression_check_and_stops_below_the_floor(midog_set):
    labels, frame, images = midog_set
    results = mb.run_validation(frame, images, labels, FakeDetector(labels), det_cfg())
    lines = mb.validation_report(results, "val", ["- meta"])
    text = "\n".join(lines)
    assert "regression check, not validation" in text and "| All | 30 |" in text
    assert all(f"| {s} | 10 |" in text for s in SCANNERS)  # per scanner
    assert "STOP" not in text and "Pooled NS-M 0.750" in text  # TP 45, FP 15, FN 15
    poor = [mb.ImageResult(r.file_name, r.scanner, r.area_mm2, r.n_gt, r.n_pred, 0, r.n_pred, r.n_gt) for r in results]
    assert "**STOP: pooled NS-M 0.000" in "\n".join(mb.validation_report(poor, "val", []))
