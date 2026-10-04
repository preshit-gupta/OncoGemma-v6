"""TCGA-BRCA manifests read from IDC DICOM (eval/datasets/idc.py; WP-8.7 prerequisite)."""
import hashlib

import pandas as pd
import pytest

from eval.datasets.idc import (
    LABEL_SOURCE,
    SERIES_MAP,
    SPLITS,
    IdcMappingError,
    build_manifest,
    container_id,
    dx_series,
    native_mag,
    series_uri,
)


def splits_frame():
    return pd.DataFrame({
        "file_id": ["f1", "f2", "f3", "f4"],
        "file_name": ["TCGA-AA-0001-01Z-00-DX1.ABC.svs", "TCGA-AA-0001-01Z-00-DX2.DEF.svs",
                      "TCGA-AA-0002-01Z-00-DX1.GHI.svs", "TCGA-AA-0003-01Z-00-DX1.JKL.svs"],
        "patient_id": ["TCGA-AA-0001", "TCGA-AA-0001", "TCGA-AA-0002", "TCGA-AA-0003"],
        "tss": ["AA"] * 4,
        "native_mag": ["40", "40", "unknown", "20"],
        "split": ["val", "val", "val", "test"],
    })


def labels_frame():
    return pd.DataFrame({
        "patient_id": ["TCGA-AA-0001", "TCGA-AA-0002"],
        "grade": pd.array([2, 3], dtype="Int64"), "total": pd.array([6, None], dtype="Int64"),
        "tubule": pd.array([2, None], dtype="Int64"), "pleo": pd.array([2, None], dtype="Int64"),
        "mitoses": pd.array([2, None], dtype="Int64"),
        "label_confidence": ["high", None], "excluded_reason": [None, "qa_pending"],
    })


def series_frame():
    return pd.DataFrame({
        "container_id": ["TCGA-AA-0001-01Z-00-DX1", "TCGA-AA-0001-01Z-00-DX2", "TCGA-AA-0002-01Z-00-DX1",
                         "TCGA-AA-0003-01Z-00-DX1"],
        "crdc_series_uuid": ["u1", "u2", "u3", "u4"],
    })


def fingerprint(uri):
    return hashlib.sha256(uri.encode()).hexdigest()


def build(split="val", series=None):
    return build_manifest(splits_frame(), labels_frame(), series_frame() if series is None else series, split,
                          fingerprint=fingerprint, specimen_type="resection", mpp_source="file")


def test_the_barcode_is_the_gdc_file_name_up_to_the_first_dot():
    assert container_id("TCGA-3C-AALI-01Z-00-DX1.F6E9A5DF-D8FB.svs") == "TCGA-3C-AALI-01Z-00-DX1"
    assert series_uri("abc") == "gs://idc-open-data/abc/"


def test_a_split_manifest_reads_every_slide_from_its_series():
    m = build()
    assert m["slide_id"].tolist() == ["f1", "f2", "f3"] and set(m["split"]) == {"val"}
    assert m["uri"].tolist() == ["gs://idc-open-data/u1/", "gs://idc-open-data/u2/", "gs://idc-open-data/u3/"]
    assert m["sha256"].tolist() == [fingerprint(u) for u in m["uri"]]
    assert set(m["dataset"]) == {"tcga_brca_dx"} and set(m["specimen_type"]) == {"resection"}
    # Ground truth from accepted labels only; a patient's grade applies to each of its slides.
    assert m["gt_grade"].tolist()[:2] == [2, 2] and pd.isna(m["gt_grade"].iloc[2])
    assert m["gt_label_source"].tolist()[:2] == [LABEL_SOURCE] * 2 and pd.isna(m["gt_label_source"].iloc[2])
    assert m["native_mag"].tolist()[:2] == [40.0, 40.0] and pd.isna(m["native_mag"].iloc[2])


def test_a_slide_without_a_series_is_an_error_not_a_dropped_row():
    with pytest.raises(IdcMappingError, match="1 slides have no IDC series"):
        build(series=series_frame().iloc[1:])
    with pytest.raises(IdcMappingError, match="no 'train' row"):
        build(split="train")


def test_native_magnification_unknown_is_null_and_nothing_else_is_guessed():
    assert native_mag("unknown") is None and native_mag("40") == 40.0
    with pytest.raises(ValueError):
        native_mag("forty")


def test_the_index_gives_one_series_per_diagnostic_slide():
    index = pd.DataFrame({
        "collection_id": ["tcga_brca", "tcga_brca", "tcga_brca", "tcga_luad"],
        "Modality": ["SM", "SM", "SM", "SM"],
        "ContainerIdentifier": ["TCGA-AA-0001-01Z-00-DX1", "TCGA-AA-0001-01A-01-TS1", "TCGA-AA-0002-01Z-00-DX1",
                                "TCGA-ZZ-0001-01Z-00-DX1"],
        "crdc_series_uuid": ["u1", "u9", "u2", "u8"],
        "SeriesInstanceUID": ["1.1", "1.9", "1.2", "1.8"],
    })
    table = dx_series(index, "v24")
    assert table["container_id"].tolist() == ["TCGA-AA-0001-01Z-00-DX1", "TCGA-AA-0002-01Z-00-DX1"]
    assert set(table["idc_version"]) == {"v24"}
    with pytest.raises(IdcMappingError, match="several IDC series"):
        dx_series(pd.concat([index, index.iloc[[0]]]), "v24")


def test_the_committed_map_covers_every_locked_slide_once():
    series = pd.read_parquet(SERIES_MAP)
    splits = pd.read_parquet(SPLITS)
    assert series["container_id"].is_unique and len(series) == 1133
    assert set(splits["file_name"].map(container_id)) <= set(series["container_id"])
