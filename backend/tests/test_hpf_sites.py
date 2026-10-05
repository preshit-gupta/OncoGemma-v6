"""HPF sites (WP-6.5, SPEC-05 §5, SPEC-06 §5.8; D22): circles of 0.5 mm in padded 0.6 mm frames.

Selection works on the circle (not a square), circles never overlap while frames may, the pathologist's
pins accumulate and are used as they are, and fewer sites than the target is inadequate tissue that
the pathologist acknowledges when confirming Stage 3.
"""
import math

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import select

from app.core.pipeline_config import get_pipeline_config
from app.models.audit import AuditEvent
from app.models.hotspot import Hotspot
from app.models.stage_execution import StageExecution
from pipeline.hotspots_v6 import score_lattice_windows, select_hotspots
from pipeline.scoring import summarize_stage4
from pipeline.tissue_mask import TissueMask
from tests.test_mitosis_gate import gate_of
from tests.test_triage_api_v6 import D, FRAME, TARGET, client_and_db, machine_output, post_edits, seed_case, serving  # noqa: F401

TILE_UM = 224.0


def profile_cfg():
    return get_pipeline_config().specimen_profiles.profiles["resection"].hotspots


def tissue_of(p_raster):
    """A tissue mask over the whole tile raster where the probability is not NaN (4 px per tile edge)."""
    return TissueMask(np.kron(~np.isnan(p_raster), np.ones((4, 4), dtype=bool)), TILE_UM / 4.0)


def extent_of(p_raster):
    return p_raster.shape[1] * TILE_UM, p_raster.shape[0] * TILE_UM


# -- selection on the circle ---------------------------------------------------------------------------------------------------------

def test_a_narrow_strip_yields_ten_sites():
    """A tumour strip 3 tiles (672 µm) wide and 25 tiles (5,600 µm) long, all tissue, holds ten 0.5 mm circles.

    The old 600 µm-square rule fitted at most nine: a square can extend at most 30% past the strip, so ten
    squares would need 6,000 µm of a 5,960 µm span.
    """
    cfg = profile_cfg()
    p = np.full((35, 13), 0.05)
    p[3:28, 5:8] = 0.9
    sites = score_lattice_windows(p, p >= 0.5, TILE_UM, tissue_of(p), extent_of(p), cfg)
    selected = select_hotspots(sites, k_max=cfg.k_max, d=cfg.hpf_diameter_um, gap=cfg.gap_um)
    assert len(selected) == cfg.k_max == 10
    assert all(s.tumor_fraction >= cfg.min_tumor_fraction and s.tissue_fraction >= cfg.min_tissue_fraction for s in selected)
    assert 10 * (cfg.frame_um) > 25 * TILE_UM + 2 * 0.3 * cfg.frame_um  # the old rule's span argument (6,000 > 5,960)


def random_slide(seed, n_rows, n_cols, tissue_share, tumour_share):
    rng = np.random.default_rng(seed)
    p = np.where(rng.random((n_rows, n_cols)) < tissue_share, rng.random((n_rows, n_cols)), np.nan)
    tumor = np.nan_to_num(p, nan=-1.0) >= (1.0 - tumour_share)
    return p, tumor


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed=st.integers(0, 2 ** 16), tissue_share=st.floats(0.5, 1.0), tumour_share=st.floats(0.3, 0.9),
       n_rows=st.integers(8, 16), n_cols=st.integers(8, 16))
def test_selected_circles_are_disjoint_frames_may_overlap_and_every_site_meets_both_fractions(
    seed, tissue_share, tumour_share, n_rows, n_cols
):
    cfg = profile_cfg()
    p, tumor = random_slide(seed, n_rows, n_cols, tissue_share, tumour_share)
    tissue = tissue_of(p)
    gate = gate_of(tumor, dilation_tiles=0)
    selected = select_hotspots(
        score_lattice_windows(p, tumor, TILE_UM, tissue, extent_of(p), cfg), k_max=cfg.k_max, d=cfg.hpf_diameter_um, gap=cfg.gap_um
    )
    assert len(selected) <= cfg.k_max
    for i, a in enumerate(selected):
        for b in selected[i + 1:]:
            assert math.dist((a.cx, a.cy), (b.cx, b.cy)) >= cfg.hpf_diameter_um - 1e-6  # circles never overlap
    for s in selected[:3]:
        # Agreement with the per-disk functions (the vectorised fractions are the same measurement).
        assert abs(s.tissue_fraction - tissue.fraction_in_disk_um(s.cx, s.cy, cfg.hpf_radius_um)) < 1e-3
        assert abs(s.tumor_fraction - gate.tumor_fraction_in_disk(s.cx, s.cy, cfg.hpf_radius_um)) < 1e-3
    for s in selected:
        assert s.tissue_fraction >= cfg.min_tissue_fraction and s.tumor_fraction >= cfg.min_tumor_fraction


