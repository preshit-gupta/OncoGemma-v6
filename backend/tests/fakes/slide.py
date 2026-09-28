"""A procedural stand-in for ``openslide.OpenSlide``.

Pixels are a function of level-0 coordinates, so regions, crops and the thumbnail
agree: an elliptical pink tissue section with purple nuclei on white glass.
"""
import numpy as np
from PIL import Image

from app.core import gcs

GLASS = (242, 242, 242)
STROMA = (225, 150, 185)
NUCLEUS = (75, 35, 120)


class FakeOpenSlide:
    def __init__(self, width_px: int, height_px: int, nucleus_pitch_px: int = 40):
        self.dimensions = (width_px, height_px)
        self.pitch = nucleus_pitch_px
        self.closed = False
        self.regions_read: list[tuple[tuple[int, int], tuple[int, int]]] = []

    def _pixels(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        """RGB for level-0 coordinate grids ``xs``, ``ys``."""
        width, height = self.dimensions
        in_tissue = ((xs - width / 2) / (0.42 * width)) ** 2 + ((ys - height / 2) / (0.4 * height)) ** 2 <= 1
        nucleus = ((xs % self.pitch) < self.pitch // 4) & ((ys % self.pitch) < self.pitch // 4)
        rgb = np.empty(xs.shape + (3,), dtype=np.uint8)
        rgb[:] = GLASS
        rgb[in_tissue] = STROMA
        rgb[in_tissue & nucleus] = NUCLEUS
        return rgb

    def read_region(self, location, level, size):
        assert level == 0
        self.regions_read.append((tuple(location), tuple(size)))
        x0, y0 = location
        w, h = size
        ys, xs = np.mgrid[y0:y0 + h, x0:x0 + w]
        rgba = np.dstack([self._pixels(xs, ys), np.full((h, w), 255, dtype=np.uint8)])
        return Image.fromarray(rgba, mode="RGBA")

    def get_thumbnail(self, size):
        tw, th = size
        width, height = self.dimensions
        xs = (np.arange(tw) + 0.5) * width / tw
        ys = (np.arange(th) + 0.5) * height / th
        grid_x, grid_y = np.meshgrid(xs, ys)
        return Image.fromarray(self._pixels(grid_x, grid_y), mode="RGB")

    def close(self):
        self.closed = True


def install_fake_slide(monkeypatch, slide: FakeOpenSlide, raw_uri: str) -> FakeOpenSlide:
    """Put a placeholder file at ``raw_uri`` in the local GCS mock and open it as ``slide``."""
    import openslide

    bucket, blob = gcs.parse_gcs_uri(raw_uri)
    gcs.upload_blob_from_bytes(bucket, blob, b"fake slide file", "application/octet-stream")
    monkeypatch.setattr(openslide, "OpenSlide", lambda path: slide)
    return slide
