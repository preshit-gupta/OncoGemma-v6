"""Offline parts of the BCSS tile-set builder (WP-6.2)."""
import hashlib
import json

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from training.tumor_head.dataset import (
    SlideMppMissingError,
    roi_sources,
    slide_mpp_from_description,
    tile_od_sum,
    write_dataset,
)


def test_od_sum_of_white_is_zero_and_darker_is_larger():
    white = np.full((4, 4, 3), 255, np.uint8)
    grey = np.full((4, 4, 3), 128, np.uint8)
    assert tile_od_sum(white) == 0.0
    assert tile_od_sum(grey) == pytest.approx(3 * -np.log10(128 / 255))


def test_slide_mpp_comes_from_the_aperio_description():
    assert slide_mpp_from_description("Aperio|AppMag = 40|MPP = 0.2521|x") == 0.2521
    with pytest.raises(SlideMppMissingError):
        slide_mpp_from_description("Aperio Image Library v12.1.3 J2K/KDU Q=70")


def _masks(tmp_path, names):
    hashes = {}
    for name in names:
        path = tmp_path / f"{name}_xmin1_ymin2_base.png"
        path.write_bytes(name.encode())
        hashes[name] = {"sha256": hashlib.sha256(name.encode()).hexdigest()}
    (tmp_path / "SHA256.json").write_text(json.dumps(hashes))


def test_roi_sources_check_hashes_slides_and_exclusions(tmp_path):
    names = ["TCGA-AA-0001-DX1", "TCGA-AA-0002-DX1"]
    _masks(tmp_path, names)
    bounds = tmp_path / "b.csv"
    pd.DataFrame({"s": names, "xmin": 1, "ymin": 2, "xmax": 11, "ymax": 22}).set_index("s").to_csv(bounds)
    dx = pd.DataFrame({"file_name": ["TCGA-AA-0001-01Z-00-DX1.U1.svs"], "file_id": ["f1"]})
    with pytest.raises(KeyError, match="no open-access GDC"):
        roi_sources(bounds, tmp_path, dx)
    (source,) = roi_sources(bounds, tmp_path, dx, frozenset({"TCGA-AA-0002-DX1"}))
    assert (source.file_id, source.origin_px, source.size_px) == ("f1", (1, 2), (10, 20))
    (tmp_path / f"{names[0]}_xmin1_ymin2_base.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="sha256"):
        roi_sources(bounds, tmp_path, dx, frozenset({"TCGA-AA-0002-DX1"}))


def test_write_dataset_keeps_rows_and_embeddings_together(tmp_path):
    frame = pd.DataFrame({
        "slide_id": ["S2", "S1", "S1"], "patient_id": ["P2", "P1", "P1"], "file_id": ["f2", "f1", "f1"],
        "i": [0, 1, 0], "j": [0, 0, 0], "x_um": [0.0, 224.0, 0.0], "y_um": [0.0] * 3, "label": ["stroma"] * 3,
        "annotated_fraction": [1.0] * 3, "majority_fraction": [1.0] * 3, "od_sum": [0.1, 0.2, 0.3],
        "frac_stroma": [1.0] * 3,
    })
    emb = np.arange(9, dtype=np.float32).reshape(3, 3)
    write_dataset([frame], [emb], tmp_path / "d.parquet")
    table = pq.read_table(tmp_path / "d.parquet")
    out = table.drop(["emb"]).to_pandas()
    vectors = np.stack(table.column("emb").to_pylist())
    assert list(zip(out["slide_id"], out["i"])) == [("S1", 0), ("S1", 1), ("S2", 0)]
    for (_, row), vector in zip(out.iterrows(), vectors):
        original = frame.index[(frame["slide_id"] == row["slide_id"]) & (frame["i"] == row["i"])][0]
        np.testing.assert_array_equal(vector, emb[original])
