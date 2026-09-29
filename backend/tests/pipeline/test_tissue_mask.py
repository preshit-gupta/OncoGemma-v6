"""The registered tissue mask (SPEC-04 §3.6, AC4 and AC5)."""
import json
import math

import numpy as np
import openslide
import pytest
from PIL import Image

from app.core.pipeline_config import TissueMaskConfig, get_pipeline_config
from pipeline.errors import TissueMaskError
from pipeline.slide_io import SlideReader
from pipeline.tissue_mask import (
    MASK_ALGORITHM_VERSION,
    TissueMask,
    compute_tissue_mask,
    otsu_threshold,
)
from tests.fakes.slide import FakeOpenSlide

GLASS = (242, 242, 242)
PINK = (225, 150, 185)
GREEN_INK = (40, 180, 60)


def profile(name: str) -> TissueMaskConfig:
    return get_pipeline_config().specimen_profiles.profiles[name].tissue_mask


def pen_ranges():
    """The QC pen-mark settings: ink colours and the size of a mark."""
    return get_pipeline_config().qc.pen_marks


# -- exact queries --------------------------------------------------------------------------------


def blobs(seed: int, shape=(30, 40)) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.random(shape) < 0.45


def brute_force_box(mask: np.ndarray, mpp: float, box, k: int = 4) -> float:
    """Tissue fraction of a box whose corners lie on a 1/k-pixel grid, by upsampling the mask k times."""
    x0, y0, x1, y1 = (round(v / mpp * k) for v in box)
    up = np.kron(mask, np.ones((k, k), dtype=bool))
    height, width = up.shape
    inside = up[max(0, y0) : max(0, min(y1, height)), max(0, x0) : max(0, min(x1, width))]
    return float(inside.sum()) / ((x1 - x0) * (y1 - y0))


def test_box_fractions_are_exact_for_fractional_edges_and_off_slide_parts():
    mask, mpp = blobs(1), 8.0
    tissue = TissueMask(mask, mpp)
    rng = np.random.default_rng(2)
    for _ in range(300):
        x0, y0 = rng.integers(-40, 320, size=2) / 4 * mpp / 1.0, rng.integers(-40, 240, size=2)[0] / 4 * mpp
        x0 = float(rng.integers(-16, 300)) / 4 * mpp
        y0 = float(rng.integers(-16, 220)) / 4 * mpp
        w, h = (float(v) / 4 * mpp for v in rng.integers(1, 90, size=2))
        box = (x0, y0, x0 + w, y0 + h)
        assert tissue.fraction_in_box_um(*box) == pytest.approx(brute_force_box(mask, mpp, box), abs=1e-12)


def test_a_box_inside_one_pixel_takes_that_pixels_value():
    tissue = TissueMask(np.array([[True, False], [False, False]]), 10.0)
    assert tissue.fraction_in_box_um(2, 2, 8, 8) == 1.0
    assert tissue.fraction_in_box_um(12, 2, 18, 8) == 0.0
    assert tissue.fraction_in_box_um(5, 0, 15, 10) == 0.5  # half in each pixel


def test_a_box_wholly_off_the_slide_is_not_tissue_and_a_bad_box_raises():
    tissue = TissueMask(np.ones((4, 4), dtype=bool), 5.0)
    assert tissue.fraction_in_box_um(100, 100, 120, 120) == 0.0
    assert tissue.fraction_in_box_um(10, 10, 30, 30) == 0.25  # only the 10 x 10 µm corner is on the slide
    with pytest.raises(ValueError, match="x1 > x0"):
        tissue.fraction_in_box_um(5, 5, 5, 9)


def brute_force_disk(mask: np.ndarray, mpp: float, cx: float, cy: float, r: float, k: int = 16) -> float:
    up = np.kron(mask, np.ones((k, k), dtype=bool))
    ys, xs = np.mgrid[0 : up.shape[0], 0 : up.shape[1]]
    inside = ((xs + 0.5) / k * mpp - cx) ** 2 + ((ys + 0.5) / k * mpp - cy) ** 2 <= r**2
    return float((up & inside).sum() / inside.sum()) if inside.any() else 0.0