def test_frames_of_adjacent_sites_may_overlap_by_the_padding():
    cfg = profile_cfg()
    p = np.full((20, 20), 0.9)
    selected = select_hotspots(
        score_lattice_windows(p, p >= 0.5, TILE_UM, tissue_of(p), extent_of(p), cfg), k_max=cfg.k_max, d=cfg.hpf_diameter_um, gap=0.0
    )
    touching = [(a, b) for a in selected for b in selected if a.id < b.id
                and math.isclose(math.dist((a.cx, a.cy), (b.cx, b.cy)), cfg.hpf_diameter_um, abs_tol=1e-6)]
    assert touching, "ten circles on an all-tumour slide include touching neighbours"
    a, b = touching[0]
    assert a.window_um == b.window_um == cfg.frame_um == 600.0
    # Touching circles: their frames overlap by the padding on each side (100 µm).
    x_overlap = a.window_um - abs(a.cx - b.cx)
    y_overlap = a.window_um - abs(a.cy - b.cy)
    assert x_overlap > 0 and y_overlap > 0


# -- pinning -------------------------------------------------------------------------------------------------------------------------

def spaced(n, y=600.0):
    """n model sites 600 µm apart along a row (their circles are disjoint, their frames touch)."""
    return [(600.0 + 600.0 * i, y) for i in range(n)]


def stored_edits(db, case_id):
    return db.scalars(select(StageExecution).where(StageExecution.case_id == case_id)).one().review_edits


def test_pinning_two_sites_in_two_requests_keeps_both(client_and_db):
    client, db = client_and_db
    case_id, output = seed_case(db, spaced(1))
    with serving(output):
        first = post_edits(client, case_id, {"op": "add", "center_um": [3000.0, 3000.0]})
        second = post_edits(client, case_id, {"op": "add", "center_um": [5000.0, 3000.0]})
    assert first.status_code == second.status_code == 200
    ids = [h["id"] for h in second.json()["hotspots"]]
    assert ids == ["hs_01", "hs_u_1", "hs_u_2"]
    assert [e["op"] for e in stored_edits(db, case_id)] == ["add", "add"]
    assert [e["id"] for e in second.json()["review_edits"]] == ["hs_u_1", "hs_u_2"]


def test_restore_clears_exclusion_and_move_recentres_a_site(client_and_db):
    client, db = client_and_db
    case_id, output = seed_case(db, spaced(2))
    with serving(output):
        post_edits(client, case_id, {"op": "exclude", "id": "hs_01", "reason": "fat"})
        excluded = post_edits(client, case_id, {"op": "move", "id": "hs_02", "center_um": [4000.0, 4000.0]}).json()
        assert next(h for h in excluded["hotspots"] if h["id"] == "hs_01")["excluded"] is True
        moved = next(h for h in excluded["hotspots"] if h["id"] == "hs_02")
        assert moved["source"] == "pathologist_modified" and moved["center_um"] == [4000.0, 4000.0]
        restored = post_edits(client, case_id, {"op": "restore", "id": "hs_01"}).json()
    site = next(h for h in restored["hotspots"] if h["id"] == "hs_01")
    assert site["excluded"] is False and site["exclude_reason"] is None


def test_a_pin_overlapping_a_circle_is_refused_but_one_overlapping_a_frame_is_not(client_and_db):
    client, db = client_and_db
    case_id, output = seed_case(db, [(3000.0, 3000.0)])
    with serving(output):
        circle = post_edits(client, case_id, {"op": "add", "center_um": [3000.0 + D - 1.0, 3000.0]})
        frame_only = post_edits(client, case_id, {"op": "add", "center_um": [3000.0 + D + 1.0, 3000.0]})
    assert circle.status_code == 422 and circle.json()["error"] == "hotspot_overlap"
    assert frame_only.status_code == 200


def test_an_eleventh_active_site_is_too_many(client_and_db):
    client, db = client_and_db
    case_id, output = seed_case(db, spaced(TARGET))
    with serving(output):
        res = post_edits(client, case_id, {"op": "add", "center_um": [3000.0, 3000.0]})
        assert res.status_code == 422 and res.json() == {
            "error": "too_many_sites", "hpf_target": TARGET, "detail": res.json()["detail"],
        }
        # Excluding one makes room for the pin.
        assert post_edits(client, case_id, {"op": "exclude", "id": "hs_01", "reason": "fat"}).status_code == 200
        assert post_edits(client, case_id, {"op": "add", "center_um": [3000.0, 3000.0]}).status_code == 200


