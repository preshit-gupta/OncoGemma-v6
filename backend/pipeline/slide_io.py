"""The single pixel entry point for whole-slide reads (SPEC-04 §3.1).

Every pixel a stage or a router reads from a slide goes through ``read_region_at_mpp``. A
caller states the physical region (µm) and the resolution it needs (µm/px); this module picks
the pyramid level, converts the colour to sRGB, resamples to exactly that resolution and says
in the returned ``Region`` what it did. Nothing else in the backend calls ``read_region``
(``tests/test_single_authority.py``).

``SlideReader`` keeps one OpenSlide handle per thread, so concurrent reads need no global lock.
The slide's resolution comes from the caller (the ``Slide`` row), never from file metadata.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

import numpy as np
import openslide
from PIL import Image, ImageCms

from pipeline.errors import IccProfileError, MissingMppError, RegionOutOfBoundsError, SlideReadError

Color = Literal["raw", "normalized"]

# Relative slack when comparing a level's resolution with the requested one: a level within it
# is used as is, so a 0.2529 µm/px scan serves a 0.25 µm/px request without a coarser read.
DEFAULT_MPP_TOLERANCE = 0.02

# Memory guard, not a clinical value: the most pixels one read may take from a level or produce.
# A request beyond it means no pyramid level is coarse enough for the resolution asked for.
MAX_READ_PIXELS = 2**28


class StainApplier(Protocol):
    """What ``read_region_at_mpp`` needs of a stain transform (``pipeline.stain.StainTransform``)."""

    profile_id: UUID | None

    def apply(self, rgb: np.ndarray) -> np.ndarray: ...


@dataclass(frozen=True)
class LevelInfo:
    index: int
    downsample: float
    mpp_x: float
    mpp_y: float
    width_px: int
    height_px: int

    @property
    def mpp(self) -> float:
        """The coarser axis, so a level counts as fine enough only when both axes are."""
        return max(self.mpp_x, self.mpp_y)


@dataclass(frozen=True, eq=False)
class Region:
    """A read region and how it was produced. It becomes the ``input_spec`` of a DecisionRecord."""

    rgb: np.ndarray  # uint8 HxWx3, sRGB
    target_mpp: float
    origin_um: tuple[float, float]
    size_um: tuple[float, float]
    native_level: int
    native_mpp: float
    upsampled: bool  # the level read is coarser than target_mpp * (1 + tolerance)
    icc_applied: bool
    color: Color
    stain_profile_id: UUID | None


def _finite_positive(value: float, name: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number, got {value!r}")
    return float(value)


def _round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


class SlideReader:
    """Thread-safe reader of one slide file. One OpenSlide handle per thread; no global lock.

    ``mpp_x`` and ``mpp_y`` are the level-0 resolution recorded on the ``Slide`` row. The file
    is opened at construction, so an unreadable file fails here and not on the first region.
    """

    def __init__(self, path: str, mpp_x: float, mpp_y: float, source_format: str | None):
        self.path = path
        self.mpp_x = _finite_positive(mpp_x, "mpp_x")
        self.mpp_y = _finite_positive(mpp_y, "mpp_y")
        self.source_format = source_format
        self._local = threading.local()
        self._handles: list = []
        self._lock = threading.Lock()
        self._closed = False

        first = self._handle()
        self._levels = self._describe_levels(first)
        self._icc_profile = getattr(first, "color_profile", None)
        # Opening the transform now fails a slide with an unusable profile before any region is read.
        self._icc_transform()

    @classmethod
    def from_slide_row(cls, path: str, slide) -> "SlideReader":
        """A reader with the resolution recorded on the ``Slide`` row; a slide without one cannot be read."""
        mpp_x, mpp_y = getattr(slide, "mpp_x", None), getattr(slide, "mpp_y", None)
        if not mpp_x or mpp_x <= 0 or not mpp_y or mpp_y <= 0:
            raise MissingMppError(
                f"Slide {getattr(slide, 'id', '?')} is missing valid MPP (status='needs_mpp'); it cannot be read."
            )
        return cls(path, float(mpp_x), float(mpp_y), getattr(slide, "format", None))

    # -- handles ---------------------------------------------------------------------

    def _handle(self):
        handle = getattr(self._local, "handle", None)
        if handle is not None:
            return handle
        if self._closed:
            raise SlideReadError(f"the reader for {self.path} is closed")
        try:
            handle = openslide.OpenSlide(self.path)
        except (openslide.OpenSlideError, OSError) as exc:
            raise SlideReadError(f"could not open slide {self.path}: {exc}") from exc
        self._local.handle = handle
        with self._lock:
            if not any(handle is known for known in self._handles):
                self._handles.append(handle)
        return handle

    def close(self) -> None:
        """Close the handles of every thread that read from this slide."""
        with self._lock:
            self._closed = True
            handles, self._handles = self._handles, []
        for handle in handles:
            handle.close()
        self._local = threading.local()

    def __enter__(self) -> "SlideReader":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- geometry --------------------------------------------------------------------

    def _describe_levels(self, handle) -> list[LevelInfo]:
        levels = []
        for index in range(handle.level_count):
            downsample = float(handle.level_downsamples[index])
            width_px, height_px = handle.level_dimensions[index]
            if downsample < 1 or width_px < 1 or height_px < 1:
                raise SlideReadError(f"{self.path}: level {index} has downsample {downsample} and size {width_px}x{height_px}")
            levels.append(
                LevelInfo(index, downsample, self.mpp_x * downsample, self.mpp_y * downsample, int(width_px), int(height_px))
            )
        if not levels or levels[0].downsample != 1:
            raise SlideReadError(f"{self.path}: level 0 must have downsample 1")
        if any(fine.downsample >= coarse.downsample for fine, coarse in zip(levels, levels[1:])):
            raise SlideReadError(f"{self.path}: level downsamples must increase, got {[lv.downsample for lv in levels]}")
        return levels

    @property
    def levels(self) -> list[LevelInfo]:
        return list(self._levels)

    @property
    def dimensions(self) -> tuple[int, int]:
        """Level-0 width and height in pixels."""
        return self._levels[0].width_px, self._levels[0].height_px

    @property
    def native_mpp(self) -> float:
        """The finest resolution the slide holds (level 0), on its coarser axis."""
        return self._levels[0].mpp

    def extent_um(self) -> tuple[float, float]:
        width_px, height_px = self.dimensions
        return width_px * self.mpp_x, height_px * self.mpp_y

    def choose_level(self, target_mpp: float, tolerance: float = DEFAULT_MPP_TOLERANCE) -> LevelInfo:
        """The coarsest level still at least as fine as ``target_mpp`` (within ``tolerance``).

        It reads the fewest pixels without upsampling. When even level 0 is coarser than the
        target, level 0 is returned and the region is upsampled.
        """
        limit = target_mpp * (1 + tolerance)
        chosen = self._levels[0]
        for level in self._levels:
            if level.mpp <= limit:
                chosen = level
        return chosen

    # -- colour ----------------------------------------------------------------------

    def _icc_transform(self):
        """The embedded profile to sRGB, built once per thread (None when the slide has no profile)."""
        if self._icc_profile is None:
            return None
        transform = getattr(self._local, "icc_transform", None)
        if transform is None:
            try:
                transform = ImageCms.buildTransform(
                    self._icc_profile, ImageCms.createProfile("sRGB"), "RGB", "RGB"
                )
            except ImageCms.PyCMSError as exc:
                raise IccProfileError(f"{self.path}: the embedded ICC profile cannot be converted to sRGB: {exc}") from exc
            self._local.icc_transform = transform
        return transform

    # -- pixels ----------------------------------------------------------------------

    def _read_level(self, location: tuple[int, int], level: int, size: tuple[int, int]) -> Image.Image:
        try:
            return self._handle().read_region(location, level, size)
        except openslide.OpenSlideError as exc:
            raise SlideReadError(f"could not read {size} px at {location} (level {level}) of {self.path}: {exc}") from exc


def clamp_origin_um(reader: SlideReader, x_um: float, y_um: float, w_um: float, h_um: float) -> tuple[float, float]:
    """Shift a region's origin so the region lies inside the slide, as far as it fits."""
    extent_w, extent_h = reader.extent_um()
    return max(0.0, min(extent_w - w_um, x_um)), max(0.0, min(extent_h - h_um, y_um))


