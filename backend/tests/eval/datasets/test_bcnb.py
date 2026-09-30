"""
Unit tests for BCNBAdapter, configuration contract, grade mapping, real fetch streaming, and pyramidal TIFF conversion.
SPEC-02 §3.2 and WP-5.2.
"""
import hashlib
from pathlib import Path
import pandas as pd
import pytest

from eval.datasets.base import DatasetConfigMissing, FetchedFile
from eval.datasets.bcnb import BCNBAdapter, convert_to_pyramidal_tiff
from eval.datasets.storage import LocalStorage


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


def test_bcnb_fetch_real_streaming_and_hashes(tmp_path: Path):
    valid_cfg = {
        "mpp": 0.25,
        "native_mag": 40.0,
        "image_glob": "WSIs/*.jpg",
        "clinical_file": str(tmp_path / "clinical.csv"),
        "grade_field": "grade",
        "grade_map": {"I": 1, "II": 2, "III": 3},
        "tumor_polygons": {"path": "poly.json", "format": "json"},
        "split_source": "official",
        "license_ref": "owner",
    }
    adapter = BCNBAdapter(config=valid_cfg)
    dest = LocalStorage(tmp_path / "dest")

    # Create real slide file
    slide_file = tmp_path / "BCNB_001.tif"
    payload = b"BCNB_SLIDE_MOCK_PAYLOAD_FOR_HASH_TEST" * 50
    slide_file.write_bytes(payload)

    expected_sha256 = hashlib.sha256(payload).hexdigest().lower()
    expected_md5 = hashlib.md5(payload).hexdigest().lower()

    row = pd.Series({
        "slide_id": "BCNB_001",
        "file_path": str(slide_file.resolve()),
    })

    fetched = adapter.fetch(row, dest)
    assert fetched.slide_id == "BCNB_001"
    assert fetched.sha256 == expected_sha256
    assert fetched.sha256 != "0" * 64
    assert fetched.md5 == expected_md5
    assert fetched.size_bytes == len(payload)
    assert dest.exists("bcnb/slides/BCNB_001.tif")


def test_bcnb_fetch_missing_file_raises(tmp_path: Path):
    valid_cfg = {
        "mpp": 0.25,
        "native_mag": 40.0,
        "image_glob": "WSIs/*.jpg",
        "clinical_file": str(tmp_path / "clinical.csv"),
        "grade_field": "grade",
        "grade_map": {"I": 1, "II": 2, "III": 3},
        "tumor_polygons": {"path": "poly.json", "format": "json"},
        "split_source": "official",
        "license_ref": "owner",
    }
    adapter = BCNBAdapter(config=valid_cfg)
    dest = LocalStorage(tmp_path / "dest")

    row = pd.Series({
        "slide_id": "BCNB_MISSING",
        "file_path": str(tmp_path / "nonexistent.tif"),
    })

    with pytest.raises(FileNotFoundError, match="not found"):
        adapter.fetch(row, dest)


def test_bcnb_labels_missing_file_raises(tmp_path: Path):
    valid_cfg = {
        "mpp": 0.25,
        "native_mag": 40.0,
        "image_glob": "WSIs/*.jpg",
        "clinical_file": str(tmp_path / "nonexistent_clinical.csv"),
        "grade_field": "grade",
        "grade_map": {"I": 1, "II": 2, "III": 3},
        "tumor_polygons": {"path": "poly.json", "format": "json"},
        "split_source": "official",
        "license_ref": "owner",
    }
    adapter = BCNBAdapter(config=valid_cfg)
    with pytest.raises(FileNotFoundError, match="not found"):
        adapter.labels()


def test_bcnb_manifest_generation(tmp_path: Path):
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

    real_hash = hashlib.sha256(b"bcnb-slide-test-content").hexdigest().lower()
    fetched = {
        "BCNB_001": FetchedFile(
            slide_id="BCNB_001",
            uri="file:///path/to/bcnb/slides/BCNB_001.tif",
            sha256=real_hash,
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
    assert manifest_df.iloc[0]["sha256"] == real_hash


def test_bcnb_pyvips_pyramidal_tiff_conversion(tmp_path: Path):
    pytest.importorskip("pyvips")

    jpg_path = tmp_path / "test.jpg"
    import pyvips  # type: ignore
    im = pyvips.Image.black(1024, 1024)
    im.write_to_file(str(jpg_path))

    out_path = tmp_path / "pyramid.tif"
    result = convert_to_pyramidal_tiff(jpg_path, out_path, mpp=0.25)
    assert Path(result).exists()
    assert out_path.stat().st_size > 0
