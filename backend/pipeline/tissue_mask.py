"""The registered tissue mask (SPEC-04 §3.6).

Stage 2 computes one mask per slide over the slide's full extent, at the specimen profile's
resolution, so pixel (row, column) covers the µm square [column, column + 1) x [row, row + 1)
times ``mpp`` from the slide origin. Every later stage asks it questions in µm; nothing
resizes the mask in proportion to a slide size any more. It is persisted as ``tissue_mask.png``
(1-bit) and ``tissue_mask.json``.
"""
from __future__ import annotations

import io
import json
import math
from dataclasses import dataclass
from typing import Iterator

import cv2
import numpy as np
from PIL import Image

from app.core.pipeline_config import PenHsvRanges, PenMarksQC, TissueMaskConfig
from pipeline.errors import TissueMaskError
from pipeline.slide_io import SlideReader, iter_extent_strips

# Bump on any change that alters a computed mask.
MASK_ALGORITHM_VERSION = "tissue_mask_v2"

# Millimetres per micrometre.
MM_PER_UM = 1e-3
# Sub-pixel samples per axis for the pixels a disk's edge crosses.
DISK_EDGE_SAMPLES = 8
# Horizontal strips a disk is cut into for the vectorised disk fractions (relative area error ~ 1 / STRIPS²).
DISK_STRIPS = 128
HSV_CHANNEL_MAX = 255
GRAY_LEVELS = 256


def disk_strips(r: float, n_strips: int = DISK_STRIPS) -> tuple[np.ndarray, np.ndarray, float]:
    """A disk of radius ``r`` as ``n_strips`` horizontal strips: (centre offsets dy, half widths, strip height).

    Each strip is the rectangle of its midline's chord, so the strips cover the disk to within ~1 / n_strips².
    """
    if not r > 0:
        raise ValueError(f"r must be positive, got {r!r}")
    height = 2.0 * r / n_strips
    dy = -r + (np.arange(n_strips) + 0.5) * height
    return dy, np.sqrt(r * r - dy * dy), height


@dataclass(frozen=True)
class TileRef:
    col: int
    row: int
    x_um: float
    y_um: float
    size_um: float
    tissue_fraction: float


