"""A procedural stand-in for ``openslide.OpenSlide``.

Pixels are a function of level-0 coordinates, so regions at every level, crops and the thumbnail
agree: an elliptical pink tissue section with purple nuclei on white glass. ``downsamples`` gives
it a pyramid, and ``picture`` replaces the tissue with any function of level-0 coordinates (a
slide of known tissue squares, say) without materialising its pixels.
"""
import numpy as np
from PIL import Image

from app.core import gcs

GLASS = (242, 242, 242)
STROMA = (225, 150, 185)
NUCLEUS = (75, 35, 120)


class FakeOpenSlide:
    def __init__(
        self,
        width_px: int,
        height_px: int,
        nucleus_pitch_px: int = 40,
        *,
        downsamples=(1,),
        color_profile=None,
        picture=None,
    ):
        self.dimensions = (width_px, height_px)
        self.pitch = nucleus_pitch_px
        self.level_downsamples = tuple(float(d) for d in downsamples)
        self.level_dimensions = tuple(
            (int(np.ceil(width_px / d)), int(np.ceil(height_px / d))) for d in self.level_downsamples
        )
        self.level_count = len(self.level_downsamples)
        self.properties: dict = {}
        self.color_profile = color_profile
        self._picture = picture if picture is not None else self._pixels
        self.closed = False
        self.regions_read: list[tuple[tuple[int, int], tuple[int, int]]] = []
        self.levels_read: list[int] = []

    def _pixels(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        """RGB for level-0 coordinate grids ``xs``, ``ys``."""
        width, height = self.dimensions
        in_tissue = ((xs - width / 2) / (0.42 * width)) ** 2 + ((ys - height / 2) / (0.4 * height)) ** 2 <= 1
        nucleus = ((xs % self.pitch) < self.pitch // 4) & ((ys % self.pitch) < self.pitch // 4)
        rgb = np.empty(xs.shape + (3,), dtype=np.uint8)
        rgb[:] = GLASS
        rgb[in_tissue] = STROMA
        rgb[in_tissue & nucleus] = NUCLEUS
        # A position hash keeps regions distinct: a periodic slide would make every tile and
        # crop identical, and the gateway cache would answer them all from the first call.
        texture = ((xs.astype(np.int64) * 73856093) ^ (ys.astype(np.int64) * 19349663)) % 9
        rgb[in_tissue] -= texture[in_tissue].astype(np.uint8)[:, None]
        return rgb

    def get_best_level_for_downsample(self, downsample: float) -> int:
        """OpenSlide's rule: the coarsest level that is not coarser than ``downsample``."""
        best = 0
        for index, level_downsample in enumerate(self.level_downsamples):
            if level_downsample <= downsample:
                best = index
        return best

    def read_region(self, location, level, size):
        """RGBA at ``level``; pixels sample level 0 at their centres, and pixels off the slide are transparent."""
        downsample = self.level_downsamples[level]
        self.regions_read.append((tuple(location), tuple(size)))
        self.levels_read.append(level)
        x0, y0 = location
        w, h = size
        xs = np.floor(x0 + (np.arange(w) + 0.5) * downsample).astype(np.int64)
        ys = np.floor(y0 + (np.arange(h) + 0.5) * downsample).astype(np.int64)
        grid_x, grid_y = np.meshgrid(xs, ys)
        width, height = self.dimensions
        on_slide = (grid_x >= 0) & (grid_x < width) & (grid_y >= 0) & (grid_y < height)
        rgba = np.dstack([self._picture(grid_x, grid_y), np.where(on_slide, 255, 0).astype(np.uint8)])
        return Image.fromarray(rgba, mode="RGBA")

    def get_thumbnail(self, size):
        tw, th = size
        width, height = self.dimensions
        xs = (np.arange(tw) + 0.5) * width / tw
        ys = (np.arange(th) + 0.5) * height / th
        grid_x, grid_y = np.meshgrid(xs, ys)
        return Image.fromarray(self._picture(grid_x, grid_y), mode="RGB")

    def close(self):
        self.closed = True


def install_fake_slide(monkeypatch, slide: FakeOpenSlide, raw_uri: str) -> FakeOpenSlide:
    """Put a placeholder file at ``raw_uri`` in the local GCS mock and open it as ``slide``."""
    import openslide

    bucket, blob = gcs.parse_gcs_uri(raw_uri)
    gcs.upload_blob_from_bytes(bucket, blob, b"fake slide file", "application/octet-stream")
    monkeypatch.setattr(openslide, "OpenSlide", lambda path: slide)
    return slide