@pytest.mark.parametrize("cx, cy, r", [(160.0, 120.0, 60.0), (30.0, 40.0, 50.0), (300.0, 200.0, 33.3), (150.0, 100.0, 7.0)])
def test_disk_fractions_match_a_fine_rasterisation(cx, cy, r):
    mask, mpp = blobs(3), 8.0
    disk = TissueMask(mask, mpp).fraction_in_disk_um(cx, cy, r)
    inside_slide = 0 <= cx - r and cx + r <= 40 * mpp and 0 <= cy - r and cy + r <= 30 * mpp
    reference = brute_force_disk(mask, mpp, cx, cy, r)
    if inside_slide:
        assert disk == pytest.approx(reference, abs=0.01)
    else:
        # The disk's area outside the slide counts as non-tissue, so the fraction can only be lower.
        assert disk <= reference + 0.01


def test_a_disk_off_the_slide_or_a_bad_radius():
    tissue = TissueMask(np.ones((20, 20), dtype=bool), 10.0)
    assert tissue.fraction_in_disk_um(500, 500, 20) == 0.0
    assert tissue.fraction_in_disk_um(100, 100, 60) == pytest.approx(1.0, abs=0.01)
    with pytest.raises(ValueError, match="r must be positive"):
        tissue.fraction_in_disk_um(100, 100, 0)


def test_contains_um_uses_the_pixel_grid_from_the_slide_origin():
    tissue = TissueMask(np.array([[False, True], [True, False]]), 10.0)
    assert [tissue.contains_um(x, y) for x, y in [(15, 5), (5, 15), (5, 5), (19.99, 19.99)]] == [True, True, False, False]
    assert not tissue.contains_um(-0.01, 5) and not tissue.contains_um(5, 20.0) and not tissue.contains_um(20.0, 5)


def test_tiles_are_whole_grid_squares_with_enough_tissue():
    mask = np.zeros((20, 30), dtype=bool)
    mask[0:10, 0:10] = True  # the top-left 100 x 100 µm at mpp 10
    mask[10:15, 10:20] = True  # half of tile (col 1, row 1)
    tissue = TissueMask(mask, 10.0)
    tiles = list(tissue.tiles(100.0, 0.5))
    assert [(t.col, t.row, t.x_um, t.y_um, t.size_um, t.tissue_fraction) for t in tiles] == [
        (0, 0, 0.0, 0.0, 100.0, 1.0),
        (1, 1, 100.0, 100.0, 100.0, 0.5),
    ]
    assert [(t.col, t.row) for t in tissue.tiles(100.0, 0.0)] == [(c, r) for r in range(2) for c in range(3)]
    assert list(tissue.tiles(100.0, 0.51)) == tiles[:1]
    assert list(tissue.tiles(400.0, 0.0)) == []  # no whole tile fits
    for tile in tiles:  # the vectorised grid agrees with the scalar query
        assert tile.tissue_fraction == tissue.fraction_in_box_um(tile.x_um, tile.y_um, tile.x_um + 100, tile.y_um + 100)


def test_tiles_do_not_cover_a_partial_edge_tile():
    tissue = TissueMask(np.ones((10, 25), dtype=bool), 10.0)  # 250 x 100 µm
    assert [(t.col, t.row) for t in tissue.tiles(100.0, 1.0)] == [(0, 0), (1, 0)]


def test_at_mpp_resamples_by_area():
    mask = np.zeros((8, 8), dtype=bool)
    mask[:, :5] = True
    tissue = TissueMask(mask, 5.0)
    assert np.array_equal(tissue.at_mpp(5.0, 8, 8), mask)
    coarse = tissue.at_mpp(10.0, 4, 4)  # 2x2 blocks: columns 0-1 tissue, column 2 half, column 3 empty
    assert coarse.tolist() == [[True, True, True, False]] * 4


def test_origins_are_seeded_inside_the_slide_and_centred_on_tissue():
    mask = np.zeros((100, 200), dtype=bool)
    mask[40:60, 100:140] = True
    tissue = TissueMask(mask, 10.0)  # 2000 x 1000 µm
    first = tissue.sample_origins_um(30, 256.0, np.random.default_rng(5))
    assert first == tissue.sample_origins_um(30, 256.0, np.random.default_rng(5))
    assert first != tissue.sample_origins_um(30, 256.0, np.random.default_rng(6))
    assert len(first) == 30
    for x, y in first:
        assert 0 <= x <= 2000 - 256 and 0 <= y <= 1000 - 256
        assert tissue.contains_um(x + 128, y + 128) or x in (0, 2000 - 256) or y in (0, 1000 - 256)
    assert tissue.sample_origins_um(5, 256.0, np.random.default_rng(0)) != []
    assert TissueMask(np.zeros((10, 10), dtype=bool), 10.0).sample_origins_um(5, 20.0, np.random.default_rng(0)) == []