class TissueMask:
    """A boolean tissue image and the exact area queries over it.

    ``array[row, column]`` is True for tissue. The origin is the slide's (0, 0) µm corner.
    """

    def __init__(self, array: np.ndarray, mpp: float, *, algorithm_version: str = MASK_ALGORITHM_VERSION, profile: str | None = None):
        array = np.asarray(array)
        if array.dtype != np.bool_ or array.ndim != 2 or array.size == 0:
            raise TissueMaskError(f"a tissue mask is a non-empty 2-D boolean array, got {array.dtype} {array.shape}")
        if not math.isfinite(mpp) or mpp <= 0:
            raise TissueMaskError(f"mpp must be positive, got {mpp!r}")
        self.array = array
        self.mpp = float(mpp)
        self.algorithm_version = algorithm_version
        self.profile = profile
        self.height_px, self.width_px = array.shape
        cells = array.astype(np.float64)
        # Cumulative tissue area over [0, x) x [0, y), in whole pixels along each edge.
        self._area = np.zeros((self.height_px + 1, self.width_px + 1))
        self._area[1:, 1:] = cells.cumsum(axis=0).cumsum(axis=1)
        # Column strips: tissue above row iy in column ix; row strips: tissue left of column ix in row iy.
        self._above = np.zeros((self.height_px + 1, self.width_px))
        self._above[1:] = cells.cumsum(axis=0)
        self._left = np.zeros((self.height_px, self.width_px + 1))
        self._left[:, 1:] = cells.cumsum(axis=1)
        self._cells = cells

    # -- geometry ----------------------------------------------------------------------

    @property
    def extent_um(self) -> tuple[float, float]:
        return self.width_px * self.mpp, self.height_px * self.mpp

    @property
    def area_mm2(self) -> float:
        return float(self.array.sum()) * self.mpp * self.mpp * 1e-6

    def _cumulative_area(self, x_px: np.ndarray, y_px: np.ndarray) -> np.ndarray:
        """Tissue area (px²) of [0, x) x [0, y), exact for fractional x and y."""
        x = np.clip(x_px, 0.0, self.width_px)
        y = np.clip(y_px, 0.0, self.height_px)
        ix = np.minimum(np.floor(x).astype(np.int64), self.width_px - 1)
        iy = np.minimum(np.floor(y).astype(np.int64), self.height_px - 1)
        ax, ay = x - ix, y - iy
        return (
            self._area[iy, ix]
            + ax * self._above[iy, ix]
            + ay * self._left[iy, ix]
            + ax * ay * self._cells[iy, ix]
        )

    def fractions_of_boxes_um(self, x0, y0, x1, y1) -> np.ndarray:
        """Tissue fraction of many boxes at once. A box's area counts in full, so a part off the slide is non-tissue."""
        x0, y0, x1, y1 = (np.asarray(v, dtype=np.float64) / self.mpp for v in (x0, y0, x1, y1))
        if np.any(x1 <= x0) or np.any(y1 <= y0):
            raise ValueError("every box needs x1 > x0 and y1 > y0")
        tissue = (
            self._cumulative_area(x1, y1) - self._cumulative_area(x0, y1)
            - self._cumulative_area(x1, y0) + self._cumulative_area(x0, y0)
        )
        return np.clip(tissue / ((x1 - x0) * (y1 - y0)), 0.0, 1.0)

    # -- queries -----------------------------------------------------------------------

    def contains_um(self, x_um: float, y_um: float) -> bool:
        column, row = math.floor(x_um / self.mpp), math.floor(y_um / self.mpp)
        if not (0 <= column < self.width_px and 0 <= row < self.height_px):
            return False
        return bool(self.array[row, column])

    def fraction_in_box_um(self, x0: float, y0: float, x1: float, y1: float) -> float:
        """Exact fraction of the box's area that is tissue (partial pixels weigh by the area covered)."""
        return float(self.fractions_of_boxes_um(x0, y0, x1, y1))

    def fraction_in_disk_um(self, cx: float, cy: float, r: float) -> float:
        """Fraction of the disk's area that is tissue. Pixels the edge crosses are sampled DISK_EDGE_SAMPLES² times."""
        if not r > 0:
            raise ValueError(f"r must be positive, got {r!r}")
        radius = r / self.mpp
        px, py = cx / self.mpp, cy / self.mpp
        col0, col1 = max(0, math.floor(px - radius)), min(self.width_px, math.ceil(px + radius))
        row0, row1 = max(0, math.floor(py - radius)), min(self.height_px, math.ceil(py + radius))
        if col0 >= col1 or row0 >= row1:
            return 0.0
        cols = np.arange(col0, col1)[None, :]
        rows = np.arange(row0, row1)[:, None]
        # Nearest and farthest points of each pixel square from the centre decide inside, outside or edge.
        near_x = np.clip(px, cols, cols + 1) - px
        near_y = np.clip(py, rows, rows + 1) - py
        far_x = np.maximum(np.abs(cols - px), np.abs(cols + 1 - px))
        far_y = np.maximum(np.abs(rows - py), np.abs(rows + 1 - py))
        inside = far_x**2 + far_y**2 <= radius**2
        outside = near_x**2 + near_y**2 >= radius**2
        weight = inside.astype(np.float64)
        edge_rows, edge_cols = np.nonzero(~inside & ~outside)
        if edge_rows.size:
            offsets = (np.arange(DISK_EDGE_SAMPLES) + 0.5) / DISK_EDGE_SAMPLES
            sub_x = (edge_cols[:, None, None] + col0 + offsets[None, None, :]) - px
            sub_y = (edge_rows[:, None, None] + row0 + offsets[None, :, None]) - py
            weight[edge_rows, edge_cols] = ((sub_x**2 + sub_y**2) <= radius**2).mean(axis=(1, 2))
        tissue = self.array[row0:row1, col0:col1]
        return float((weight * tissue).sum() / (math.pi * radius**2))

    def fractions_in_disks_um(self, cx, cy, r: float, chunk: int = 64) -> np.ndarray:
        """``fraction_in_disk_um`` of many disks at once (centres ``cx``, ``cy``, one radius ``r``): the same
        measurement, the same sub-pixel sampling of the pixels the edge crosses, evaluated for a chunk of
        centres together on a window of pixels around each. A part of the disk off the slide counts as non-tissue.
        """
        if not r > 0:
            raise ValueError(f"r must be positive, got {r!r}")
        cx = np.asarray(cx, dtype=np.float64).ravel()
        cy = np.asarray(cy, dtype=np.float64).ravel()
        radius = r / self.mpp
        reach = math.ceil(radius) + 1
        offsets = np.arange(-reach, reach + 1)
        padded = np.zeros((self.height_px + 2 * reach + 1, self.width_px + 2 * reach + 1))
        padded[reach:reach + self.height_px, reach:reach + self.width_px] = self._cells
        sub = (np.arange(DISK_EDGE_SAMPLES) + 0.5) / DISK_EDGE_SAMPLES
        out = np.empty(cx.shape)
        for k in range(0, len(cx), chunk):
            px, py = cx[k:k + chunk] / self.mpp, cy[k:k + chunk] / self.mpp
            ix, iy = np.floor(px).astype(np.int64), np.floor(py).astype(np.int64)
            # Pixel (iy + j, ix + i) spans [ix + i, ix + i + 1) x [iy + j, iy + j + 1); distances are from the centre.
            dx0 = (offsets[None, :] + (ix - px)[:, None])           # (n, W): left edge of each pixel column
            dy0 = (offsets[None, :] + (iy - py)[:, None])           # (n, W): top edge of each pixel row
            near_x = np.where(dx0 > 0, dx0, np.where(dx0 + 1 < 0, -(dx0 + 1), 0.0))
            near_y = np.where(dy0 > 0, dy0, np.where(dy0 + 1 < 0, -(dy0 + 1), 0.0))
            far_x = np.maximum(np.abs(dx0), np.abs(dx0 + 1))
            far_y = np.maximum(np.abs(dy0), np.abs(dy0 + 1))
            inside = (far_y[:, :, None] ** 2 + far_x[:, None, :] ** 2) <= radius ** 2
            outside = (near_y[:, :, None] ** 2 + near_x[:, None, :] ** 2) >= radius ** 2
            weight = inside.astype(np.float64)
            n_i, r_i, c_i = np.nonzero(~inside & ~outside)
            if n_i.size:
                sub_x = dx0[n_i, c_i][:, None, None] + sub[None, None, :]
                sub_y = dy0[n_i, r_i][:, None, None] + sub[None, :, None]
                weight[n_i, r_i, c_i] = ((sub_x ** 2 + sub_y ** 2) <= radius ** 2).mean(axis=(1, 2))
            rows = (iy[:, None] + offsets[None, :] + reach)[:, :, None]
            cols = (ix[:, None] + offsets[None, :] + reach)[:, None, :]
            tissue = padded[np.clip(rows, 0, padded.shape[0] - 1), np.clip(cols, 0, padded.shape[1] - 1)]
            out[k:k + chunk] = (weight * tissue).sum(axis=(1, 2)) / (math.pi * radius ** 2)
        return out

    def fraction_grid(
        self, cell_w_um: float, cell_h_um: float, n_cols: int, n_rows: int, origin_um: tuple[float, float] = (0.0, 0.0)
    ) -> np.ndarray:
        """Tissue fraction of every cell of a grid starting at ``origin_um``, shape (n_rows, n_cols).

        Each cell is exact (see ``fraction_in_box_um``); the grid may extend past the slide, and the
        part off the slide counts as non-tissue.
        """
        x0, y0 = np.meshgrid(origin_um[0] + np.arange(n_cols) * cell_w_um, origin_um[1] + np.arange(n_rows) * cell_h_um)
        return self.fractions_of_boxes_um(x0, y0, x0 + cell_w_um, y0 + cell_h_um)

    def at_mpp(self, mpp: float, width_px: int, height_px: int) -> np.ndarray:
        """The mask on another pixel grid over the same origin: True where at least half the pixel is tissue."""
        return self.fraction_grid(mpp, mpp, width_px, height_px) >= 0.5

    def tiles(self, tile_um: float, min_fraction: float) -> Iterator[TileRef]:
        """Whole tiles of a grid from the slide origin, in row-major order, with at least ``min_fraction`` tissue."""
        if not tile_um > 0:
            raise ValueError(f"tile_um must be positive, got {tile_um!r}")
        extent_w, extent_h = self.extent_um
        n_cols, n_rows = math.floor(extent_w / tile_um), math.floor(extent_h / tile_um)
        if n_cols < 1 or n_rows < 1:
            return
        cols, rows = np.meshgrid(np.arange(n_cols), np.arange(n_rows))
        x0, y0 = cols * tile_um, rows * tile_um
        fractions = self.fractions_of_boxes_um(x0, y0, x0 + tile_um, y0 + tile_um)
        for row, col in zip(*np.nonzero(fractions >= min_fraction)):
            yield TileRef(int(col), int(row), float(col * tile_um), float(row * tile_um), float(tile_um), float(fractions[row, col]))

    def sample_origins_um(self, n: int, size_um: float, rng: np.random.Generator) -> list[tuple[float, float]]:
        """``n`` origins of ``size_um`` squares centred on random tissue pixels, kept inside the slide.

        Positions are drawn uniformly over the tissue area, so the seeded generator alone decides them.
        """
        extent_w, extent_h = self.extent_um
        rows, cols = np.nonzero(self.array)
        if rows.size == 0:
            return []
        chosen = rng.choice(rows.size, size=n, replace=rows.size < n)
        centers_x = (cols[chosen] + 0.5) * self.mpp
        centers_y = (rows[chosen] + 0.5) * self.mpp
        xs = np.clip(centers_x - size_um / 2, 0.0, max(0.0, extent_w - size_um))
        ys = np.clip(centers_y - size_um / 2, 0.0, max(0.0, extent_h - size_um))
        return [(float(x), float(y)) for x, y in zip(xs, ys)]

    # -- persistence ---------------------------------------------------------------------

    def to_artifacts(self, params: dict | None = None) -> tuple[bytes, dict]:
        """(``tissue_mask.png`` bytes, ``tissue_mask.json`` content)."""
        buffer = io.BytesIO()
        Image.fromarray(self.array).save(buffer, format="PNG")  # a boolean array saves as a 1-bit image
        meta = {
            "mpp": self.mpp,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "origin_um": [0.0, 0.0],
            "algorithm_version": self.algorithm_version,
            "profile": self.profile,
            "params": params,
        }
        return buffer.getvalue(), meta

    @classmethod
    def from_artifacts(cls, png: bytes, meta: dict) -> "TissueMask":
        try:
            array = np.array(Image.open(io.BytesIO(png)).convert("L")) > 0
            mpp, width_px, height_px = float(meta["mpp"]), int(meta["width_px"]), int(meta["height_px"])
            origin = [float(v) for v in meta["origin_um"]]
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise TissueMaskError(f"tissue mask artifacts are unreadable: {exc!r}") from exc
        if origin != [0.0, 0.0]:
            raise TissueMaskError(f"a tissue mask must start at the slide origin, got {origin}")
        if array.shape != (height_px, width_px):
            raise TissueMaskError(f"tissue_mask.png is {array.shape[::-1]} px but tissue_mask.json says {(width_px, height_px)}")
        return cls(array, mpp, algorithm_version=meta["algorithm_version"], profile=meta.get("profile"))

    @classmethod
    def from_json_bytes(cls, png: bytes, meta_json: bytes) -> "TissueMask":
        try:
            meta = json.loads(meta_json.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise TissueMaskError(f"tissue_mask.json is not valid JSON: {exc}") from exc
        return cls.from_artifacts(png, meta)


# -- computation ----------------------------------------------------------------------------------


def otsu_threshold(histogram: np.ndarray) -> int:
    """The grey level t (0-254) that best separates a 256-bin histogram into ``<= t`` and ``> t`` (Otsu)."""
    counts = histogram.astype(np.float64)
    levels = np.arange(GRAY_LEVELS, dtype=np.float64)
    weight_low = np.cumsum(counts)
    weight_high = np.cumsum(counts[::-1])[::-1]
    mean_low = np.cumsum(counts * levels) / np.maximum(weight_low, 1.0)
    mean_high = (np.cumsum((counts * levels)[::-1]) / np.maximum(weight_high[::-1], 1.0))[::-1]
    between = weight_low[:-1] * weight_high[1:] * (mean_low[:-1] - mean_high[1:]) ** 2
    return int(np.argmax(between))


def _pen_mask(hsv: np.ndarray, ranges: PenHsvRanges) -> np.ndarray:
    pen = np.zeros(hsv.shape[:2], dtype=bool)
    for rng in ranges.model_dump().values():
        s_max = HSV_CHANNEL_MAX if rng["s_max"] is None else rng["s_max"]
        v_max = HSV_CHANNEL_MAX if rng["v_max"] is None else rng["v_max"]
        lower = np.array([rng["h_min"], rng["s_min"], rng["v_min"]], dtype=np.uint8)
        upper = np.array([rng["h_max"], s_max, v_max], dtype=np.uint8)
        pen |= cv2.inRange(hsv, lower, upper) > 0
    return pen


def _pen_marks(ink: np.ndarray, mpp: float, pen: PenMarksQC) -> np.ndarray:
    """Pen marks among pen-coloured pixels: components of at least ``pen.min_component_area_mm2``.

    This is QC's notion of a mark (after the same 3 x 3 opening). Blue-purple hematoxylin falls in the
    blue pen range, so colour alone must not remove tissue: only a large connected region is ink.
    """
    opened = cv2.morphologyEx(ink.astype(np.uint8), cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(opened, connectivity=8)
    large = np.zeros(n, dtype=bool)
    large[1:] = stats[1:, cv2.CC_STAT_AREA] * (mpp * MM_PER_UM) ** 2 >= pen.min_component_area_mm2
    return large[labels]


def _disk(radius_px: int) -> np.ndarray:
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius_px + 1, 2 * radius_px + 1))


