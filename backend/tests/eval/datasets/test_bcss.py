"""
Unit tests for BCSSAdapter parsing mask filenames, ROI offsets, real fetch streaming, and manifest generation.
SPEC-02 §3.4 and WP-5.2.

Mask names follow BCSS download_crowdsource_dataset.py: "<slide>_xmin<X>_ymin<Y>_MPP-<mpp>.png",
with the first row of meta/roiBounds.csv (TCGA-A1-A0SK-DX1, xmin 45749, ymin 25055) as the example.
"""
import hashlib
from pathlib import Path
from PIL import Image
import pandas as pd
import pytest

from eval.datasets.base import DatasetConfigMissing, FetchedFile, load_config
from eval.datasets.bcss import BCSSAdapter, BCSSNameError
from eval.datasets.storage import LocalStorage

MASK_NAME = "TCGA-A1-A0SK-DX1_xmin45749_ymin25055_MPP-0.2500.png"


def test_bcss_parse_mask_filename():
    parsed = BCSSAdapter().parse_mask_filename(MASK_NAME)

    assert parsed["slide_barcode"] == "TCGA-A1-A0SK-DX1"
    assert parsed["patient_id"] == "TCGA-A1-A0SK"
    assert parsed["roi_origin_px"] == (45749, 25055)
    assert parsed["mask_mpp"] == 0.25


@pytest.mark.parametrize(
    "name",
    [
        "TCGA-A1-A0SK-01Z-00-DX1_1000_2000_3000_4000.png",  # not the BCSS form
        "TCGA-A1-A0SK-DX1_xmin45749_ymin25055_MAG-0.png",  # magnification download: mask µm/px unknown
        "mask.png",
    ],
)
def test_bcss_parse_mask_filename_rejects_other_forms(name: str):
    with pytest.raises(BCSSNameError, match="xmin"):
        BCSSAdapter().parse_mask_filename(name)


def test_bcss_roi_bbox_um_uses_slide_mpp_for_origin_and_mask_mpp_for_size():
    # Origin in slide base pixels (0.5 µm/px here), size in mask pixels at 0.25 µm/px.
    bbox = BCSSAdapter.roi_bbox_um((1000, 2000), (400, 200), mask_mpp=0.25, slide_mpp=0.5)
    assert bbox == (500.0, 1000.0, 600.0, 1050.0)


def test_bcss_discover_files(tmp_path: Path):
    adapter = BCSSAdapter()
    Image.new("L", (16, 8), color=1).save(tmp_path / MASK_NAME)
    Image.new("L", (16, 16), color=2).save(tmp_path / "TCGA-E2-A14X-DX1_xmin500_ymin600_MPP-0.2500.png")

    df = adapter.discover(tmp_path, slide_mpp={"TCGA-A1-A0SK-DX1": 0.2527})
    assert len(df) == 2
    by_slide = df.set_index("slide_barcode")
    assert set(df["patient_id"]) == {"TCGA-A1-A0SK", "TCGA-E2-A14X"}

    a0sk = by_slide.loc["TCGA-A1-A0SK-DX1"]
    assert a0sk["roi_origin_px"] == (45749, 25055)
    assert a0sk["roi_size_px"] == (16, 8)
    assert a0sk["mask_mpp"] == 0.25
    assert a0sk["roi_bbox_um"] == BCSSAdapter.roi_bbox_um((45749, 25055), (16, 8), 0.25, 0.2527)
    # No slide µm/px known for this slide, so no µm box.
    assert by_slide.loc["TCGA-E2-A14X-DX1"]["roi_bbox_um"] is None


def test_bcss_discover_rejects_misnamed_mask(tmp_path: Path):
    Image.new("L", (16, 16), color=1).save(tmp_path / "TCGA-A1-A0SK-01Z-00-DX1_100_200_300_400.png")
    with pytest.raises(BCSSNameError):
        BCSSAdapter().discover(tmp_path)


def test_bcss_discover_missing_directory_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        BCSSAdapter().discover(tmp_path / "absent")


def test_bcss_fetch_real_streaming_and_hashes(tmp_path: Path):
    adapter = BCSSAdapter()
    dest = LocalStorage(tmp_path / "dest")

    mask_file = tmp_path / MASK_NAME
    Image.new("L", (32, 32), color=1).save(mask_file)
    expected_bytes = mask_file.read_bytes()

    row = pd.Series({"slide_barcode": "TCGA-A1-A0SK-DX1", "mask_uri": mask_file.resolve().as_uri()})

    fetched = adapter.fetch(row, dest)
    assert fetched.slide_id == "TCGA-A1-A0SK-DX1"
    assert fetched.sha256 == hashlib.sha256(expected_bytes).hexdigest()
    assert fetched.md5 == hashlib.md5(expected_bytes).hexdigest()
    assert fetched.size_bytes == len(expected_bytes)
    assert dest.exists(f"bcss/masks/{MASK_NAME}")


def test_bcss_fetch_missing_file_raises(tmp_path: Path):
    adapter = BCSSAdapter()
    dest = LocalStorage(tmp_path / "dest")

    row = pd.Series({
        "slide_barcode": "TCGA-A1-A0SK-DX1",
        "mask_uri": (tmp_path / "nonexistent.png").resolve().as_uri(),
    })

    with pytest.raises(FileNotFoundError, match="not found"):
        adapter.fetch(row, dest)


def test_bcss_labels_match_published_gtruth_codes():
    df = BCSSAdapter().labels()
    codes = dict(zip(df["class_code"], df["class_name"]))
    assert set(codes) == set(range(22))
    # Spot checks against BCSS meta/gtruth_codes.tsv.
    assert codes[0] == "outside_roi"
    assert codes[3] == "lymphocytic_infiltrate"
    assert codes[5] == "glandular_secretions"
    assert codes[20] == "dcis"
    assert codes[21] == "other"


def test_bcss_missing_config_key_raises():
    config = dict(load_config()["bcss"])
    del config["specimen_type"]
    with pytest.raises(DatasetConfigMissing, match="specimen_type"):
        BCSSAdapter(config=config)


def test_bcss_to_manifest(tmp_path: Path):
    adapter = BCSSAdapter()
    mask_file = tmp_path / MASK_NAME
    Image.new("L", (16, 16), color=1).save(mask_file)
    content = mask_file.read_bytes()
    real_sha256 = hashlib.sha256(content).hexdigest()
    discovered = adapter.discover([mask_file])

    fetched = {
        "TCGA-A1-A0SK-DX1": FetchedFile(
            slide_id="TCGA-A1-A0SK-DX1",
            uri="gs://datasets/bcss/masks/" + MASK_NAME,
            sha256=real_sha256,
            md5=None,
            size_bytes=len(content),
        )
    }

    manifest_df = adapter.to_manifest(discovered, fetched)
    assert len(manifest_df) == 1
    row = manifest_df.iloc[0]
    assert row["dataset"] == "bcss"
    assert row["patient_id"] == "TCGA-A1-A0SK"
    assert row["tss"] == "A1"
    assert row["specimen_type"] == "resection"
    assert row["mpp_override"] == 0.25
    assert pd.isna(row["scanner"])
    # Ground truth points at the fetched copy, not the local source file.
    assert row["regions_uri"] == "gs://datasets/bcss/masks/" + MASK_NAME
    assert row["sha256"] == real_sha256