def test_a_mask_is_validated():
    for bad in (np.zeros((0, 5), dtype=bool), np.zeros((4, 4), dtype=np.uint8), np.zeros((2, 2, 2), dtype=bool)):
        with pytest.raises(TissueMaskError, match="non-empty 2-D boolean"):
            TissueMask(bad, 1.0)
    with pytest.raises(TissueMaskError, match="mpp"):
        TissueMask(np.ones((2, 2), dtype=bool), 0.0)


def test_area_in_mm2():
    assert TissueMask(np.ones((100, 200), dtype=bool), 10.0).area_mm2 == pytest.approx(2.0)  # 20000 px x 100 µm²


# -- persistence -----------------------------------------------------------------------------------


def test_artifacts_round_trip_and_the_png_is_one_bit():
    mask = blobs(4)
    tissue = TissueMask(mask, 4.0, profile="core_biopsy")
    png, meta = tissue.to_artifacts(params={"open_radius_um": 12.0})
    assert Image.open(__import__("io").BytesIO(png)).mode == "1"
    assert meta == {
        "mpp": 4.0, "width_px": 40, "height_px": 30, "origin_um": [0.0, 0.0],
        "algorithm_version": MASK_ALGORITHM_VERSION, "profile": "core_biopsy", "params": {"open_radius_um": 12.0},
    }
    again = TissueMask.from_json_bytes(png, json.dumps(meta).encode())
    assert np.array_equal(again.array, mask) and (again.mpp, again.profile) == (4.0, "core_biopsy")


@pytest.mark.parametrize(
    "edit, message",
    [
        (lambda m: m.update(width_px=41), "says"),
        (lambda m: m.pop("mpp"), "unreadable"),
        (lambda m: m.update(origin_um=[5.0, 0.0]), "slide origin"),
        (lambda m: m.update(mpp="fast"), "unreadable"),
    ],
)
def test_inconsistent_artifacts_are_refused(edit, message):
    png, meta = TissueMask(blobs(4), 4.0).to_artifacts()
    edit(meta)
    with pytest.raises(TissueMaskError, match=message):
        TissueMask.from_artifacts(png, meta)


def test_unreadable_artifacts_are_refused():
    png, meta = TissueMask(blobs(4), 4.0).to_artifacts()
    with pytest.raises(TissueMaskError, match="unreadable"):
        TissueMask.from_artifacts(b"not a png", meta)
    with pytest.raises(TissueMaskError, match="not valid JSON"):
        TissueMask.from_json_bytes(png, b"{oops")


# -- Otsu ------------------------------------------------------------------------------------------


def test_otsu_separates_a_bimodal_histogram():
    histogram = np.zeros(256, dtype=int)
    histogram[60:90] = 100  # tissue
    histogram[230:250] = 400  # glass
    assert 89 <= otsu_threshold(histogram) < 230
    assert otsu_threshold(np.zeros(256, dtype=int)) == 0  # a blank histogram has no split; the caller clips it


# -- computing the mask from a slide ------------------------------------------------------------------