def _clean_up(mask: np.ndarray, cfg: TissueMaskConfig) -> np.ndarray:
    """Open with a disk, drop small fragments, fill small holes (SPEC-04 §3.6)."""
    pixel_um2 = cfg.mpp * cfg.mpp
    radius_px = round(cfg.open_radius_um / cfg.mpp)
    image = mask.astype(np.uint8)
    if radius_px > 0:
        image = cv2.morphologyEx(image, cv2.MORPH_OPEN, _disk(radius_px))

    n, labels, stats, _ = cv2.connectedComponentsWithStats(image, connectivity=8)
    keep = np.zeros(n, dtype=bool)
    keep[1:] = stats[1:, cv2.CC_STAT_AREA] * pixel_um2 >= cfg.min_component_um2
    image = keep[labels].astype(np.uint8)

    # A hole is a background component that does not touch the image border.
    n, labels, stats, _ = cv2.connectedComponentsWithStats(1 - image, connectivity=4)
    height, width = image.shape
    fill = np.zeros(n, dtype=bool)
    for label in range(1, n):
        x, y, w, h, area = stats[label]
        touches_border = x == 0 or y == 0 or x + w == width or y + h == height
        fill[label] = not touches_border and area * pixel_um2 < cfg.fill_holes_max_um2
    return (image.astype(bool)) | fill[labels]


