"""Real pyramidal TIFF slides for tests that need the OpenSlide library itself.

The picture is a function of physical position (µm), so a 40x scan and its 2x-downsampled 20x
derivative show the same tissue. Hard-edged dark squares give tests landmarks whose µm positions
are known.
"""
from pathlib import Path

import numpy as np
import tifffile

GLASS = 242
PINK = (225, 150, 185)
DARK = (75, 35, 120)


def tissue_rgb(width_px: int, height_px: int, mpp: float, squares_um=()) -> np.ndarray:
    """A pink section on white glass with dark squares ``(x_um, y_um, side_um)`` and a smooth texture."""
    xs = (np.arange(width_px) + 0.5) * mpp
    ys = (np.arange(height_px) + 0.5) * mpp
    x_um, y_um = np.meshgrid(xs, ys)
    rgb = np.empty((height_px, width_px, 3), dtype=np.float64)
    rgb[:] = GLASS
    inside = (x_um > 0.05 * width_px * mpp) & (x_um < 0.95 * width_px * mpp) & (y_um > 0.05 * height_px * mpp) & (y_um < 0.95 * height_px * mpp)
    rgb[inside] = PINK
    rgb[..., 0] += inside * 12 * np.sin(x_um / 37.0)
    rgb[..., 1] += inside * 18 * np.cos(y_um / 29.0)
    for sx, sy, side in squares_um:
        square = (x_um >= sx) & (x_um < sx + side) & (y_um >= sy) & (y_um < sy + side)
        rgb[square] = DARK
    return np.clip(np.round(rgb), 0, 255).astype(np.uint8)


def halve(rgb: np.ndarray) -> np.ndarray:
    """2x2 mean downsample, the pyramid level a scanner would store for half the resolution."""
    height, width = rgb.shape[0] // 2 * 2, rgb.shape[1] // 2 * 2
    blocks = rgb[:height, :width].reshape(height // 2, 2, width // 2, 2, 3)
    return np.round(blocks.mean(axis=(1, 3))).astype(np.uint8)


def write_pyramid_tiff(path: Path, base: np.ndarray, mpp: float, levels: int = 4) -> Path:
    """Write ``base`` (level 0, ``mpp`` µm/px) and ``levels - 1`` halvings as a tiled pyramidal TIFF."""
    pages = [base]
    for _ in range(levels - 1):
        pages.append(halve(pages[-1]))
    pixels_per_cm = 1e4 / mpp
    with tifffile.TiffWriter(str(path)) as writer:
        for index, page in enumerate(pages):
            scale = 2**index
            writer.write(
                page,
                tile=(128, 128),
                photometric="rgb",
                compression="deflate",
                resolution=(pixels_per_cm / scale, pixels_per_cm / scale),
                resolutionunit="CENTIMETER",
                subfiletype=0 if index == 0 else 1,
            )
    return path