def centered_origin_um(reader: SlideReader, cx_um: float, cy_um: float, w_um: float, h_um: float) -> tuple[float, float]:
    """The origin of a ``w_um`` x ``h_um`` region centred on a point, shifted inside the slide."""
    return clamp_origin_um(reader, cx_um - w_um / 2, cy_um - h_um / 2, w_um, h_um)


def read_region_at_mpp(
    reader: SlideReader,
    x_um: float,
    y_um: float,
    w_um: float,
    h_um: float,
    target_mpp: float,
    *,
    color: Color = "raw",
    stain: StainApplier | None = None,
    mpp_tolerance: float = DEFAULT_MPP_TOLERANCE,
) -> Region:
    """Read the region with its top-left corner at (``x_um``, ``y_um``) as sRGB at ``target_mpp``.

    1. The coarsest level at least as fine as the target is read; a coarser level is never
       upsampled unless level 0 itself is coarser (then ``upsampled`` is set).
    2. The minimal pixel box covering the request is read from that level.
    3. Transparent pixels are composited on white and the embedded ICC profile is applied.
    4. The box is resampled to ``round(w_um / target_mpp)`` x ``round(h_um / target_mpp)`` with
       the request's exact sub-pixel edges: LANCZOS when shrinking, BICUBIC when enlarging.
    5. ``color="normalized"`` applies the persisted stain transform, and needs one.

    A region overhanging the slide edge is padded with white. One entirely outside raises.
    """
    for name, value in (("x_um", x_um), ("y_um", y_um)):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite, got {value!r}")
    _finite_positive(w_um, "w_um")
    _finite_positive(h_um, "h_um")
    _finite_positive(target_mpp, "target_mpp")
    if not math.isfinite(mpp_tolerance) or mpp_tolerance < 0:
        raise ValueError(f"mpp_tolerance must be a finite number >= 0, got {mpp_tolerance!r}")
    if color not in ("raw", "normalized"):
        raise ValueError(f"color must be 'raw' or 'normalized', got {color!r}")
    if color == "normalized" and stain is None:
        raise ValueError("color='normalized' needs the slide's persisted stain transform")
    if color == "raw" and stain is not None:
        raise ValueError("a stain transform was given for color='raw'; pass color='normalized' to apply it")

    out_w, out_h = _round_half_up(w_um / target_mpp), _round_half_up(h_um / target_mpp)
    if out_w < 1 or out_h < 1:
        raise ValueError(f"a {w_um} x {h_um} µm region is smaller than one pixel at {target_mpp} µm/px")
    if out_w * out_h > MAX_READ_PIXELS:
        raise SlideReadError(f"a {out_w}x{out_h} px output at {target_mpp} µm/px exceeds {MAX_READ_PIXELS} px")

    level = reader.choose_level(target_mpp, mpp_tolerance)

    # The request in level-0 pixels (float), then the exact box inside the level image that we read.
    x0_l0, y0_l0 = x_um / reader.mpp_x, y_um / reader.mpp_y
    fx0, fy0 = x0_l0 / level.downsample, y0_l0 / level.downsample
    fx1 = (x_um + w_um) / level.mpp_x
    fy1 = (y_um + h_um) / level.mpp_y
    if fx1 <= 0 or fy1 <= 0 or fx0 >= level.width_px or fy0 >= level.height_px:
        raise RegionOutOfBoundsError(
            f"region ({x_um}, {y_um}) {w_um}x{h_um} µm lies outside the {level.width_px}x{level.height_px} px slide"
        )

    # The read starts on a whole level-0 pixel at or before the request, so the box offset is >= 0.
    loc_x, loc_y = math.floor(x0_l0), math.floor(y0_l0)
    origin_x, origin_y = loc_x / level.downsample, loc_y / level.downsample
    box = (fx0 - origin_x, fy0 - origin_y, fx1 - origin_x, fy1 - origin_y)
    read_w, read_h = max(1, math.ceil(box[2])), max(1, math.ceil(box[3]))
    if read_w * read_h > MAX_READ_PIXELS:
        raise SlideReadError(
            f"reading {w_um}x{h_um} µm at {target_mpp} µm/px needs a {read_w}x{read_h} px read from level {level.index} "
            f"({level.mpp:.4g} µm/px), over the {MAX_READ_PIXELS} px limit; the slide has no coarser level"
        )

    image = reader._read_level((loc_x, loc_y), level.index, (read_w, read_h))
    if image.mode == "RGBA":
        image = Image.alpha_composite(Image.new("RGBA", image.size, (255, 255, 255, 255)), image).convert("RGB")
    elif image.mode != "RGB":
        image = image.convert("RGB")

    icc_applied = False
    transform = reader._icc_transform()
    if transform is not None:
        try:
            image = ImageCms.applyTransform(image, transform)
        except ImageCms.PyCMSError as exc:
            raise IccProfileError(f"{reader.path}: applying the ICC profile failed: {exc}") from exc
        icc_applied = True

    box_w, box_h = box[2] - box[0], box[3] - box[1]
    resample = Image.Resampling.LANCZOS if (box_w >= out_w and box_h >= out_h) else Image.Resampling.BICUBIC
    image = image.resize((out_w, out_h), resample, box=(box[0], box[1], min(box[2], read_w), min(box[3], read_h)))
    rgb = np.array(image, dtype=np.uint8)

    stain_profile_id = None
    if color == "normalized":
        rgb = stain.apply(rgb)
        stain_profile_id = stain.profile_id

    return Region(
        rgb=rgb,
        target_mpp=target_mpp,
        origin_um=(float(x_um), float(y_um)),
        size_um=(float(w_um), float(h_um)),
        native_level=level.index,
        native_mpp=level.mpp,
        upsampled=level.mpp > target_mpp * (1 + mpp_tolerance),
        icc_applied=icc_applied,
        color=color,
        stain_profile_id=stain_profile_id,
    )
