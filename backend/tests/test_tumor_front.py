"""The invasive front and arm H1P (WP-6.6, SPEC-05 §5.2, D22): sites at the tumour periphery rank first."""
import math
import numpy as np
import pytest

from pipeline.hotspots_v6 import score_lattice_windows, select_hotspots
from pipeline.tumor_front import front_distance
from tests.test_hpf_sites import TILE_UM, extent_of, profile_cfg, tissue_of

BAND_UM = 1000.0


def run(p, cfg=None):
    cfg = cfg or profile_cfg()
    windows = score_lattice_windows(p, p >= 0.5, TILE_UM, tissue_of(p), extent_of(p), cfg)
    return select_hotspots(windows, k_max=cfg.k_max, d=cfg.hpf_diameter_um, gap=cfg.gap_um)


def test_the_profile_uses_h1p_with_the_owner_band():
    cfg = profile_cfg()
    assert cfg.ranking_arm == "H1P" and cfg.periphery_band_um == BAND_UM


def test_front_distance_is_the_distance_to_non_tumour_tissue_not_to_glass():
    p = np.full((5, 12), np.nan)
    p[1:4, 0:6] = 0.9      # tumour, its left side on the raster edge (glass)
    p[1:4, 6:9] = 0.05     # stroma on its right
    d = front_distance(p, p >= 0.5, TILE_UM)
    assert d.distance_um[2, 5] == pytest.approx(TILE_UM)         # next to the stroma
    assert d.distance_um[2, 0] == pytest.approx(6 * TILE_UM)     # the glass edge is not front
    assert d.distance_um[0, 3] > 2 * TILE_UM                     # tumour beside glass above it


def test_holes_in_the_section_count_as_non_tumour_tissue():
    p = np.full((9, 9), 0.9)
    p[4, 4] = np.nan       # a lumen: no tissue tile, but inside the section
    d = front_distance(p, p >= 0.5, TILE_UM)
    assert d.has_front and d.distance_um[4, 4] == 0.0 and d.distance_um[4, 6] == pytest.approx(2 * TILE_UM)


def test_a_section_that_is_all_tumour_has_no_front():
    p = np.full((6, 6), 0.9)
    d = front_distance(p, p >= 0.5, TILE_UM)
    assert not d.has_front and np.isinf(d.at_um([100.0, 700.0], [300.0, 900.0])).all()


def test_the_distance_is_bilinear_between_tile_centres():
    p = np.full((3, 6), 0.9)
    p[:, 0] = 0.05
    d = front_distance(p, p >= 0.5, TILE_UM)
    # centres of columns 2 and 3 are 2 and 3 tiles from the stroma column's centre
    x = 3.0 * TILE_UM   # halfway between them
    assert d.at_um([x], [1.5 * TILE_UM])[0] == pytest.approx(2.5 * TILE_UM)


def test_round_tumour_sites_hug_the_edge_not_the_core():
    n, c, radius_tiles = 60, 29.5, 4000.0 / TILE_UM
    yy, xx = np.mgrid[0:n, 0:n]
    dist = np.hypot(xx - c, yy - c)
    p = np.where(dist <= radius_tiles, 0.6 + 0.39 * (1.0 - dist / radius_tiles), 0.05)   # highest in the core
    cfg = profile_cfg()
    h1 = run(p, cfg.model_copy(update={"ranking_arm": "H1"}))
    assert min(math.dist((s.cx, s.cy), (c * TILE_UM + TILE_UM / 2, c * TILE_UM + TILE_UM / 2)) for s in h1) < 1000.0   # H1 alone takes the core
    sites = run(p)
    centre = (c + 0.5) * TILE_UM
    assert len(sites) == 10
    assert all(s.at_periphery and s.front_distance_um <= BAND_UM for s in sites)
    assert all(math.dist((s.cx, s.cy), (centre, centre)) > 4000.0 - BAND_UM - TILE_UM for s in sites)
    assert {s.score_kind for s in sites} == {"periphery_then_tumor"}


def test_a_glass_edge_is_not_the_front():
    p = np.full((40, 50), 0.05)
    p[:, :10] = np.nan            # glass on the left
    p[:, 10:31] = 0.9             # tumour: its left side meets the glass, its right meets stroma
    sites = run(p)
    assert len(sites) == 10 and all(s.at_periphery for s in sites)
    stroma_edge = 31 * TILE_UM
    assert all(s.cx >= stroma_edge - BAND_UM - TILE_UM for s in sites)
    assert all(s.cx > 10 * TILE_UM + BAND_UM + TILE_UM for s in sites)   # none along the glass edge


def test_a_narrow_front_fills_the_rest_with_interior_sites_in_rank_order():
    p = np.full((40, 60), np.nan)
    p[10:16, 5:30] = 0.9          # tumour, glass on three sides
    p[10:16, 30:34] = 0.05        # a short stretch of front on the right
    sites = run(p)
    assert len(sites) == 10
    assert [s.at_periphery for s in sites] == [True] * 6 + [False] * 4
    assert [s.rank for s in sites] == list(range(1, 11))
    assert all(s.front_distance_um > BAND_UM for s in sites[6:])


def test_without_a_front_h1p_equals_h1():
    p = np.full((30, 30), 0.5)
    rng = np.random.default_rng(3)
    p = np.clip(p + 0.4 * rng.random(p.shape), 0, 1)
    cfg = profile_cfg()
    h1p, h1 = run(p), run(p, cfg.model_copy(update={"ranking_arm": "H1"}))
    assert [s.at_periphery for s in h1p] == [False] * len(h1p)
    assert all(s.front_distance_um is None for s in h1p)
    assert [(s.cx, s.cy) for s in h1p] == [(s.cx, s.cy) for s in h1]


def test_the_selection_is_deterministic_and_h1_is_the_wp_6_5_ranking():
    p = np.full((40, 40), 0.05)
    p[5:35, 5:35] = 0.9
    cfg = profile_cfg()
    assert [(s.cx, s.cy) for s in run(p)] == [(s.cx, s.cy) for s in run(p)]
    h1 = run(p, cfg.model_copy(update={"ranking_arm": "H1"}))
    assert {s.score_kind for s in h1} == {"mean_p_tumor"} and all(s.at_periphery is None and s.front_distance_um is None for s in h1)


def test_h1p_needs_its_band():
    p = np.full((10, 10), 0.9)
    with pytest.raises(ValueError, match="periphery_band_um"):
        run(p, profile_cfg().model_copy(update={"periphery_band_um": None}))