def picture_of(mpp_level0: float, squares_um=(), ink_um=(), core=None, holes_um=()):
    """A picture function of level-0 pixel coordinates: pink squares (x, y, w, h µm) on glass."""

    def picture(xs, ys):
        x, y = xs * mpp_level0, ys * mpp_level0
        rgb = np.empty(xs.shape + (3,), dtype=np.uint8)
        rgb[:] = GLASS
        tissue = np.zeros(xs.shape, dtype=bool)
        for sx, sy, w, h in squares_um:
            tissue |= (x >= sx) & (x < sx + w) & (y >= sy) & (y < sy + h)
        if core is not None:
            (ax, ay), (bx, by), radius = core
            dx, dy = bx - ax, by - ay
            t = np.clip(((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy), 0, 1)
            tissue |= (x - (ax + t * dx)) ** 2 + (y - (ay + t * dy)) ** 2 <= radius**2
        for sx, sy, w, h in holes_um:
            tissue &= ~((x >= sx) & (x < sx + w) & (y >= sy) & (y < sy + h))
        rgb[tissue] = PINK
        for sx, sy, w, h in ink_um:
            rgb[(x >= sx) & (x < sx + w) & (y >= sy) & (y < sy + h)] = GREEN_INK
        return rgb

    return picture


def open_slide(monkeypatch, width_px, height_px, mpp, **picture_args) -> SlideReader:
    slide = FakeOpenSlide(width_px, height_px, picture=picture_of(mpp, **picture_args))
    monkeypatch.setattr(openslide, "OpenSlide", lambda path: slide)
    return SlideReader("mask.svs", mpp, mpp, "svs")


def centroid_um(mask: TissueMask, box_um) -> tuple[float, float]:
    """Centroid, in µm, of the mask pixels inside a box."""
    x0, y0, x1, y1 = (round(v / mask.mpp) for v in box_um)
    rows, cols = np.nonzero(mask.array[y0:y1, x0:x1])
    return ((cols.mean() + x0 + 0.5) * mask.mpp, (rows.mean() + y0 + 0.5) * mask.mpp)


def test_ac4_a_60_by_20_mm_slide_registers_tissue_within_one_mask_pixel(monkeypatch):
    """Tissue squares at known positions; the slide is wider than the 50 mm v5 capped it at."""
    squares = [(2000.0, 3000.0, 3000.0, 2000.0), (30500.0, 9000.0, 4000.0, 4000.0), (54000.0, 14000.0, 4000.0, 5000.0)]
    reader = open_slide(monkeypatch, 7500, 2500, 8.0, squares_um=squares)  # 60 x 20 mm at 8 µm/px
    cfg = profile("resection")
    mask = compute_tissue_mask(reader, cfg, pen_ranges(), "resection")

    assert (mask.width_px, mask.height_px, mask.mpp) == (7500, 2500, 8.0)  # aspect preserved, no 50 mm cap
    for x, y, w, h in squares:
        cx, cy = centroid_um(mask, (x - 400, y - 400, x + w + 400, y + h + 400))
        assert abs(cx - (x + w / 2)) <= mask.mpp and abs(cy - (y + h / 2)) <= mask.mpp
    assert mask.area_mm2 == pytest.approx(sum(w * h for _, _, w, h in squares) / 1e6, rel=0.02)
    assert (mask.algorithm_version, mask.profile) == (MASK_ALGORITHM_VERSION, "resection")


def test_the_mask_is_the_same_however_the_extent_is_split_into_strips(monkeypatch):
    squares = [(2000.0, 3000.0, 3000.0, 2000.0), (30500.0, 9000.0, 4000.0, 4000.0)]
    reader = open_slide(monkeypatch, 7500, 2500, 8.0, squares_um=squares)
    whole = compute_tissue_mask(reader, profile("resection"), pen_ranges(), "resection")
    monkeypatch.setattr("pipeline.slide_io.STRIP_MAX_PIXELS", 7500 * 37)
    strips = compute_tissue_mask(reader, profile("resection"), pen_ranges(), "resection")
    assert np.array_equal(whole.array, strips.array)


def test_a_non_square_slide_keeps_its_aspect_ratio(monkeypatch):
    reader = open_slide(monkeypatch, 3000, 1000, 10.0, squares_um=[(1000.0, 1000.0, 3000.0, 3000.0)])
    mask = compute_tissue_mask(reader, profile("resection"), pen_ranges(), "resection")  # 30 x 10 mm at 8 µm/px
    assert (mask.width_px, mask.height_px) == (math.ceil(30000 / 8), math.ceil(10000 / 8))


def test_ac5_a_thin_diagonal_core_keeps_its_area_in_the_core_biopsy_mask(monkeypatch):
    """A 1.0 mm x 15 mm core across the slide; v5's opening on an 80-column grid ate cores this thin."""
    start, end = (1500.0, 1500.0), (1500.0 + 15000 / math.sqrt(2), 1500.0 + 15000 / math.sqrt(2))
    core = (start, end, 500.0)
    reader = open_slide(monkeypatch, 3000, 3000, 4.0, core=core)  # 12 x 12 mm at 4 µm/px
    cfg = profile("core_biopsy")
    mask = compute_tissue_mask(reader, cfg, pen_ranges(), "core_biopsy")

    truth = picture_of(4.0, core=core)(*np.meshgrid(np.arange(3000), np.arange(3000)))
    truth_area = int((truth[..., 0] == PINK[0]).sum())
    assert truth_area > 0.9 * (1.0 * 12.0 * 1e6 / 16)  # the core really is ~1 mm wide and long
    kept = int((mask.array & (truth[..., 0] == PINK[0])).sum())
    assert kept / truth_area >= 0.95
    assert mask.array.sum() <= 1.05 * truth_area  # and the mask does not balloon


def test_small_fragments_are_dropped_and_small_holes_filled(monkeypatch):
    big = (2000.0, 2000.0, 12000.0, 12000.0)
    reader = open_slide(
        monkeypatch, 3000, 3000, 8.0,  # 24 x 24 mm
        squares_um=[big, (18000.0, 2000.0, 300.0, 300.0)],  # dust: 0.09 mm² < 0.2 mm²
        # fill_holes_max_um2 is 0.04 mm²: a 0.0225 mm² hole is filled, 0.09 mm² and 9 mm² holes stay
        holes_um=[(6000.0, 6000.0, 150.0, 150.0), (7500.0, 7500.0, 300.0, 300.0), (9000.0, 9000.0, 3000.0, 3000.0)],
    )
    mask = compute_tissue_mask(reader, profile("resection"), pen_ranges(), "resection")
    assert not mask.contains_um(18150, 2150)  # dust gone
    assert mask.contains_um(6075, 6075)  # small hole filled
    assert not mask.contains_um(7650, 7650)  # medium hole kept
    assert not mask.contains_um(10500, 10500)  # large hole kept
    assert mask.contains_um(4000, 4000)


def test_pen_ink_is_not_tissue(monkeypatch):
    reader = open_slide(
        monkeypatch, 2000, 2000, 8.0,
        squares_um=[(1000.0, 1000.0, 10000.0, 10000.0)],
        ink_um=[(3000.0, 3000.0, 3000.0, 3000.0)],
    )
    mask = compute_tissue_mask(reader, profile("resection"), pen_ranges(), "resection")
    assert mask.contains_um(8000, 8000) and mask.contains_um(2000, 2000)
    assert not mask.contains_um(4500, 4500)  # green ink over tissue


def test_blue_speckle_is_tissue_but_a_large_blue_region_is_ink(monkeypatch):
    """Hematoxylin can fall in the blue pen colour range: only a connected region the size of a QC pen mark is ink."""
    blue = (60, 70, 170)  # HSV hue ~117 (OpenCV), saturation and value inside the configured blue range

    def picture(xs, ys):
        rgb = picture_of(8.0, squares_um=[(1000.0, 1000.0, 10000.0, 10000.0)])(xs, ys)
        x, y = xs * 8.0, ys * 8.0
        speckle = ((xs % 6) < 2) & ((ys % 6) < 2) & (x < 6000)  # 16 µm dots, far below 1 mm²
        patch = (x >= 8000) & (x < 11000) & (y >= 8000) & (y < 11000)  # 9 mm² of solid blue
        rgb[(speckle | patch) & (rgb[..., 0] != 242)] = blue
        return rgb

    slide = FakeOpenSlide(2000, 2000, picture=picture)
    monkeypatch.setattr(openslide, "OpenSlide", lambda path: slide)
    reader = SlideReader("mask.svs", 8.0, 8.0, "svs")
    mask = compute_tissue_mask(reader, profile("resection"), pen_ranges(), "resection")
    assert mask.contains_um(3000, 3000) and mask.contains_um(5000, 2000)  # speckled tissue stays
    assert not mask.contains_um(9500, 9500)  # the solid blue patch is ink


def test_an_empty_slide_gives_an_empty_mask(monkeypatch):
    reader = open_slide(monkeypatch, 1000, 1000, 8.0)
    mask = compute_tissue_mask(reader, profile("resection"), pen_ranges(), "resection")
    assert mask.area_mm2 == 0.0 and not mask.array.any()


def test_the_mask_follows_the_profiles_resolution(monkeypatch):
    reader = open_slide(monkeypatch, 2000, 2000, 4.0, squares_um=[(1000.0, 1000.0, 4000.0, 4000.0)])
    assert compute_tissue_mask(reader, profile("core_biopsy"), pen_ranges(), "core_biopsy").mpp == 4.0
    assert compute_tissue_mask(reader, profile("resection"), pen_ranges(), "resection").mpp == 8.0
