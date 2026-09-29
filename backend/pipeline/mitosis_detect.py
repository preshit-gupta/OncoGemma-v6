"""Stage A of SPEC-06: the detector over a region, at its input resolution, with tile ownership.

The region is swept in square tiles on a grid at the detector's resolution (``cfg.mpp``);
a slide at another resolution is resampled window by window. Consecutive tiles overlap by
``cfg.overlap_px``, and each tile *owns* the half of every overlap nearest to it (for the
regular 512/448 grid, ``[32, 480)`` px), extended to the region's edges. Owned areas
partition the region, so a detection is kept once, by the tile that owns its centre
(SPEC-06 §5.1, AC6). Points are returned raw, down to the ``min_prob`` sent to the
detector; thresholds are applied by the caller.

Used by the mitosis worker and by the evaluation baseline (eval/mitosis_baseline.py), so
the baseline measures the production code path.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from PIL import Image

from app.core.pipeline_config import MitosisDetectorConfig
from app.core.tasks import EntityType, Task
from app.inference.batching import plan_batches
from app.inference.gateway import EntityRef, ImageInput, InputSpec, ModelInputs
from app.inference.outputs import DetectionList


@dataclass(frozen=True)
class Tile:
    """One detector input: a tile of the working grid and the part of it it owns (tile px)."""

    x: int
    y: int
    own_x0: int
    own_y0: int
    own_x1: int
    own_y1: int


@dataclass(frozen=True)
class StageAPoint:
    x_um: float
    y_um: float
    prob: float
    tile_id: str
    record_id: str


# (patch PNGs at cfg.mpp, their ids) -> per patch: ([(x_px, y_px, prob)], DecisionRecord id)
DetectBatch = Callable[[Sequence[bytes], Sequence[str]], Sequence[tuple[Sequence[tuple[float, float, float]], str]]]
# (x_px, y_px, width_px, height_px) at level 0 -> RGB image of that size
ReadNative = Callable[[int, int, int, int], Image.Image]
# (x0_um, y0_um, x1_um, y1_um) of a tile -> whether to sweep it (tissue, hotspot polygon)
IncludeTile = Callable[[float, float, float, float], bool]


def _starts(length: int, tile: int, stride: int) -> list[int]:
    if length <= tile:
        return [0]
    starts = list(range(0, length - tile + 1, stride))
    if starts[-1] + tile < length:
        starts.append(length - tile)
    return starts


def _owned_bounds(starts: list[int], tile: int, length: int) -> list[tuple[int, int]]:
    """Each tile owns up to the middle of its overlap with each neighbour, and to the region edge."""
    bounds = []
    for i, start in enumerate(starts):
        lo = 0 if i == 0 else (starts[i - 1] + tile + start) // 2
        hi = length if i == len(starts) - 1 else (start + tile + starts[i + 1]) // 2
        bounds.append((lo - start, hi - start))
    return bounds


def plan_tiles(width_px: int, height_px: int, tile_px: int, stride_px: int) -> list[Tile]:
    """Tiles covering a width x height grid whose owned areas partition it exactly."""
    xs, ys = _starts(width_px, tile_px, stride_px), _starts(height_px, tile_px, stride_px)
    x_own = _owned_bounds(xs, tile_px, width_px)
    y_own = _owned_bounds(ys, tile_px, height_px)
    return [
        Tile(x, y, ox0, oy0, ox1, oy1)
        for y, (oy0, oy1) in zip(ys, y_own)
        for x, (ox0, ox1) in zip(xs, x_own)
    ]


def _png(image: Image.Image) -> bytes:
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def detect_region(
    read_native: ReadNative,
    region_um: tuple[float, float, float, float],
    slide_mpp: tuple[float, float],
    cfg: MitosisDetectorConfig,
    detect_batch: DetectBatch,
    *,
    mpp_tolerance: float,
    batch_size: int,
    threads: int,
    include_tile: Optional[IncludeTile] = None,
    tile_prefix: str = "t",
) -> list[StageAPoint]:
    """Raw Stage-A points (µm, level-0 frame) in ``region_um`` = (x0, y0, x1, y1).

    The slide's own pixels are sent when both axes are within ``mpp_tolerance`` (relative,
    the detector contract's) of ``cfg.mpp``; otherwise each tile is resampled from level 0.
    """
    x0_um, y0_um, x1_um, y1_um = region_um
    if x1_um <= x0_um or y1_um <= y0_um:
        raise ValueError(f"empty detection region {region_um}")
    mpp_x, mpp_y = slide_mpp
    tile_px, work_mpp = cfg.tile_size_px, cfg.mpp
    width_px = int(round((x1_um - x0_um) / work_mpp))
    height_px = int(round((y1_um - y0_um) / work_mpp))
    resample = abs(mpp_x - work_mpp) / work_mpp > mpp_tolerance or abs(mpp_y - work_mpp) / work_mpp > mpp_tolerance

    tiles = []
    for tile in plan_tiles(width_px, height_px, tile_px, cfg.stride_px):
        tx_um, ty_um = x0_um + tile.x * work_mpp, y0_um + tile.y * work_mpp
        if include_tile is None or include_tile(tx_um, ty_um, tx_um + tile_px * work_mpp, ty_um + tile_px * work_mpp):
            tiles.append(tile)

    def read(tile: Tile) -> bytes:
        tx_um, ty_um = x0_um + tile.x * work_mpp, y0_um + tile.y * work_mpp
        if not resample:
            return _png(read_native(int(round(tx_um / mpp_x)), int(round(ty_um / mpp_y)), tile_px, tile_px))
        # Level-0 window covering the tile's physical extent, resampled to tile_px.
        w = int(round(tile_px * work_mpp / mpp_x))
        h = int(round(tile_px * work_mpp / mpp_y))
        window = read_native(int(round(tx_um / mpp_x)), int(round(ty_um / mpp_y)), w, h)
        return _png(window.resize((tile_px, tile_px), Image.LANCZOS))

    def sweep(group: list[Tile]) -> list[StageAPoint]:
        ids = [f"{tile_prefix}_{t.x}_{t.y}" for t in group]
        results = detect_batch([read(t) for t in group], ids)
        if len(results) != len(group):
            raise ValueError(f"detector returned {len(results)} results for {len(group)} tiles")
        kept = []
        for tile, tile_id, (found, record_id) in zip(group, ids, results):
            for x, y, prob in found:
                if tile.own_x0 <= x < tile.own_x1 and tile.own_y0 <= y < tile.own_y1:
                    kept.append(StageAPoint(
                        x0_um + (tile.x + x) * work_mpp,
                        y0_um + (tile.y + y) * work_mpp,
                        float(prob),
                        tile_id,
                        record_id,
                    ))
        return kept

    groups = [tiles[start:start + batch_size] for start in range(0, len(tiles), batch_size)]
    with ThreadPoolExecutor(max_workers=threads) as pool:
        return [point for kept in pool.map(sweep, groups) for point in kept]


def make_detect_batch(gateway, ctx, cfg: MitosisDetectorConfig, entry) -> DetectBatch:
    """A ``DetectBatch`` over the model gateway: ``mitosis_detect`` calls, split to the registry's limits."""
    size = tuple(entry.input.size_px)
    spec = InputSpec(mpp=cfg.mpp, size_px=size, color=entry.input.color, format=entry.input.format)
    limits = entry.limits

    def detect(patches: Sequence[bytes], ids: Sequence[str]):
        images = [ImageInput(data, spec) for data in patches]
        results: list = [None] * len(images)
        for batch in plan_batches([len(image.data) for image in images], limits.max_batch, limits.max_request_bytes):
            result = gateway.invoke(
                Task.MITOSIS_DETECT,
                cfg.producer,
                ModelInputs(images=tuple(images[i] for i in batch)),
                ctx,
                EntityRef(EntityType.TILE_BATCH, f"{ids[batch[0]]}_b{len(batch)}", ids=tuple(ids[i] for i in batch)),
                DetectionList,
                params={"min_prob": cfg.min_prob},
            )
            if len(result.output.detections) != len(batch):
                raise ValueError(f"{cfg.producer} returned {len(result.output.detections)} point lists for {len(batch)} tiles")
            for i, points in zip(batch, result.output.detections):
                results[i] = ([(p.x, p.y, p.prob) for p in points], str(result.record_id))
        return results

    return detect