def compute_tissue_mask(reader: SlideReader, cfg: TissueMaskConfig, pen: PenMarksQC, profile: str) -> TissueMask:
    """The slide's tissue mask over its full extent at ``cfg.mpp``.

    The extent is read in strips at ``cfg.mpp`` (aspect preserved, no size cap). Tissue is
    ``gray <= clip(otsu(gray), *cfg.otsu_clip)`` or saturation above ``cfg.sat_min``, minus pen marks
    (``pen`` gives the ink colours and the size of a mark); then the clean-up. The strips are read
    once: grey, saturation and pen-coloured pixels are kept, not the RGB.
    """
    extent_w, extent_h = reader.extent_um()
    width_px, height_px = math.ceil(extent_w / cfg.mpp), math.ceil(extent_h / cfg.mpp)
    gray = np.empty((height_px, width_px), dtype=np.uint8)
    saturation = np.empty((height_px, width_px), dtype=np.uint8)
    ink = np.empty((height_px, width_px), dtype=bool)
    for row0, rgb in iter_extent_strips(reader, cfg.mpp):
        rows = rgb.shape[0]
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
        gray[row0 : row0 + rows] = rgb.mean(axis=2).astype(np.uint8)
        saturation[row0 : row0 + rows] = hsv[..., 1]
        ink[row0 : row0 + rows] = _pen_mask(hsv, pen.hsv_ranges)

    low, high = cfg.otsu_clip
    threshold = min(max(otsu_threshold(np.bincount(gray.ravel(), minlength=GRAY_LEVELS)), low), high)
    tissue = ((gray <= threshold) | (saturation > cfg.sat_min)) & ~_pen_marks(ink, cfg.mpp, pen)
    return TissueMask(_clean_up(tissue, cfg), cfg.mpp, profile=profile)
