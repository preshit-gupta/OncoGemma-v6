"""
Unit tests for BCSSAdapter parsing mask filenames, ROI offsets, and manifest generation.
SPEC-02 §3.4 and WP-5.2.
"""
from pathlib import Path
import pandas as pd
import pytest

from eval.datasets.base import FetchedFile
from eval.datasets.bcss import BCSSAdapter


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


def test_bcss_discover_synthetic_files(tmp_path: Path):
    adapter = BCSSAdapter()

    # Create synthetic mask files
    f1 = tmp_path / "TCGA-A1-A0SK-01Z-00-DX1_100_200_300_400.png"
    f1.write_bytes(b"\x89PNG\r\n\x1a\n")

    f2 = tmp_path / "TCGA-E2-A14X-01Z-00-DX1_500_600_700_800.png"
    f2.write_bytes(b"\x89PNG\r\n\x1a\n")

    df = adapter.discover(tmp_path)
    assert len(df) == 2
    assert "patient_id" in df.columns
    assert "slide_barcode" in df.columns
    assert "roi_bbox_um" in df.columns
    assert "mask_uri" in df.columns
    assert "mask_mpp" in df.columns

    p_ids = set(df["patient_id"].values)
    assert p_ids == {"TCGA-A1-A0SK", "TCGA-E2-A14X"}


def test_bcss_to_manifest():
    adapter = BCSSAdapter()
    discovered = pd.DataFrame([
        {
            "patient_id": "TCGA-A1-A0SK",
            "slide_barcode": "TCGA-A1-A0SK-01Z-00-DX1",
            "roi_bbox_um": (250.0, 500.0, 750.0, 1000.0),
            "mask_uri": "file:///path/to/bcss/mask1.png",
            "mask_mpp": 0.25,
        }
    ])

    fetched = {
        "TCGA-A1-A0SK-01Z-00-DX1": FetchedFile(
            slide_id="TCGA-A1-A0SK-01Z-00-DX1",
            uri="file:///path/to/bcss/mask1.png",
            sha256="e" * 64,
            md5=None,
            size_bytes=2048,
        )
    }

    manifest_df = adapter.to_manifest(discovered, fetched)
    assert len(manifest_df) == 1
    assert manifest_df.iloc[0]["dataset"] == "bcss"
    assert manifest_df.iloc[0]["patient_id"] == "TCGA-A1-A0SK"
    assert manifest_df.iloc[0]["specimen_type"] == "resection"
    assert manifest_df.iloc[0]["regions_uri"] == "file:///path/to/bcss/mask1.png"
