"""
Stage 5 stratified sampling inside the confirmed Stage 3 hotspot windows (SPEC-07 §4; WP-8.6).

The frame is the tumour tiles of ``triage/tiles.parquet`` (``is_tumor``) that lie inside a
confirmed, non-excluded hotspot window (owner decision 2026-10-02; SPEC-07 §4 says the whole
tumour mask). Each tumour tile is split into ``subdivisions`` x ``subdivisions`` cells; the cell
centres inside a window are the candidate sample centres, weighted by their area. Weighted k-means
(``k`` = the number of samples, seeded by the slide SHA-256) gives the strata, and each stratum
gives one sample at its member nearest the centroid:

- tubule samples (512 µm @ 1.0 µm/px) may overlap; the box may extend past its window, and the
  centre is moved just enough to keep the box inside the slide (the read would shift it anyway);
- pleomorphism fields (128 µm @ 0.25 µm/px) lie inside their window and are pairwise at least
  one field apart (Chebyshev, so the boxes never overlap); a stratum with no admissible member
  gives no field, and the shortfall is reported, never padded.

``tumor_area_um2`` is the exact area of the tumour mask (the union of the tumour tiles, clipped to
the slide) inside the sample box.
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass

import numpy as np
import pyarrow.parquet as pq
import shapely
import shapely.prepared
from shapely.geometry import Polygon, box
from sklearn.cluster import KMeans

TILES_FORMAT = "triage_tiles_v1"
TILES_METADATA_KEY = b"oncogemma.triage_tiles"


class SamplingFrameEmptyError(ValueError):
    """No tumour inside the confirmed hotspots: Stage 5 has nothing to sample (no whole-slide fallback)."""


class TumorTilesInvalidError(ValueError):
    """``triage/tiles.parquet`` is not the format Stage 3 writes."""


@dataclass(frozen=True)
class TumorTiles:
    """The tumour tiles of the Stage 3 grid: top-left corners in µm and the tile side."""

    x_um: np.ndarray
    y_um: np.ndarray
    tile_um: float


@dataclass(frozen=True)
class HotspotFrame:
    """A confirmed hotspot window (its ``polygon_um`` as stored, pathologist edits included)."""

    id: str
    polygon_um: list


@dataclass(frozen=True)
class Sample:
    id: str
    center_um: tuple[float, float]
    size_um: float
    mpp: float
    stratum: int
    hotspot_id: str
    tumor_area_um2: float


@dataclass(frozen=True)
class SamplePlan:
    tubule: list[Sample]
    pleo: list[Sample]
    n_candidates: int


def parse_tumor_tiles(data: bytes) -> TumorTiles:
    """The tumour tiles from ``triage/tiles.parquet`` bytes (SPEC-05 §4.3)."""
    table = pq.read_table(io.BytesIO(data))
    raw = (table.schema.metadata or {}).get(TILES_METADATA_KEY)
    if raw is None:
        raise TumorTilesInvalidError("tiles.parquet has no oncogemma.triage_tiles metadata")
    meta = json.loads(raw)
    if meta.get("format") != TILES_FORMAT:
        raise TumorTilesInvalidError(f"tiles.parquet format is {meta.get('format')!r}, expected {TILES_FORMAT!r}")
    missing = {"x_um", "y_um", "is_tumor"} - set(table.column_names)
    if missing:
        raise TumorTilesInvalidError(f"tiles.parquet lacks columns {sorted(missing)}")
    is_tumor = table.column("is_tumor").to_numpy(zero_copy_only=False).astype(bool)
    x = table.column("x_um").to_numpy().astype(np.float64)[is_tumor]
    y = table.column("y_um").to_numpy().astype(np.float64)[is_tumor]
    return TumorTiles(x_um=x, y_um=y, tile_um=float(meta["tile_um"]))


def seed_from_sha256(sha256: str) -> int:
    """The k-means seed: the slide SHA-256 (SPEC-07 §4), folded to 32 bits."""
    if not sha256:
        raise ValueError("Stage 5 sampling is seeded by the slide SHA-256, and this slide has none")
    return int(sha256[:16], 16) % (2 ** 32)


def _overlap_1d(lo: np.ndarray, hi: np.ndarray, a: float, b: float) -> np.ndarray:
    return np.clip(np.minimum(hi, b) - np.maximum(lo, a), 0.0, None)


def tumor_area_in_box(tiles: TumorTiles, extent_um: tuple[float, float], center_um: tuple[float, float], size_um: float) -> float:
    """Exact area (µm²) of the tumour mask, clipped to the slide, inside the square box."""
    width, height = extent_um
    half = size_um / 2.0
    x0, x1 = max(center_um[0] - half, 0.0), min(center_um[0] + half, width)
    y0, y1 = max(center_um[1] - half, 0.0), min(center_um[1] + half, height)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    t = tiles.tile_um
    dx = _overlap_1d(tiles.x_um, tiles.x_um + t, x0, x1)
    dy = _overlap_1d(tiles.y_um, tiles.y_um + t, y0, y1)
    return float(np.sum(dx * dy))


def candidate_cells(
    tiles: TumorTiles, frames: list[HotspotFrame], extent_um: tuple[float, float], subdivisions: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Cell centres of the tumour tiles inside a window: points (N, 2), area weights (N,), frame index (N,).

    A centre on two windows belongs to the first (the frames come in rank order).
    """
    width, height = extent_um
    cell = tiles.tile_um / subdivisions
    offsets = (np.arange(subdivisions) + 0.5) * cell
    ox, oy = np.meshgrid(offsets, offsets)
    xs = (tiles.x_um[:, None] + ox.ravel()[None, :]).ravel()
    ys = (tiles.y_um[:, None] + oy.ravel()[None, :]).ravel()
    half = cell / 2.0
    weights = _overlap_1d(xs - half, xs + half, 0.0, width) * _overlap_1d(ys - half, ys + half, 0.0, height)
    frame_of = np.full(xs.shape, -1, dtype=np.int64)
    for k, frame in enumerate(frames):
        inside = shapely.contains_xy(Polygon(frame.polygon_um), xs, ys) & (frame_of < 0)
        frame_of[inside] = k
    keep = (frame_of >= 0) & (weights > 0)
    return np.column_stack([xs[keep], ys[keep]]), weights[keep], frame_of[keep]


