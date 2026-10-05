"""
Stage 4 HPFs (SPEC-06 §5.8, D22): the circles of the confirmed Stage 3 sites, where they are.
The tumour gate and the audit fractions are in test_mitosis_gate.py.
"""
import math

import numpy as np
import pytest
from hypothesis import given, settings, strategies as st

from app.core.pipeline_config import get_pipeline_config
from pipeline.hpf import SiteWithoutCentreError, attach_sites, hpf_seq_of, hpfs_from_sites
from pipeline.scoring import summarize_stage4
from pipeline.tissue_mask import TissueMask
from tests.test_mitosis_gate import gate_of, hpf_settings, site, square

ALL_TUMOR = np.ones((20, 20), dtype=bool)  # 4.48 mm of 224 µm tiles


def place(cands, sites, tissue=None):
    hs = hpf_settings()
    tissue = tissue or TissueMask(np.ones((100, 100), dtype=bool), 40.0)  # 4 x 4 mm of tissue (coarse: fast)
    return hpfs_from_sites(sites, cands, tissue=tissue, tumor=gate_of(ALL_TUMOR), diameter_um=hs.hpf_diameter_um)


def test_an_hpf_is_the_circle_of_its_site_whatever_the_figures_do():
    """The disk neither searches nor shifts towards figures: a cluster just outside the circle stays outside."""
    hs = hpf_settings()
    r = hs.hpf_radius_um
    cands = [{"centroid_um": [1000.0 + r + 5.0, 1000.0 + dy], "counted": True} for dy in (-10.0, 0.0, 10.0)]
    (hpf,) = place(cands, [site("hs_01", 1000.0, 1000.0)])
    assert hpf["center_um"] == [1000.0, 1000.0] and hpf["radius_um"] == r and hpf["count"] == 0


def test_only_counted_candidates_count_with_no_probability_weighting():
    """SPEC-06 §5.8: an equivocal or rejected candidate adds nothing, whatever its probability."""
    cands = [{"centroid_um": [1000.0, 1000.0], "counted": True, "p_a": 0.2},
             {"centroid_um": [1010.0, 1000.0], "counted": False, "p_a": 0.99}]
    (hpf,) = place(cands, [site("hs_01", 1000.0, 1000.0)])
    assert hpf["count"] == 1


def test_seq_follows_the_order_of_the_sites_and_each_names_its_site_and_frame():
    hs = hpf_settings()
    sites = [site("hs_01", 600.0, 600.0), site("hs_02", 1800.0, 600.0), site("hs_u_1", 3000.0, 600.0, "pathologist_added")]
    hpfs = place([], sites)
    assert [h["seq"] for h in hpfs] == [1, 2, 3]
    assert [h["hotspot_id"] for h in hpfs] == ["hs_01", "hs_02", "hs_u_1"]
    assert hpfs[2]["frame_um"] == square(3000.0, 600.0, hs.frame_um) and hpfs[2]["source"] == "pathologist"


def test_a_site_without_a_centre_is_an_error_not_a_guess():
    with pytest.raises(SiteWithoutCentreError, match="run triage again"):
        place([], [{"id": "hs_01", "center_um": None, "polygon_um": square(1000.0, 1000.0, 600.0), "source": "model"}])


def test_hpfs_need_sites():
    with pytest.raises(ValueError, match="sites"):
        place([], [])


def test_fewer_fields_than_the_target_are_flagged_instead_of_relaxing_the_rules():
    hs = hpf_settings()
    hpfs = place([], [site("hs_01", 1000.0, 1000.0)])
    _, summary = summarize_stage4([], hpfs, scoring=get_pipeline_config().mitosis.scoring, hpf_count=hs.k_max)
    assert summary["n_hpf"] == 1 and summary["flags"] == ["hpf_count_lt_10"] and summary["hpf_target"] == hs.k_max


def test_a_candidate_in_a_frames_padding_has_no_hpf_and_is_not_counted():
    """A figure in the padding is imaged (it is in the frame) but belongs to no circle."""
    hs = hpf_settings()
    r = hs.hpf_radius_um
    inside = {"id": "a", "centroid_um": [1000.0 + r - 5.0, 1000.0], "counted": True, "final_decision": "mitosis", "review_label": None}
    padding = {"id": "b", "centroid_um": [1000.0 + r + 20.0, 1000.0], "counted": True, "final_decision": "mitosis", "review_label": None}
    hpfs = place([inside, padding], [site("hs_01", 1000.0, 1000.0)])
    assert hpf_seq_of(inside["centroid_um"], hpfs) == 1 and hpf_seq_of(padding["centroid_um"], hpfs) is None
    _, summary = summarize_stage4([inside, padding], hpfs, scoring=get_pipeline_config().mitosis.scoring, hpf_count=hs.k_max)
    assert summary["count_total"] == 1


def test_a_candidate_is_in_one_circle_only():
    hs = hpf_settings()
    d = hs.hpf_diameter_um
    hpfs = place([], [site("hs_01", 1000.0, 1000.0), site("hs_02", 1000.0 + d, 1000.0)])  # touching, frames overlap
    point_in_both_frames = [1000.0 + d / 2, 1000.0]  # on the circles' touching point
    assert hpf_seq_of(point_in_both_frames, hpfs) == 1  # the first circle holds it; the second only touches
    assert hpf_seq_of([1000.0 + d / 2 + 1.0, 1000.0], hpfs) == 2


def test_attach_sites_matches_hpfs_to_the_site_at_their_centre():
    hs = hpf_settings()
    hpfs = place([], [site("hs_01", 600.0, 600.0)])
    (attached,) = attach_sites(hpfs, [site("hs_01", 600.0, 600.0)])
    assert attached["hotspot_id"] == "hs_01" and attached["frame_um"] == square(600.0, 600.0, hs.frame_um)
    with pytest.raises(LookupError, match="no confirmed hotspot"):
        attach_sites(hpfs, [site("hs_02", 2000.0, 2000.0)])


@settings(max_examples=25, deadline=None)
@given(points=st.lists(st.tuples(st.floats(0, 4000), st.floats(0, 4000), st.booleans()), max_size=40))
def test_count_total_is_the_counted_candidates_inside_a_circle(points):
    """AC10 (property): the total over HPFs is exactly the counted candidates inside some circle."""
    hs = hpf_settings()
    d = hs.hpf_diameter_um
    sites = [site(f"hs_{i:02d}", 600.0 + (i % 4) * (d + 300.0), 600.0 + (i // 4) * (d + 300.0)) for i in range(10)]
    cands = [{"id": f"c{k}", "centroid_um": [x, y], "counted": c, "final_decision": "mitosis", "review_label": None}
             for k, (x, y, c) in enumerate(points)]
    hpfs = place(cands, sites)
    _, summary = summarize_stage4(cands, hpfs, scoring=get_pipeline_config().mitosis.scoring, hpf_count=hs.k_max)
    expected = sum(1 for c in cands if c["counted"] and any(math.dist(c["centroid_um"], h["center_um"]) <= h["radius_um"] for h in hpfs))
    assert summary["count_total"] == expected == sum(h["count"] for h in hpfs)
