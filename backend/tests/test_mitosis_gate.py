"""Tumour-cell gate and HPF tumour constraints (SPEC-06 §5.5, §5.8; WP-7.6b).

The gate reads the tumour mask triage persists (one pixel per 224 µm tile); an HPF is the circle of a
confirmed site (D22) and reports its tumour fraction for audit.
"""
import io
import json
import math
import uuid

import numpy as np
import pytest
from PIL import Image

from app.core.config import settings
from app.core.gcs import upload_blob_from_bytes
from app.core.pipeline_config import get_pipeline_config
from pipeline.hpf import hpfs_from_sites
from pipeline.mitosis_gate import TumorGate, TumorMaskMissingError, load_tumor_gate, tumor_mask_blob_names
from pipeline.scoring import is_counted
from pipeline.tissue_mask import TissueMask

TILE_UM = 224.0


def mask_artifacts(is_tumor: np.ndarray, tile_um: float = TILE_UM, head_version: str = "tumor_head@1.0.0") -> tuple[bytes, dict]:
    buf = io.BytesIO()
    Image.fromarray(np.where(is_tumor, 255, 0).astype(np.uint8), mode="L").save(buf, "PNG")
    meta = {"tile_um": tile_um, "origin_um": [0.0, 0.0], "nx": int(is_tumor.shape[1]), "ny": int(is_tumor.shape[0]),
            "head_version": head_version, "threshold": 0.5, "smoothing_sigma_tiles": None}
    return buf.getvalue(), meta


def save_tumor_mask(case_id, is_tumor: np.ndarray, tile_um: float = TILE_UM) -> None:
    """Write a triage tumour mask for a case, as worker/triage.py does."""
    png, meta = mask_artifacts(np.asarray(is_tumor, dtype=bool), tile_um)
    png_name, meta_name = tumor_mask_blob_names(case_id)
    upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, png_name, png, "image/png")
    upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, meta_name, json.dumps(meta).encode("utf-8"), "application/json")


def gate_of(is_tumor, dilation_tiles=1) -> TumorGate:
    png, meta = mask_artifacts(np.asarray(is_tumor, dtype=bool))
    return TumorGate.from_artifacts(png, meta, dilation_tiles)


def centre_of(i, j):
    return (i + 0.5) * TILE_UM, (j + 0.5) * TILE_UM


# -- the gate ------------------------------------------------------------------------------------------------------------------------

def test_a_candidate_is_in_tumour_in_the_mask_dilated_by_one_tile():
    is_tumor = np.zeros((9, 9), dtype=bool)
    is_tumor[4, 4] = True  # one tumour tile in stroma
    gate = gate_of(is_tumor)
    assert gate.in_tumor(*centre_of(4, 4))
    assert gate.in_tumor(*centre_of(5, 5)) and gate.in_tumor(*centre_of(3, 4))  # 8-connected neighbours (224 µm)
    assert not gate.in_tumor(*centre_of(6, 4)) and not gate.in_tumor(*centre_of(1, 1))  # stroma two tiles away
    assert not gate.in_tumor(-10.0, 50.0) and not gate.in_tumor(9 * TILE_UM + 1.0, 50.0)  # off the grid
    undilated = gate_of(is_tumor, dilation_tiles=0)
    assert undilated.in_tumor(*centre_of(4, 4)) and not undilated.in_tumor(*centre_of(5, 5))


def test_a_stroma_candidate_is_not_counted_until_a_pathologist_labels_it():
    is_tumor = np.zeros((9, 9), dtype=bool)
    is_tumor[0:3, 0:3] = True
    gate = gate_of(is_tumor)
    in_stroma = gate.in_tumor(*centre_of(7, 7))
    assert in_stroma is False
    assert not is_counted(None, "mitosis", in_stroma)
    assert is_counted("mitosis", "mitosis", in_stroma)  # the pathologist's label still counts
    assert is_counted(None, "mitosis", gate.in_tumor(*centre_of(1, 1)))


def test_the_summary_says_in_situ_exclusion_is_not_available():
    summary = gate_of(np.ones((2, 2), dtype=bool)).summary()
    assert summary["applied"] is True and summary["dilation_tiles"] == 1
    assert summary["in_situ_exclusion"] is False and "in-situ" in summary["in_situ_exclusion_reason"]


def test_a_case_without_a_tumour_mask_fails_loudly():
    with pytest.raises(TumorMaskMissingError, match="run the triage stage"):
        load_tumor_gate(uuid.uuid4(), 1)


def test_the_gate_reads_the_persisted_mask():
    case_id = uuid.uuid4()
    is_tumor = np.zeros((4, 5), dtype=bool)
    is_tumor[1, 2] = True
    save_tumor_mask(case_id, is_tumor)
    gate = load_tumor_gate(case_id, 0)
    assert gate.mask.array.tolist() == is_tumor.tolist() and gate.head_version == "tumor_head@1.0.0"


def test_a_mask_whose_size_disagrees_with_its_metadata_is_refused():
    png, meta = mask_artifacts(np.ones((3, 3), dtype=bool))
    with pytest.raises(Exception, match="metadata"):
        TumorGate.from_artifacts(png, {**meta, "nx": 4}, 1)


# -- HPFs ---------------------------------------------------------------------------------------------------------------------------

def square(cx, cy, side):
    h = side / 2
    return [[cx - h, cy - h], [cx + h, cy - h], [cx + h, cy + h], [cx - h, cy + h]]


def hpf_settings():
    return get_pipeline_config().specimen_profiles.profiles["resection"].hotspots


def site(hid, cx, cy, source="model"):
    hs = hpf_settings()
    return {"id": hid, "center_um": [cx, cy], "polygon_um": square(cx, cy, hs.frame_um), "source": source}


def test_an_hpf_reports_its_audit_fractions_over_the_circle():
    hs = hpf_settings()
    tissue = TissueMask(np.ones((400, 400), dtype=bool), 10.0)  # 4 x 4 mm of tissue
    is_tumor = np.zeros((18, 18), dtype=bool)
    is_tumor[:, :5] = True  # tumour only left of x = 1120 µm
    gate = gate_of(is_tumor)
    (hpf,) = hpfs_from_sites([site("hs_01", 800.0, 800.0)], [], tissue=tissue, tumor=gate, diameter_um=hs.hpf_diameter_um)
    assert hpf["tumor_fraction"] == gate.tumor_fraction_in_disk(800.0, 800.0, hs.hpf_radius_um)
    assert math.isclose(hpf["tissue_coverage"], tissue.fraction_in_disk_um(800.0, 800.0, hs.hpf_radius_um))
    # A site over stroma still becomes an HPF: the circle is the pathologist's, nothing is filtered here.
    (stroma,) = hpfs_from_sites([site("hs_u_1", 3000.0, 800.0, "pathologist_added")], [], tissue=tissue, tumor=gate,
                                diameter_um=hs.hpf_diameter_um)
    assert stroma["tumor_fraction"] == 0.0 and stroma["source"] == "pathologist"