def test_a_pin_whose_circle_leaves_the_slide_is_out_of_bounds(client_and_db):
    client, db = client_and_db
    case_id, output = seed_case(db, spaced(1))
    with serving(output):
        res = post_edits(client, case_id, {"op": "add", "center_um": [100.0, 100.0]})
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_site" and res.json()["id"] == "hs_u_1" and res.json()["reason"] == "out_of_bounds"


def test_an_unknown_site_is_refused_not_ignored(client_and_db):
    client, db = client_and_db
    case_id, output = seed_case(db, spaced(1))
    with serving(output):
        res = post_edits(client, case_id, {"op": "delete", "id": "hs_99"})
    assert res.status_code == 422 and res.json()["reason"] == "unknown_site"


# -- the confirm gate ----------------------------------------------------------------------------------------------------------------

def confirm(client, case_id, **body):
    return client.post("/api/v1/stages/triage/confirm", json={"case_id": case_id, "no_invasive_tumor": False, **body})


def test_nine_active_sites_need_the_acceptance_to_confirm(client_and_db):
    client, db = client_and_db
    case_id, output = seed_case(db, spaced(9))
    with serving(output):
        refused = confirm(client, case_id)
        assert refused.status_code == 409
        assert refused.json()["error"] == "hpf_sites_lt_10" and refused.json()["n_active"] == 9 and refused.json()["hpf_target"] == TARGET
        assert db.scalars(select(Hotspot)).first() is None  # nothing was confirmed
        accepted = confirm(client, case_id, accept_fewer_hpfs=True)
    assert accepted.status_code == 200 and accepted.json()["next_stage_queued"] == "mitosis"
    audit = [e for e in db.scalars(select(AuditEvent).where(AuditEvent.event_type == "stage_confirmed")).all() if e.stage == "triage"]
    assert audit[0].payload["accept_fewer_hpfs"] is True and audit[0].payload["n_active"] == 9
    mitosis = db.scalars(select(StageExecution).where(StageExecution.stage == "mitosis")).one()
    assert mitosis.input_ref["accept_fewer_hpfs"] is True and mitosis.input_ref["hpf_target"] == TARGET


def test_ten_active_sites_confirm_without_an_acceptance(client_and_db):
    client, db = client_and_db
    case_id, output = seed_case(db, spaced(TARGET))
    with serving(output):
        res = confirm(client, case_id)
    assert res.status_code == 200 and res.json()["accept_fewer_hpfs"] is False


def test_stage_4_reports_nine_hpfs_and_the_inadequate_flag():
    from pipeline.hpf import hpfs_from_sites
    from tests.test_mitosis_gate import site

    cfg = profile_cfg()
    sites = [site(f"hs_{i:02d}", 600.0 + 600.0 * i, 600.0) for i in range(9)]
    hpfs = hpfs_from_sites(sites, [], tissue=TissueMask(np.ones((100, 100), dtype=bool), 100.0),
                           tumor=gate_of(np.ones((50, 50), dtype=bool)), diameter_um=cfg.hpf_diameter_um)
    _, summary = summarize_stage4([], hpfs, scoring=get_pipeline_config().mitosis.scoring, hpf_count=cfg.k_max)
    assert summary["n_hpf"] == 9 and summary["flags"] == ["hpf_count_lt_10"] and summary["hpf_target"] == 10


# -- Stage 4 uses the circles as confirmed ------------------------------------------------------------------------------------------------

def test_hpf_centres_equal_the_confirmed_sites_whatever_the_figures_and_a_pin_is_always_an_hpf():
    from pipeline.hpf import hpfs_from_sites
    from tests.test_mitosis_gate import site

    cfg = profile_cfg()
    sites = [site("hs_01", 1000.0, 1000.0), site("hs_u_1", 2000.0, 2000.0, "pathologist_added")]  # the pin sits over stroma
    rng = np.random.default_rng(0)
    figures = [{"centroid_um": [float(x), float(y)], "counted": True} for x, y in rng.uniform(500.0, 2500.0, (60, 2))]
    gate = gate_of(np.eye(20, dtype=bool))
    tissue = TissueMask(np.ones((100, 100), dtype=bool), 50.0)
    hpfs = hpfs_from_sites(sites, figures, tissue=tissue, tumor=gate, diameter_um=cfg.hpf_diameter_um)
    assert [h["center_um"] for h in hpfs] == [[1000.0, 1000.0], [2000.0, 2000.0]]
    assert [h["hotspot_id"] for h in hpfs] == ["hs_01", "hs_u_1"]
