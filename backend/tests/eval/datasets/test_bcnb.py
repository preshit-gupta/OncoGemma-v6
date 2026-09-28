"""
Unit tests for BCNBAdapter, configuration contract, grade mapping, and pyramidal TIFF conversion.
SPEC-02 §3.2 and WP-5.2.
"""
from pathlib import Path
import pandas as pd
import pytest

from eval.datasets.base import DatasetConfigMissing, FetchedFile
from eval.datasets.bcnb import BCNBAdapter, convert_to_pyramidal_tiff


def test_bcnb_config_missing_raises():
    # Empty config raises DatasetConfigMissing listing missing keys
    with pytest.raises(DatasetConfigMissing) as exc_info:
        BCNBAdapter(config={})

    missing = exc_info.value.missing_keys
    assert "mpp" in missing
    assert "clinical_file" in missing
    assert "grade_map" in missing
    assert "tumor_polygons" in missing


def test_bcnb_valid_config():
    valid_cfg = {
        "mpp": 0.25,
        "native_mag": 40.0,
        "image_glob": "WSIs/*.jpg",
        "clinical_file": "clinical.csv",
        "grade_field": "histological_grade",
        "grade_map": {"I": 1, "II": 2, "III": 3},
        "tumor_polygons": {"path": "polygons.json", "format": "geojson"},
        "split_source": "official",
        "license_ref": "pending_owner",
    }
    adapter = BCNBAdapter(config=valid_cfg)
    assert adapter.config["mpp"] == 0.25


def test_bcnb_grade_mapping_and_exclusions():
    valid_cfg = {
        "mpp": 0.25,
        "native_mag": 40.0,
        "image_glob": "WSIs/*.jpg",
        "clinical_file": "clinical.csv",
        "grade_field": "grade",
        "grade_map": {"1": 1, "2": 2, "3": 3, "I": 1, "II": 2, "III": 3},
        "tumor_polygons": {"path": "poly.json", "format": "json"},
        "split_source": "official",
        "license_ref": "owner",
    }
    adapter = BCNBAdapter(config=valid_cfg)

    clinical_data = pd.DataFrame([
        {"patient_id": "P1", "grade": "II"},
        {"patient_id": "P2", "grade": "III"},
        {"patient_id": "P3", "grade": "unknown_grade"},  # Unmapped
        {"patient_id": "P4", "grade": None},             # Missing
    ])

    valid_df, excluded_df = adapter.map_grades(clinical_data)

    assert len(valid_df) == 2
    assert list(valid_df["gt_grade"]) == [2, 3]

    assert len(excluded_df) == 2
    assert "Unmapped grade value: unknown_grade" in excluded_df.iloc[0]["excluded_reason"]
    assert "Missing grade value" in excluded_df.iloc[1]["excluded_reason"]


def test_bcnb_manifest_generation():
    valid_cfg = {
        "mpp": 0.25,
        "native_mag": 40.0,
        "image_glob": "WSIs/*.jpg",
        "clinical_file": "clinical.csv",
        "grade_field": "grade",
        "grade_map": {"I": 1, "II": 2, "III": 3},
        "tumor_polygons": {"path": "poly.json", "format": "json"},
        "split_source": "official",
        "license_ref": "owner",
    }
    adapter = BCNBAdapter(config=valid_cfg)

    discovered = pd.DataFrame([
        {
            "patient_id": "BCNB_001",
            "slide_id": "BCNB_001",
            "file_path": "WSIs/BCNB_001.jpg",
            "mpp": 0.25,
            "native_mag": 40.0,
            "gt_grade": 2,
        }
    ])

    fetched = {
        "BCNB_001": FetchedFile(
            slide_id="BCNB_001",
            uri="file:///path/to/bcnb/slides/BCNB_001.tif",
            sha256="f" * 64,
            md5=None,
            size_bytes=10000,
        )
    }

    manifest_df = adapter.to_manifest(discovered, fetched)
    assert len(manifest_df) == 1
    assert manifest_df.iloc[0]["dataset"] == "bcnb"
    assert manifest_df.iloc[0]["specimen_type"] == "core_biopsy"
    assert manifest_df.iloc[0]["mpp_override"] == 0.25
    assert manifest_df.iloc[0]["gt_grade"] == 2


def test_bcnb_pyvips_pyramidal_tiff_conversion(tmp_path: Path):
    pytest.importorskip("pyvips")

    # If pyvips is available, test conversion
    jpg_path = tmp_path / "test.jpg"
    # Create small dummy image
    import pyvips  # type: ignore
    im = pyvips.Image.black(1024, 1024)
    im.write_to_file(str(jpg_path))

    out_path = tmp_path / "pyramid.tif"
    result = convert_to_pyramidal_tiff(jpg_path, out_path, mpp=0.25)
    assert Path(result).exists()
    assert out_path.stat().st_size > 0
