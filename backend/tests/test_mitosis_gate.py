"""Tumour-cell gate and HPF tumour constraints (SPEC-06 §5.5, §5.8; WP-7.6b).

The gate reads the tumour mask triage persists (one pixel per 224 µm tile); HPFs are one per
hotspot window with the disk inside it (owner decision 2026-10-04), tumour fraction >= 0.5.
"""
import io
import json
import math
import uuid

import numpy as np
import pytest
from hypothesis import given, settings as hsettings, strategies as st
from PIL import Image

from app.core.config import settings
from app.core.gcs import upload_blob_from_bytes
from app.core.pipeline_config import get_pipeline_config
from pipeline.hpf import place_hpfs, window_centres
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


# -- HPFs ----------------------------------------------------------------------------------------------------------------------------

def square(cx, cy, side):
    h = side / 2
    return [[cx - h, cy - h], [cx + h, cy - h], [cx + h, cy + h], [cx - h, cy + h]]


def hpf_settings():
    config = get_pipeline_config()
    return config.mitosis.hpf, config.specimen_profiles.profiles["resection"].hotspots


def test_a_window_leaves_the_centre_a_76_um_square():
    cfg, hs = hpf_settings()
    centres = window_centres(square(1000.0, 1000.0, hs.window_um), cfg.radius_um, cfg.centre_step_um)
    half = hs.window_um / 2 - cfg.radius_um  # 38 µm
    assert (1000.0, 1000.0) in centres
    assert all(abs(x - 1000.0) <= half + 1e-6 and abs(y - 1000.0) <= half + 1e-6 for x, y in centres)
    assert window_centres(square(1000.0, 1000.0, 500.0), cfg.radius_um, cfg.centre_step_um) == []  # too small for a disk


def test_an_hpf_is_never_placed_with_tumour_fraction_below_one_half():
    cfg, hs = hpf_settings()
    tissue = TissueMask(np.ones((400, 400), dtype=bool), 10.0)  # 4 x 4 mm of tissue
    is_tumor = np.zeros((18, 18), dtype=bool)
    is_tumor[:, :5] = True  # tumour only left of x = 1120 µm
    gate = gate_of(is_tumor)
    windows = [(square(800.0, 800.0, hs.window_um), 0.9),    # mostly tumour
               (square(2400.0, 800.0, hs.window_um), 0.95)]  # stroma, ranked first
    hpfs = place_hpfs([], windows, tissue=tissue, tumor=gate, slide_dimensions_um=(4000.0, 4000.0), cfg=cfg,
                      min_tissue_fraction=hs.min_tissue_fraction, min_tumor_fraction=hs.min_tumor_fraction)
    assert len(hpfs) == 1 and hpfs[0]["center_um"][0] < 1200.0
    assert hpfs[0]["tumor_fraction"] >= hs.min_tumor_fraction
    assert math.isclose(hpfs[0]["tumor_fraction"], gate.tumor_fraction_in_disk(*hpfs[0]["center_um"], cfg.radius_um))


def test_the_centre_with_most_counted_candidates_wins_inside_a_window():
    cfg, hs = hpf_settings()
    tissue = TissueMask(np.ones((300, 300), dtype=bool), 10.0)
    gate = gate_of(np.ones((14, 14), dtype=bool))
    # Figures just inside the disk only if the centre moves right by 32 µm.
    cands = [{"centroid_um": [1000.0 + 32.0 + 262.0 - 5.0, 1000.0 + dy], "counted": True} for dy in (-10.0, 0.0, 10.0)]
    cands += [{"centroid_um": [1000.0 - 262.0 - 30.0, 1000.0], "counted": False}]
    hpfs = place_hpfs(cands, [(square(1000.0, 1000.0, hs.window_um), 1.0)], tissue=tissue, tumor=gate,
                      slide_dimensions_um=(3000.0, 3000.0), cfg=cfg,
                      min_tissue_fraction=hs.min_tissue_fraction, min_tumor_fraction=hs.min_tumor_fraction)
    assert len(hpfs) == 1 and hpfs[0]["count"] == 3 and hpfs[0]["center_um"][0] > 1000.0


@hsettings(max_examples=25, deadline=None)
@given(
    windows=st.lists(st.tuples(st.integers(0, 5), st.integers(0, 5), st.floats(0, 1)), min_size=1, max_size=12,
                     unique_by=lambda w: (w[0], w[1])),
    points=st.lists(st.tuples(st.floats(0, 3600), st.floats(0, 3600), st.booleans()), max_size=30),
    tumor_seed=st.integers(0, 2 ** 16),
)
def test_one_hpf_per_window_with_the_disk_inside_it(windows, points, tumor_seed):
    """Property: every field lies inside one window (disk ⊂ window), no window holds two, fields never overlap,
    and each meets the tissue and tumour minimums."""
    cfg, hs = hpf_settings()
    w = hs.window_um
    tissue = TissueMask(np.ones((360, 360), dtype=bool), 10.0)
    gate = gate_of(np.random.default_rng(tumor_seed).random((17, 17)) < 0.7)
    polys = [(square(w / 2 + i * w, w / 2 + j * w, w), prio) for i, j, prio in windows]  # a non-overlapping tiling
    cands = [{"centroid_um": [x, y], "counted": c} for x, y, c in points]
    hpfs = place_hpfs(cands, polys, tissue=tissue, tumor=gate, slide_dimensions_um=(3600.0, 3600.0), cfg=cfg,
                      min_tissue_fraction=hs.min_tissue_fraction, min_tumor_fraction=hs.min_tumor_fraction)
    assert len(hpfs) <= min(cfg.count, len(polys))
    holders = []
    for h in hpfs:
        cx, cy = h["center_um"]
        inside = [k for k, (poly, _) in enumerate(polys)
                  if poly[0][0] + cfg.radius_um - 1e-6 <= cx <= poly[1][0] - cfg.radius_um + 1e-6
                  and poly[0][1] + cfg.radius_um - 1e-6 <= cy <= poly[2][1] - cfg.radius_um + 1e-6]
        assert len(inside) == 1
        holders.append(inside[0])
        assert h["tumor_fraction"] >= hs.min_tumor_fraction and h["tissue_coverage"] >= hs.min_tissue_fraction
    assert len(holders) == len(set(holders))
    for i, a in enumerate(hpfs):
        for b in hpfs[i + 1:]:
            assert math.dist(a["center_um"], b["center_um"]) >= 2 * cfg.radius_um - 1e-6
