"""Stage A tiling with ownership (SPEC-06 §5.1, AC6) without a detector or a slide."""
import io
import math

import numpy as np
import pytest
from PIL import Image

from app.core.pipeline_config import get_pipeline_config
from pipeline.mitosis_detect import detect_region, plan_tiles

TILE, STRIDE = 512, 448


def detector_cfg(**changes):
    return get_pipeline_config().mitosis.detector.model_copy(update=changes)


@pytest.mark.parametrize("width, height", [(100, 80), (512, 512), (513, 700), (960, 960), (2528, 2528), (3001, 1234)])
def test_owned_areas_partition_the_region(width, height):
    covered = np.zeros((height, width), dtype=int)
    for tile in plan_tiles(width, height, TILE, STRIDE):
        assert 0 <= tile.own_x0 < tile.own_x1 <= TILE and 0 <= tile.own_y0 < tile.own_y1 <= TILE
        covered[tile.y + tile.own_y0:tile.y + tile.own_y1, tile.x + tile.own_x0:tile.x + tile.own_x1] += 1
    assert (covered == 1).all()


def test_regular_tiles_own_32_to_480():
    tiles = plan_tiles(448 * 4 + 64, 448 * 4 + 64, TILE, STRIDE)
    inner = [t for t in tiles if 0 < t.x < 448 * 3 and 0 < t.y < 448 * 3]
    assert inner and all((t.own_x0, t.own_x1, t.own_y0, t.own_y1) == (32, 480, 32, 480) for t in inner)


def fake_detector(objects_um, work_mpp, region_origin_um, prob=0.9):
    """Fires on every object inside each tile it is sent (all overlapping tiles see it)."""
    calls = []

    def detect_batch(patches, ids):
        results = []
        for data, tile_id in zip(patches, ids):
            with Image.open(io.BytesIO(data)) as image:
                assert image.size == (TILE, TILE)
            _, x_px, y_px = tile_id.rsplit("_", 2)
            tx_um = region_origin_um[0] + int(x_px) * work_mpp
            ty_um = region_origin_um[1] + int(y_px) * work_mpp
            found = [
                ((ox - tx_um) / work_mpp, (oy - ty_um) / work_mpp, prob)
                for ox, oy in objects_um
                if 0 <= (ox - tx_um) / work_mpp < TILE and 0 <= (oy - ty_um) / work_mpp < TILE
            ]
            calls.append(tile_id)
            results.append((found, f"rec_{tile_id}"))
        return results

    return detect_batch, calls


def white(x_px, y_px, width, height):
    return Image.new("RGB", (width, height), (240, 240, 240))


def test_an_object_seen_by_every_overlapping_tile_is_kept_once():
    """AC6: a synthetic field; the detector fires on every object in every tile that contains it."""
    rng = np.random.default_rng(7)
    region = (1000.0, 2000.0, 1000.0 + 700.0, 2000.0 + 600.0)
    objects = [(float(x), float(y)) for x, y in zip(rng.uniform(region[0], region[2], 400), rng.uniform(region[1], region[3], 400))]
    # Objects on tile seams and corners are the hard cases.
    objects += [(region[0] + 448 * 0.25 + d, region[1] + 448 * 0.25 + d) for d in (0.0, 4.0, 8.0, 16.0)]
    cfg = detector_cfg()
    detect_batch, calls = fake_detector(objects, cfg.mpp, region[:2])

    points = detect_region(white, region, (0.25, 0.25), cfg, detect_batch, mpp_tolerance=0.01, batch_size=4, threads=2)

    assert len(calls) > 1
    found = sorted((round(p.x_um, 6), round(p.y_um, 6)) for p in points)
    assert found == sorted((round(x, 6), round(y, 6)) for x, y in objects)


def test_a_20x_slide_is_read_in_windows_and_resampled():
    reads = []

    def read(x_px, y_px, width, height):
        reads.append((x_px, y_px, width, height))
        return Image.new("RGB", (width, height), (230, 200, 220))

    objects = [(150.0, 90.0)]
    cfg = detector_cfg()
    detect_batch, _ = fake_detector(objects, cfg.mpp, (0.0, 0.0))
    points = detect_region(read, (0.0, 0.0, 300.0, 200.0), (0.5, 0.5), cfg, detect_batch, mpp_tolerance=0.01, batch_size=1, threads=1)

    # A 512 px tile at 0.25 µm/px is 128 µm: a 256 px window of the 0.5 µm/px slide.
    assert reads and all((w, h) == (256, 256) for *_, w, h in reads)
    assert [(p.x_um, p.y_um) for p in points] == objects


def test_tiles_outside_the_sweep_are_not_sent_and_their_objects_not_kept():
    cfg = detector_cfg()
    objects = [(10.0, 10.0), (250.0, 250.0)]
    detect_batch, calls = fake_detector(objects, cfg.mpp, (0.0, 0.0))

    def left_half_only(x0, y0, x1, y1):
        return x0 < 150.0 and y0 < 150.0

    points = detect_region(white, (0.0, 0.0, 300.0, 300.0), (0.25, 0.25), cfg, detect_batch,
                           mpp_tolerance=0.01, batch_size=4, threads=1, include_tile=left_half_only)
    assert [(p.x_um, p.y_um) for p in points] == [(10.0, 10.0)]
    assert all(math.isfinite(p.prob) for p in points)


def test_a_detector_answering_for_fewer_tiles_is_an_error():
    cfg = detector_cfg()

    def short(patches, ids):
        return [([], "rec")] * (len(patches) - 1)

    with pytest.raises(ValueError, match="results for"):
        detect_region(white, (0.0, 0.0, 300.0, 300.0), (0.25, 0.25), cfg, short, mpp_tolerance=0.01, batch_size=4, threads=1)
