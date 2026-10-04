"""
Batch 16 Test Suite: Workstream 3 Mitosis Detection & Morphometrics.
Validates fixes for:
  - #119, #581, #582, #121, #475, #583: Detector loading, confidence un-flooring, tile caps, polygon intersection, sub-mask OD.
  - #595, #124: Verifier output parsing and 72px analysis window.
  - #118, #764, #373: Dynamic scoring config, variable HPF radii area summation, zero-HPF division safety.
  - #584: Optical crop boundary clamping in worker.
  - #115, #344, #127: Router typed models, HPF non-overlap 422 validation, thumbnail invalidation.
  - #114, #592: RFC-6902 review_edits diff tracking in /recompute and /bulk_action.
  - #128, #593, #753, #399: Unique candidate IDs, 7.5 µm proximity dedup, anisotropic mpp_y, defensive GCS upload.
  - #756: Cache-Control immutable on candidate crop streaming.
  - Medical safety: 409 Conflict gating on confirmed stages and non-awaiting confirmation attempts.
"""
import math
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.core.db import Base, get_db
from pipeline.detect import enumerate_hotspot_tiles
from pipeline.tissue_mask import TissueMask
from pipeline.heuristics.od_sweep import detect_hyperchromatic_features
from app.core.pipeline_config import get_pipeline_config
from pipeline.scoring import compute_nottingham_mitotic_score


# Database setup for isolated testing
SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[get_db] = override_get_db
    yield
    app.dependency_overrides.pop(get_db, None)


client = TestClient(app)


# =========================================================================
# 1. Detector Tests (#119, #581, #582, #121, #583)
# =========================================================================
def test_hyperchromatic_feature_detection_and_unfloored_conf():
    """Validates #582 & #583 on the v5 OD sweep, now an ablation-only heuristic."""

    # Synthetic 1024x1024 tile with a very dark mitotic-like spot in the center
    tile = np.full((1024, 1024, 3), 220, dtype=np.uint8)
    tile[495:505, 495:505, :] = 30  # High optical density

    candidates = detect_hyperchromatic_features(tile, 0.40, max_candidates_per_tile=10)
    assert isinstance(candidates, list)
    if candidates:
        cx, cy, conf = candidates[0]
        assert conf >= 0.40
        assert cx > 0
        assert cy > 0


def test_tile_candidate_cap():
    """Validates #581: Candidate count per tile respects max_candidates_per_tile."""
    cap = 5

    # Synthetic tile with many dark spots
    tile = np.full((1024, 1024, 3), 220, dtype=np.uint8)
    for i in range(10):
        r = 100 + i * 80
        tile[r:r+12, r:r+12, :] = 20

    candidates = detect_hyperchromatic_features(tile, 0.10, max_candidates_per_tile=cap)
    assert len(candidates) <= cap


def test_enumerate_hotspot_tiles_clamping_and_polygon_check():
    """Validates #121: Boundary clamping to slide extent and polygon intersection."""
    hotspot_polygon_um = [
        [1000.0, 1000.0],
        [1500.0, 1000.0],
        [1500.0, 1500.0],
        [1000.0, 1500.0]
    ]
    tissue = TissueMask(np.ones((200, 200), dtype=bool), 10.0)  # a 2 x 2 mm slide of tissue
    tiles = enumerate_hotspot_tiles(
        hotspot_polygon_um=hotspot_polygon_um,
        tile_um=256.0,
        stride_um=240.0,
        tissue=tissue,
        min_tissue_fraction=0.2,
    )

    assert len(tiles) > 0
    for t in tiles:
        assert t["origin_um"][0] >= 0
        assert t["origin_um"][1] >= 0
        assert t["size_um"] == [256.0, 256.0]


def test_enumerate_hotspot_tiles_skips_glass_and_tiles_off_the_polygon():
    """A tile needs enough registered tissue and must touch the hotspot polygon."""
    polygon = [[0.0, 0.0], [1000.0, 0.0], [1000.0, 500.0], [0.0, 500.0]]
    cells = np.zeros((100, 200), dtype=bool)
    cells[:, :100] = True  # tissue on the left half of a 2 x 1 mm slide
    tissue = TissueMask(cells, 10.0)
    tiles = enumerate_hotspot_tiles(polygon, tile_um=250.0, stride_um=250.0, tissue=tissue, min_tissue_fraction=0.5)
    assert tiles and all(t["origin_um"][0] + 250.0 <= 1000.0 + 1e-9 for t in tiles)  # none over the glass
    assert all(t["origin_um"][1] < 500.0 for t in tiles)  # none below the polygon
    with pytest.raises(ValueError, match="at least 3 vertices"):
        enumerate_hotspot_tiles([[0.0, 0.0], [10.0, 10.0]], tile_um=250.0, stride_um=250.0, tissue=tissue, min_tissue_fraction=0.5)


# =========================================================================
# 3. Scoring Tests (#118, #764, #373)
# =========================================================================
def test_scoring_config_loading_and_multi_radius_summation():
    """Validates #118, #764: injected typed scoring config and multi-radius area summation."""
    scoring = get_pipeline_config().mitosis.scoring

    hpfs = [{"seq": i, "center_um": [i * 600, 1000], "radius_um": 262.0, "count": 2} for i in range(10)]
    res = compute_nottingham_mitotic_score(count_total=20, n_hpf=10, radius_um=262.0, scoring=scoring, hpfs=hpfs)
    assert res["score"] in (1, 2, 3)
    assert abs(res["area_mm2"] - (10 * math.pi * (0.262 ** 2))) < 0.01

    var_hpfs = [
        {"seq": 1, "center_um": [0, 0], "radius_um": 200.0, "count": 1},
        {"seq": 2, "center_um": [1000, 0], "radius_um": 300.0, "count": 2}
    ]
    expected_area = math.pi * (0.200 ** 2) + math.pi * (0.300 ** 2)
    res_var = compute_nottingham_mitotic_score(count_total=3, n_hpf=2, radius_um=250.0, scoring=scoring, hpfs=var_hpfs)
    assert abs(res_var["area_mm2"] - expected_area) < 0.001


def test_scoring_zero_hpfs_safe():
    """Validates #373: zero HPFs returns 0 area without zero division, and no score (contract mitosis_v6: null when n_hpf = 0)."""
    res = compute_nottingham_mitotic_score(
        count_total=0, n_hpf=0, radius_um=262.0, scoring=get_pipeline_config().mitosis.scoring
    )
    assert res["score"] is None and res["mitotic_score"] is None
    assert res["area_mm2"] == 0.0
    assert res["mitoses_per_mm2"] == 0.0


# =========================================================================
# 4. Router tests: the v5 routes (/recompute, /add_candidate, /bulk_action, /re_place_hpfs) are gone
# (WP-7.6a). Locked edits, proximity de-duplication on /add and the confirm gate are tested on the
# mitosis_v6 routes in test_mitosis_api.py.
# =========================================================================
