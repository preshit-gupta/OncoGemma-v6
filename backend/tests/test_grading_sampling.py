"""Stage 5 stratified sampling inside the confirmed hotspots (SPEC-07 §4, owner decision 2026-10-02; WP-8.6)."""
import numpy as np
import pytest
from shapely.geometry import Point, Polygon, box

from pipeline.grading_sampling import (
    HotspotFrame,
    SamplingFrameEmptyError,
    TumorTilesInvalidError,
    parse_tumor_tiles,
    plan_samples,
    seed_from_sha256,
    tumor_area_in_box,
)
from tests.fakes.stage3 import TILE_UM, tiles_covering, tiles_parquet_bytes

EXTENT = (20000.0, 20000.0)
WINDOW = 600.0
SHA = "ab" * 32


def square(cx, cy, side=WINDOW):
    h = side / 2
    return [[cx - h, cy - h], [cx + h, cy - h], [cx + h, cy + h], [cx - h, cy + h], [cx - h, cy - h]]


CENTRES = {"hs_a": (3000.0, 3000.0), "hs_b": (6000.0, 3100.0), "hs_c": (3050.0, 7000.0)}


def frames(ids=("hs_a", "hs_b", "hs_c")):
    return [HotspotFrame(id=i, polygon_um=square(*CENTRES[i])) for i in ids]


def tumour_under(ids=("hs_a", "hs_b", "hs_c"), margin=300.0):
    cells = []
    for i in ids:
        cx, cy = CENTRES[i]
        cells += tiles_covering(cx - WINDOW / 2 - margin, cy - WINDOW / 2 - margin, cx + WINDOW / 2 + margin, cy + WINDOW / 2 + margin)
    return parse_tumor_tiles(tiles_parquet_bytes(sorted(set(cells))))


def plan(tiles, frm, seed=None, n_tubule=48, n_pleo=48):
    return plan_samples(
        tiles, frm, EXTENT, n_tubule=n_tubule, n_pleo=n_pleo,
        tubule_size_um=512.0, tubule_mpp=1.0, pleo_size_um=128.0, pleo_mpp=0.25,
        subdivisions=8, n_init=4, seed=seed_from_sha256(SHA) if seed is None else seed,
    )


def on_tumour_tile(tiles, x, y):
    return bool(np.any((tiles.x_um <= x) & (x <= tiles.x_um + tiles.tile_um) & (tiles.y_um <= y) & (y <= tiles.y_um + tiles.tile_um)))


def test_sampling_is_deterministic_for_a_slide():
    tiles = tumour_under()
    first, second = plan(tiles, frames()), plan(tiles, frames())
    assert first == second
    other = plan(tiles, frames(), seed=seed_from_sha256("cd" * 32))
    assert [s.center_um for s in other.tubule] != [s.center_um for s in first.tubule]


def test_every_sample_centre_is_on_a_tumour_tile_inside_a_confirmed_window():
    tiles = tumour_under()
    result = plan(tiles, frames())
    polygons = {f.id: Polygon(f.polygon_um) for f in frames()}
    for s in result.tubule + result.pleo:
        assert on_tumour_tile(tiles, *s.center_um)
        assert polygons[s.hotspot_id].covers(Point(s.center_um))
    assert len(result.tubule) == 48
    assert [s.id for s in result.tubule] == [f"t_{k:02d}" for k in range(1, 49)]
    assert {s.size_um for s in result.tubule} == {512.0} and {s.mpp for s in result.tubule} == {1.0}
    assert {s.size_um for s in result.pleo} == {128.0} and {s.mpp for s in result.pleo} == {0.25}


def test_pleomorphism_fields_lie_inside_their_window_and_never_overlap():
    result = plan(tumour_under(), frames())
    polygons = {f.id: Polygon(f.polygon_um) for f in frames()}
    for f in result.pleo:
        x, y = f.center_um
        assert polygons[f.hotspot_id].covers(box(x - 64, y - 64, x + 64, y + 64))
    centres = np.array([f.center_um for f in result.pleo])
    for k in range(len(centres)):
        others = np.delete(centres, k, axis=0)
        assert np.min(np.max(np.abs(others - centres[k]), axis=1)) >= 128.0


def test_tubule_counts_follow_the_profile_with_one_hotspot_and_pleomorphism_is_never_padded():
    result = plan(tumour_under(("hs_a",)), frames(("hs_a",)))
    assert len(result.tubule) == 48  # overlapping tubule samples are allowed (owner decision 2026-10-02)
    assert len({s.center_um for s in result.tubule}) == 48
    # At most a 4 x 4 grid of separated 128 µm fields fits inside one 600 µm window.
    assert 0 < len(result.pleo) <= 16 < 48


def test_strata_are_numbered_and_unique_per_kind():
    result = plan(tumour_under(), frames())
    assert len({s.stratum for s in result.tubule}) == len(result.tubule)
    assert len({s.stratum for s in result.pleo}) == len(result.pleo)


def test_no_hotspot_or_no_tumour_inside_them_is_a_specific_error():
    with pytest.raises(SamplingFrameEmptyError, match="no confirmed"):
        plan(tumour_under(), [])
    far = parse_tumor_tiles(tiles_parquet_bytes([(80, 80), (81, 80)]))
    with pytest.raises(SamplingFrameEmptyError, match="no tumour tile"):
        plan(far, frames())


def test_only_the_given_frames_are_sampled():
    """Excluded hotspots are left out of the frame list (worker); tumour under them is never sampled."""
    result = plan(tumour_under(), frames(("hs_a", "hs_c")))
    assert {s.hotspot_id for s in result.tubule + result.pleo} <= {"hs_a", "hs_c"}


def test_tumour_area_is_the_exact_intersection_with_the_box():
    tiles = parse_tumor_tiles(tiles_parquet_bytes([(10, 10), (11, 10)]))  # x 2240-2688, y 2240-2464
    assert tumor_area_in_box(tiles, EXTENT, (2464.0, 2352.0), 128.0) == pytest.approx(128.0 ** 2)
    # A box centred on the tumour's top edge holds half its area in tumour.
    assert tumor_area_in_box(tiles, EXTENT, (2464.0, 2240.0), 128.0) == pytest.approx(128.0 ** 2 / 2)
    assert tumor_area_in_box(tiles, EXTENT, (9000.0, 9000.0), 128.0) == 0.0


def test_tubule_boxes_stay_inside_the_slide():
    frm = [HotspotFrame(id="edge", polygon_um=square(300.0, 300.0))]
    tiles = parse_tumor_tiles(tiles_parquet_bytes(tiles_covering(0, 0, 700, 700)))
    result = plan(tiles, frm, n_tubule=6, n_pleo=4)
    for s in result.tubule:
        assert s.center_um[0] >= 256.0 and s.center_um[1] >= 256.0


def test_the_tiles_file_must_be_the_stage_3_format():
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    buf = io.BytesIO()
    pq.write_table(pa.table({"x_um": [0.0], "y_um": [0.0], "is_tumor": [True]}), buf)
    with pytest.raises(TumorTilesInvalidError):
        parse_tumor_tiles(buf.getvalue())


def test_the_seed_needs_the_slide_hash():
    with pytest.raises(ValueError, match="SHA-256"):
        seed_from_sha256("")
    assert TILE_UM == 224.0
