"""
HPF placement and the density map (SPEC-06 §5.8; AC10: HPFs never overlap).
"""
import math
import numpy as np
import pytest
from hypothesis import given, settings, strategies as st

from app.core.pipeline_config import get_pipeline_config
from pipeline.tissue_mask import TissueMask
from pipeline.hpf import (
    create_circular_disk_mask,
    generate_mitosis_density_map,
    greedy_place_hpfs,
    is_point_in_polygon,
    place_hpfs,
)


def test_circular_disk_mask():
    radius_cells = 3.0
    mask = create_circular_disk_mask(radius_cells)
    # Dimensions should be 2*ceil(r) + 1 = 7x7
    assert mask.shape == (7, 7)
    # Center cell must be 1.0
    assert mask[3, 3] == 1.0
    # Far corner must be 0.0
    assert mask[0, 0] == 0.0
    # Boundary points
    assert mask[3, 0] == 1.0 # distance = 3
    assert mask[0, 3] == 1.0


def test_generate_mitosis_density_map():
    # Synthetic candidates clustered near (1000, 1000)
    candidates = [
        {"id": "m_01", "centroid_um": [1000.0, 1000.0], "counted": True},
        {"id": "m_02", "centroid_um": [1050.0, 1020.0], "counted": True},
        {"id": "m_03", "centroid_um": [980.0, 1010.0], "counted": True},
        {"id": "m_04", "centroid_um": [2000.0, 2000.0], "counted": False}, # should be ignored
    ]
    bbox_um = (800.0, 800.0, 1200.0, 1200.0)
    density_map, grid_meta = generate_mitosis_density_map(
        candidates,
        bounding_box_um=bbox_um,
        grid_res_um=16.0,
        radius_um=262.0
    )

    assert density_map.ndim == 2
    assert grid_meta["stride_um"] == 16.0
    assert np.max(density_map) >= 3.0 # At least 3 mitoses convolved in cluster


def test_greedy_place_hpfs_non_overlap_invariant():
    # Create synthetic density map with several high peaks
    ny, nx = 100, 100
    stride = 16.0
    density_map = np.zeros((ny, nx), dtype=np.float32)
    # Add multiple peaks separated by >= 524 um
    density_map[20, 20] = 10.0
    density_map[20, 60] = 8.0
    density_map[60, 20] = 7.0
    density_map[60, 60] = 9.0
    density_map[95, 20] = 6.0

    grid_meta = {
        "origin_um": [0.0, 0.0],
        "stride_um": stride,
        "nx": nx,
        "ny": ny
    }

    radius_um = 262.0
    min_sep_um = 524.0 # 2 * radius

    hpfs = greedy_place_hpfs(
        density_map,
        grid_meta,
        count=5,
        radius_um=radius_um,
        min_separation_um=min_sep_um
    )

    assert len(hpfs) == 5
    # Verify non-overlap distance invariant between all placed pairs
    for i in range(len(hpfs)):
        for j in range(i + 1, len(hpfs)):
            c1 = hpfs[i]["center_um"]
            c2 = hpfs[j]["center_um"]
            dist = math.hypot(c1[0] - c2[0], c1[1] - c2[1])
            assert dist >= min_sep_um - 1e-2, f"HPF {i} and {j} overlap: dist={dist} < {min_sep_um}"


def test_density_comes_from_counted_candidates_only_with_no_probability_weighting():
    """SPEC-06 §5.8: an equivocal or unlabelled candidate adds nothing, whatever its probability."""
    counted = [{"id": "a", "centroid_um": [1000.0, 1000.0], "counted": True, "p_a": 0.2}]
    uncounted = [{"id": "b", "centroid_um": [1000.0, 1000.0], "counted": False, "p_a": 0.99}]
    bbox = (800.0, 800.0, 1200.0, 1200.0)
    with_b, _ = generate_mitosis_density_map(counted + uncounted, bounding_box_um=bbox, grid_res_um=16.0, radius_um=262.0)
    without_b, _ = generate_mitosis_density_map(counted, bounding_box_um=bbox, grid_res_um=16.0, radius_um=262.0)
    assert np.array_equal(with_b, without_b)
    assert math.isclose(float(np.max(with_b)), 1.0, rel_tol=1e-4)


def test_fewer_fields_are_placed_instead_of_overlapping_ones():
    # Small area where 10 strictly non-overlapping circles cannot fit
    ny, nx = 40, 40
    stride = 16.0
    density_map = np.ones((ny, nx), dtype=np.float32) * 5.0
    grid_meta = {
        "origin_um": [0.0, 0.0],
        "stride_um": stride,
        "nx": nx,
        "ny": ny
    }

    hpfs = greedy_place_hpfs(
        density_map,
        grid_meta,
        count=10,
        radius_um=262.0,
        min_separation_um=524.0,
    )

    # Issue #718: Must return ONLY fields that actually fit (no spiral padding to 10), and no relaxed pass
    assert 1 <= len(hpfs) < 10
    for i in range(len(hpfs)):
        for j in range(i + 1, len(hpfs)):
            c1 = hpfs[i]["center_um"]
            c2 = hpfs[j]["center_um"]
            dist = math.hypot(c1[0] - c2[0], c1[1] - c2[1])
            assert dist >= 524.0 - 1e-2

    # Verify area-normalized scoring uses actual counted area per PRD 04 §4.2
    from pipeline.scoring import compute_nottingham_mitotic_score
    score_res = compute_nottingham_mitotic_score(
        count_total=5, n_hpf=len(hpfs), radius_um=262.0, scoring=get_pipeline_config().mitosis.scoring
    )
    single_hpf_area = math.pi * (0.262 ** 2)
    expected_area = round(len(hpfs) * single_hpf_area, 3)
    assert score_res["n_hpf"] == len(hpfs)
    assert score_res["area_mm2"] == expected_area


