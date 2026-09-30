"""
Unit tests for BCSSAdapter parsing mask filenames, ROI offsets, real fetch streaming, and manifest generation.
SPEC-02 §3.4 and WP-5.2.
"""
import hashlib
from pathlib import Path
from PIL import Image
import pandas as pd
import pytest

from eval.datasets.base import FetchedFile
from eval.datasets.bcss import BCSSAdapter
from eval.datasets.storage import LocalStorage


def test_bcss_parse_mask_filename_with_bbox():
    adapter = BCSSAdapter()
    filename = "TCGA-A1-A0SK-01Z-00-DX1_1000_2000_3000_4000.png"
    parsed = adapter.parse_mask_filename(filename)

    assert parsed["slide_barcode"] == "TCGA-A1-A0SK-01Z-00-DX1"
    assert parsed["patient_id"] == "TCGA-A1-A0SK"
    assert parsed["roi_bbox_px"] == (1000, 2000, 3000, 4000)
    # default mpp = 0.25 -> 1000 * 0.25 = 250.0, etc.
    assert parsed["roi_bbox_um"] == (250.0, 500.0, 750.0, 1000.0)
    assert parsed["mask_mpp"] == 0.25


def test_bcss_parse_mask_filename_without_bbox():
    adapter = BCSSAdapter()
    filename = "TCGA-A1-A0SK-01Z-00-DX1_roi1.png"
    parsed = adapter.parse_mask_filename(filename)

    assert parsed["slide_barcode"] == "TCGA-A1-A0SK-01Z-00-DX1"
    assert parsed["patient_id"] == "TCGA-A1-A0SK"
    assert parsed["roi_bbox_px"] is None
    assert parsed["roi_bbox_um"] is None


def test_bcss_discover_files(tmp_path: Path):
    adapter = BCSSAdapter()

    # Create real mini mask images
    f1 = tmp_path / "TCGA-A1-A0SK-01Z-00-DX1_100_200_300_400.png"
    Image.new("L", (16, 16), color=1).save(f1)

    f2 = tmp_path / "TCGA-E2-A14X-01Z-00-DX1_500_600_700_800.png"
    Image.new("L", (16, 16), color=2).save(f2)

    df = adapter.discover(tmp_path)
    assert len(df) == 2
    assert "patient_id" in df.columns
    assert "slide_barcode" in df.columns
    assert "roi_bbox_um" in df.columns
    assert "mask_uri" in df.columns
    assert "mask_mpp" in df.columns

    p_ids = set(df["patient_id"].values)
    assert p_ids == {"TCGA-A1-A0SK", "TCGA-E2-A14X"}


def test_bcss_fetch_real_streaming_and_hashes(tmp_path: Path):
    adapter = BCSSAdapter()
    dest = LocalStorage(tmp_path / "dest")

    # Create real mask image
    mask_file = tmp_path / "TCGA-A1-A0SK-01Z-00-DX1_100_200_300_400.png"
    Image.new("L", (32, 32), color=1).save(mask_file)
    expected_bytes = mask_file.read_bytes()
    expected_sha256 = hashlib.sha256(expected_bytes).hexdigest().lower()
    expected_md5 = hashlib.md5(expected_bytes).hexdigest().lower()

    row = pd.Series({
        "slide_barcode": "TCGA-A1-A0SK-01Z-00-DX1",
        "mask_uri": mask_file.resolve().as_uri(),
    })

    fetched = adapter.fetch(row, dest)
    assert fetched.slide_id == "TCGA-A1-A0SK-01Z-00-DX1"
    assert fetched.sha256 == expected_sha256
    assert fetched.sha256 != "0" * 64
    assert fetched.md5 == expected_md5
    assert fetched.size_bytes == len(expected_bytes)
    assert dest.exists("bcss/masks/TCGA-A1-A0SK-01Z-00-DX1_100_200_300_400.png")


def test_bcss_fetch_missing_file_raises(tmp_path: Path):
    adapter = BCSSAdapter()
    dest = LocalStorage(tmp_path / "dest")

    row = pd.Series({
        "slide_barcode": "TCGA-MISSING-0001",
        "mask_uri": (tmp_path / "nonexistent.png").resolve().as_uri(),
    })

    with pytest.raises(FileNotFoundError, match="not found"):
        adapter.fetch(row, dest)


def test_bcss_labels():
    adapter = BCSSAdapter()
    df = adapter.labels()
    assert len(df) >= 6
    assert set(df["class_code"]) == {0, 1, 2, 3, 4, 5}


def test_bcss_to_manifest(tmp_path: Path):
    adapter = BCSSAdapter()
    mask_file = tmp_path / "mask.png"
    Image.new("L", (16, 16), color=1).save(mask_file)
    content = mask_file.read_bytes()
    real_sha256 = hashlib.sha256(content).hexdigest().lower()

    discovered = pd.DataFrame([
        {
            "patient_id": "TCGA-A1-A0SK",
            "slide_barcode": "TCGA-A1-A0SK-01Z-00-DX1",
            "roi_bbox_um": (250.0, 500.0, 750.0, 1000.0),
            "mask_uri": mask_file.resolve().as_uri(),
            "mask_mpp": 0.25,
        }
    ])

    fetched = {
        "TCGA-A1-A0SK-01Z-00-DX1": FetchedFile(
            slide_id="TCGA-A1-A0SK-01Z-00-DX1",
            uri=mask_file.resolve().as_uri(),
            sha256=real_sha256,
            md5=None,
            size_bytes=len(content),
        )
    }

    manifest_df = adapter.to_manifest(discovered, fetched)
    assert len(manifest_df) == 1
    assert manifest_df.iloc[0]["dataset"] == "bcss"
    assert manifest_df.iloc[0]["patient_id"] == "TCGA-A1-A0SK"
    assert manifest_df.iloc[0]["tss"] == "A1"
    assert manifest_df.iloc[0]["specimen_type"] == "resection"
    assert manifest_df.iloc[0]["regions_uri"] == mask_file.resolve().as_uri()
    assert manifest_df.iloc[0]["sha256"] == real_sha256