def _strata(points: np.ndarray, weights: np.ndarray, k: int, seed: int, n_init: int) -> tuple[np.ndarray, np.ndarray]:
    """Stratum label per point and the centroids, strata numbered by centroid (y, then x)."""
    if k >= len(points):
        labels, centroids = np.arange(len(points)), points.copy()
    else:
        km = KMeans(n_clusters=k, n_init=n_init, random_state=seed).fit(points, sample_weight=weights)
        labels, centroids = km.labels_, km.cluster_centers_
    order = np.lexsort((centroids[:, 0], centroids[:, 1]))
    rank = np.empty_like(order)
    rank[order] = np.arange(len(order))
    return rank[labels], centroids[order]


def _clamp(value: float, half: float, limit: float) -> float:
    return min(max(value, half), limit - half)


def plan_samples(
    tiles: TumorTiles,
    frames: list[HotspotFrame],
    extent_um: tuple[float, float],
    *,
    n_tubule: int,
    n_pleo: int,
    tubule_size_um: float,
    tubule_mpp: float,
    pleo_size_um: float,
    pleo_mpp: float,
    subdivisions: int,
    n_init: int,
    seed: int,
) -> SamplePlan:
    """Tubule samples and pleomorphism fields (module docstring). Raises ``SamplingFrameEmptyError``."""
    if not frames:
        raise SamplingFrameEmptyError("the case has no confirmed, non-excluded Stage 3 hotspot to sample from")
    points, weights, frame_of = candidate_cells(tiles, frames, extent_um, subdivisions)
    if len(points) == 0:
        raise SamplingFrameEmptyError("no tumour tile lies inside the confirmed Stage 3 hotspots")
    width, height = extent_um

    tubule: list[Sample] = []
    labels, centroids = _strata(points, weights, n_tubule, seed, n_init)
    half = tubule_size_um / 2.0
    for s, centroid in enumerate(centroids):
        members = np.flatnonzero(labels == s)
        if len(members) == 0:
            continue
        m = members[np.argmin(np.sum((points[members] - centroid) ** 2, axis=1))]
        center = (round(_clamp(points[m, 0], half, width), 1), round(_clamp(points[m, 1], half, height), 1))
        tubule.append(Sample(
            id=f"t_{len(tubule) + 1:02d}", center_um=center, size_um=tubule_size_um, mpp=tubule_mpp, stratum=s,
            hotspot_id=frames[frame_of[m]].id,
            tumor_area_um2=round(tumor_area_in_box(tiles, extent_um, center, tubule_size_um), 1),
        ))

    pleo: list[Sample] = []
    labels, centroids = _strata(points, weights, n_pleo, seed, n_init)
    windows = [shapely.prepared.prep(Polygon(f.polygon_um)) for f in frames]
    half = pleo_size_um / 2.0
    chosen = np.empty((0, 2))
    for s, centroid in enumerate(centroids):
        members = np.flatnonzero(labels == s)
        members = members[np.argsort(np.sum((points[members] - centroid) ** 2, axis=1), kind="stable")]
        for m in members:
            cx, cy = round(float(points[m, 0]), 1), round(float(points[m, 1]), 1)
            if len(chosen) and np.min(np.max(np.abs(chosen - (cx, cy)), axis=1)) < pleo_size_um:
                continue
            if not windows[frame_of[m]].contains(box(cx - half, cy - half, cx + half, cy + half)):
                continue
            center = (cx, cy)
            chosen = np.vstack([chosen, [cx, cy]])
            pleo.append(Sample(
                id=f"p_{len(pleo) + 1:02d}", center_um=center, size_um=pleo_size_um, mpp=pleo_mpp, stratum=s,
                hotspot_id=frames[frame_of[m]].id,
                tumor_area_um2=round(tumor_area_in_box(tiles, extent_um, center, pleo_size_um), 1),
            ))
            break
    return SamplePlan(tubule=tubule, pleo=pleo, n_candidates=len(points))