def test_greedy_place_hpfs_rejects_empty_glass():
    # Grid where left half (x < 1000 um) is dense tissue and right half (x >= 1000 um) is empty glass
    ny, nx = 100, 100
    stride = 16.0
    slide_w_um = nx * stride # 1600 um
    slide_h_um = ny * stride # 1600 um

    # Tissue mask (one pixel per grid cell): the left half is tissue, the right half empty glass
    tissue_cells = np.zeros((100, 100), dtype=bool)
    tissue_cells[:, :50] = True
    tissue = TissueMask(tissue_cells, stride)

    # Put high density candidates on the right half (glass) and lower on the left half (tissue)
    density_map = np.zeros((ny, nx), dtype=np.float32)
    density_map[30, 20] = 5.0  # Tissue
    density_map[70, 20] = 5.0  # Tissue
    density_map[30, 80] = 100.0 # Glass (should be strictly rejected!)
    density_map[70, 80] = 100.0 # Glass (should be strictly rejected!)

    grid_meta = {
        "origin_um": [0.0, 0.0],
        "stride_um": stride,
        "nx": nx,
        "ny": ny
    }

    hpfs = greedy_place_hpfs(
        density_map,
        grid_meta,
        count=2,
        radius_um=262.0,
        min_separation_um=524.0,
        tissue=tissue,
        slide_dimensions_um=(slide_w_um, slide_h_um),
        min_tissue_coverage=0.70
    )

    assert len(hpfs) == 2
    for h in hpfs:
        cx, cy = h["center_um"]
        # Must be on left half (tissue)
        assert cx < 800.0, f"HPF placed in glass area: center_x = {cx}"
        assert h["tissue_coverage"] >= 0.70, f"Insufficient tissue coverage: {h['tissue_coverage']}"


def test_greedy_place_hpfs_prioritizes_dense_hotspots():
    ny, nx = 100, 100
    stride = 16.0
    slide_w_um = nx * stride
    slide_h_um = ny * stride

    tissue = TissueMask(np.ones((100, 100), dtype=bool), stride)
    density_map = np.ones((ny, nx), dtype=np.float32)

    grid_meta = {
        "origin_um": [0.0, 0.0],
        "stride_um": stride,
        "nx": nx,
        "ny": ny
    }

    # Hotspot A (lower priority 0.50): centered at (300, 300)
    hs_a = [[100.0, 100.0], [500.0, 100.0], [500.0, 500.0], [100.0, 500.0]]
    # Hotspot B (higher priority 0.95): centered at (1200, 1200)
    hs_b = [[1000.0, 1000.0], [1400.0, 1000.0], [1400.0, 1400.0], [1000.0, 1400.0]]

    hpfs = greedy_place_hpfs(
        density_map,
        grid_meta,
        hotspot_polygons_um=[hs_a, hs_b],
        hotspot_priorities=[0.50, 0.95],
        count=2,
        radius_um=262.0,
        min_separation_um=524.0,
        tissue=tissue,
        slide_dimensions_um=(slide_w_um, slide_h_um)
    )

    assert len(hpfs) == 2
    # HPF 1 must be from the higher priority hotspot (Hotspot B, cx >= 1000)
    assert hpfs[0]["center_um"][0] >= 1000.0, f"Expected HPF 1 in Hotspot B (>= 1000 um), got {hpfs[0]['center_um']}"



def square(x0, y0, side):
    return [[x0, y0], [x0 + side, y0], [x0 + side, y0 + side], [x0, y0 + side]]


@settings(max_examples=60, deadline=None)
@given(
    points=st.lists(st.tuples(st.floats(0, 3200), st.floats(0, 3200), st.booleans()), max_size=40),
    hotspots=st.lists(st.tuples(st.floats(0, 2600), st.floats(0, 2600), st.floats(150, 1400), st.floats(0, 1)),
                      min_size=1, max_size=5),
    glass_cols=st.integers(0, 150),
)
def test_placed_hpfs_never_overlap(points, hotspots, glass_cols):
    """AC10 (property): whatever the candidates, hotspots and tissue, placed HPFs are >= 2r apart, inside the
    slide, at most `count`, each at least min_tissue_coverage tissue."""
    cfg = get_pipeline_config().mitosis.hpf
    stride = 16.0
    cells = np.ones((250, 250), dtype=bool)
    cells[:, :glass_cols] = False
    tissue = TissueMask(cells, stride)
    candidates = [{"id": f"m{i}", "centroid_um": [x, y], "counted": c} for i, (x, y, c) in enumerate(points)]
    hpfs = place_hpfs(
        candidates,
        [(square(x, y, side), prio) for x, y, side, prio in hotspots],
        tissue=tissue,
        slide_dimensions_um=(4000.0, 4000.0),
        cfg=cfg,
    )
    assert len(hpfs) <= cfg.count
    for i, a in enumerate(hpfs):
        cx, cy = a["center_um"]
        assert cfg.radius_um <= cx <= 4000.0 - cfg.radius_um and cfg.radius_um <= cy <= 4000.0 - cfg.radius_um
        assert a["tissue_coverage"] >= cfg.min_tissue_coverage
        for b in hpfs[i + 1:]:
            assert math.dist(a["center_um"], b["center_um"]) >= 2 * cfg.radius_um - 1e-3


def test_place_hpfs_needs_hotspots():
    with pytest.raises(ValueError, match="hotspots"):
        place_hpfs([], [], tissue=TissueMask(np.ones((10, 10), dtype=bool), 16.0), slide_dimensions_um=(160.0, 160.0),
                   cfg=get_pipeline_config().mitosis.hpf)
