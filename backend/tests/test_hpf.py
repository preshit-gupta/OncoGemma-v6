"""
HPF placement (SPEC-06 §5.8; AC10: HPFs never overlap): one field per hotspot window, the disk inside it
(owner decision 2026-10-04). The tumour constraints and the per-window property test are in test_mitosis_gate.py.
"""
import math

import numpy as np
import pytest
from hypothesis import given, settings, strategies as st

from app.core.pipeline_config import get_pipeline_config
from pipeline.hpf import place_hpfs, window_centres
from pipeline.scoring import compute_nottingham_mitotic_score, summarize_stage4
from pipeline.tissue_mask import TissueMask
from tests.test_mitosis_gate import gate_of, square

ALL_TUMOR = np.ones((20, 20), dtype=bool)  # 4.48 mm of 224 µm tiles


def cfgs():
    config = get_pipeline_config()
    return config.mitosis.hpf, config.specimen_profiles.profiles["resection"].hotspots


def place(cands, windows, tissue=None, slide=(4000.0, 4000.0)):
    cfg, hs = cfgs()
    tissue = tissue or TissueMask(np.ones((100, 100), dtype=bool), 40.0)  # 4 x 4 mm of tissue (coarse: fast)
    return place_hpfs(cands, windows, tissue=tissue, tumor=gate_of(ALL_TUMOR), slide_dimensions_um=slide, cfg=cfg,
                      min_tissue_fraction=hs.min_tissue_fraction, min_tumor_fraction=hs.min_tumor_fraction)


def test_only_counted_candidates_count_with_no_probability_weighting():
    """SPEC-06 §5.8: an equivocal or rejected candidate adds nothing, whatever its probability."""
    _, hs = cfgs()
    cands = [{"centroid_um": [1000.0, 1000.0], "counted": True, "p_a": 0.2},
             {"centroid_um": [1010.0, 1000.0], "counted": False, "p_a": 0.99}]
    hpfs = place(cands, [(square(1000.0, 1000.0, hs.window_um), 1.0)])
    assert len(hpfs) == 1 and hpfs[0]["count"] == 1


def test_windows_are_taken_by_priority_and_fields_stop_at_count():
    cfg, hs = cfgs()
    w = hs.window_um
    windows = [(square(w / 2 + i * w, w / 2 + j * w, w), float(i + 6 * j)) for i in range(6) for j in range(6)]
    hpfs = place([], windows)
    assert len(hpfs) == cfg.count
    expected = sorted(windows, key=lambda x: x[1], reverse=True)[:cfg.count]
    assert [h["center_um"] for h in hpfs] == [[float(np.mean([p[0] for p in poly])), float(np.mean([p[1] for p in poly]))]
                                              for poly, _ in expected]


def test_fewer_fields_than_count_are_flagged_instead_of_relaxing_the_rules():
    cfg, hs = cfgs()
    hpfs = place([], [(square(1000.0, 1000.0, hs.window_um), 1.0)])
    assert len(hpfs) == 1
    _, summary = summarize_stage4([], hpfs, scoring=get_pipeline_config().mitosis.scoring, hpf_count=cfg.count)
    assert summary["n_hpf"] == 1 and summary["flags"] == ["hpf_count_lt_10"]


def test_a_window_on_glass_or_over_the_slide_edge_gets_no_field():
    _, hs = cfgs()
    cells = np.ones((400, 400), dtype=bool)
    cells[:, 200:] = False  # glass right of 2 mm
    tissue = TissueMask(cells, 10.0)
    windows = [(square(3000.0, 1000.0, hs.window_um), 1.0),   # glass
               (square(200.0, 1000.0, hs.window_um), 0.9),    # disk would cross x = 0
               (square(1000.0, 1000.0, hs.window_um), 0.5)]
    hpfs = place([], windows, tissue=tissue)
    assert [h["center_um"] for h in hpfs] == [[1000.0, 1000.0]] and math.isclose(hpfs[0]["tissue_coverage"], 1.0, abs_tol=1e-3)


def test_place_hpfs_needs_hotspots():
    with pytest.raises(ValueError, match="hotspots"):
        place([], [])


def test_window_centres_need_a_polygon():
    with pytest.raises(ValueError, match="3 vertices"):
        window_centres([[0.0, 0.0], [1.0, 1.0]], 262.0, 16.0)


@settings(max_examples=25, deadline=None)
@given(windows=st.lists(st.tuples(st.floats(300, 3700), st.floats(300, 3700), st.floats(530, 800), st.floats(0, 1)),
                        min_size=1, max_size=12),
       points=st.lists(st.tuples(st.floats(0, 4000), st.floats(0, 4000), st.booleans()), max_size=30))
def test_placed_hpfs_never_overlap(windows, points):
    """AC10 (property): whatever the windows (overlapping or not) and candidates, fields are >= 2r apart and inside the slide."""
    cfg, _ = cfgs()
    hpfs = place([{"centroid_um": [x, y], "counted": c} for x, y, c in points],
                 [(square(x, y, side), prio) for x, y, side, prio in windows])
    assert len(hpfs) <= cfg.count
    for i, a in enumerate(hpfs):
        cx, cy = a["center_um"]
        assert cfg.radius_um - 1e-6 <= cx <= 4000.0 - cfg.radius_um + 1e-6 and cfg.radius_um - 1e-6 <= cy <= 4000.0 - cfg.radius_um + 1e-6
        for b in hpfs[i + 1:]:
            assert math.dist(a["center_um"], b["center_um"]) >= 2 * cfg.radius_um - 1e-3


def test_area_normalised_score_uses_the_placed_fields():
    scoring = get_pipeline_config().mitosis.scoring
    res = compute_nottingham_mitotic_score(count_total=5, n_hpf=3, radius_um=262.0, scoring=scoring)
    assert res["n_hpf"] == 3 and res["area_mm2"] == round(3 * math.pi * 0.262 ** 2, 3)
