"""
Tests for manifest validation and schema compliance.
SPEC-02 §5.1 and WP-5.2.
"""
import pandas as pd
import pytest

from eval.datasets.manifest import (
    MANIFEST_COLUMNS,
    empty_manifest,
    validate_manifest,
)


def make_valid_manifest_row(**kwargs) -> dict:
    base = {
        "dataset": "tcga_brca_dx",
        "patient_id": "TCGA-A7-A0DC",
        "slide_id": "dx-slide-001",
        "uri": "file:///path/to/dx-slide-001.svs",
        "sha256": "a" * 64,
        "specimen_type": "resection",
        "mpp_override": 0.25,
        "mpp_source": "dataset_doc",
        "native_mag": 40.0,
        "scanner": "Aperio",
        "tss": "A7",
        "split": None,  # Split can be null before WP-5.3
        "gt_grade": 2,
        "gt_total": 6,
        "gt_tubule": 2,
        "gt_pleo": 2,
        "gt_mitoses": 2,
        "gt_histotype": "IDC-NST",
        "gt_label_source": "report_extract",
        "gt_label_confidence": "high",
        "regions_uri": None,
    }
    base.update(kwargs)
    return base


def test_valid_manifest_passes():
    row1 = make_valid_manifest_row()
    row2 = make_valid_manifest_row(
        patient_id="TCGA-BH-A0BQ",
        slide_id="dx-slide-002",
        split="train",
        gt_grade=3,
        gt_total=8,
        gt_tubule=3,
        gt_pleo=3,
        gt_mitoses=2,
    )
    df = pd.DataFrame([row1, row2])
    # Should not raise
    validate_manifest(df)


def test_empty_manifest_helper():
    df = empty_manifest()
    assert list(df.columns) == list(MANIFEST_COLUMNS.keys())
    assert len(df) == 0


def test_validation_empty_manifest_fails():
    df = empty_manifest()
    with pytest.raises(ValueError, match="Manifest is empty"):
        validate_manifest(df)


def test_validation_missing_columns():
    df = pd.DataFrame([{"dataset": "tcga_brca_dx", "patient_id": "P1"}])
    with pytest.raises(ValueError, match="Missing required manifest columns"):
        validate_manifest(df)


def test_validation_duplicate_slide_ids():
    row1 = make_valid_manifest_row(slide_id="dup-slide")
    row2 = make_valid_manifest_row(slide_id="dup-slide")
    df = pd.DataFrame([row1, row2])
    with pytest.raises(ValueError, match="Duplicate"):
        validate_manifest(df)


def test_validation_invalid_sha256():
    row = make_valid_manifest_row(sha256="not-a-valid-sha256")
    df = pd.DataFrame([row])
    with pytest.raises(ValueError, match="sha256"):
        validate_manifest(df)


def test_validation_invalid_enums():
    # Invalid specimen_type
    row_st = make_valid_manifest_row(specimen_type="blood_sample")
    with pytest.raises(ValueError, match="specimen_type"):
        validate_manifest(pd.DataFrame([row_st]))

    # Invalid mpp_source
    row_ms = make_valid_manifest_row(mpp_source="magic_guess")
    with pytest.raises(ValueError, match="mpp_source"):
        validate_manifest(pd.DataFrame([row_ms]))

    # Invalid split
    row_sp = make_valid_manifest_row(split="holdout_extra")
    with pytest.raises(ValueError, match="split"):
        validate_manifest(pd.DataFrame([row_sp]))


def test_validation_nottingham_inconsistencies():
    # 1. Total score mismatch: 2+2+2 = 6, but total=7
    row_sum = make_valid_manifest_row(gt_tubule=2, gt_pleo=2, gt_mitoses=2, gt_total=7)
    with pytest.raises(ValueError, match="Nottingham total mismatch"):
        validate_manifest(pd.DataFrame([row_sum]))

    # 2. Grade band mismatch: Grade 1 but total score is 7 (Grade 1 requires 3-5)
    row_grade = make_valid_manifest_row(gt_grade=1, gt_total=7, gt_tubule=2, gt_pleo=3, gt_mitoses=2)
    with pytest.raises(ValueError, match="Inconsistent grade 1 with total score 7"):
        validate_manifest(pd.DataFrame([row_grade]))
