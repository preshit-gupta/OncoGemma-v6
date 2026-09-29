"""Synthetic H&E pixels with known stain vectors and concentrations (Beer-Lambert).

Optical density is ``W^T C`` for unit stain vectors ``W`` (hematoxylin first) and concentrations
``C``, so the stain estimator and the stain transform can be checked against exact answers.
"""
import numpy as np

H_VECTOR = np.array([0.65, 0.70, 0.29]) / np.linalg.norm([0.65, 0.70, 0.29])
E_VECTOR = np.array([0.07, 0.99, 0.11]) / np.linalg.norm([0.07, 0.99, 0.11])
W_HE = np.vstack([H_VECTOR, E_VECTOR])
GLASS = 242


def rgb_from_concentrations(conc: np.ndarray, w: np.ndarray = W_HE) -> np.ndarray:
    """uint8 RGB for concentrations of shape (..., 2)."""
    od = conc @ w  # (..., 3)
    return np.clip(np.rint(255.0 * 10.0 ** (-od)), 0, 255).astype(np.uint8)


def he_pixels(n: int, seed: int, w: np.ndarray = W_HE, max_h: float = 1.2, max_e: float = 0.9) -> np.ndarray:
    """(n, 3) pixels: a tenth nearly pure hematoxylin, a tenth nearly pure eosin, the rest mixed."""
    rng = np.random.default_rng(seed)
    conc = rng.uniform(0.05, 1.0, size=(n, 2)) * np.array([max_h, max_e])
    tenth = n // 10
    conc[:tenth, 1] = 0.01
    conc[tenth : 2 * tenth, 0] = 0.01
    return rgb_from_concentrations(conc, w)


def he_picture(w: np.ndarray = W_HE, block_px: int = 8, tissue_fraction: float = 0.9, eosin_only: bool = False):
    """A ``picture`` for FakeOpenSlide: blocks of random H&E concentrations on glass, in level-0 coordinates."""

    def picture(xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        bx, by = xs.astype(np.int64) // block_px, ys.astype(np.int64) // block_px
        hashed = (bx * 73856093) ^ (by * 19349663)
        u = (hashed % 1000) / 1000.0
        v = ((hashed // 1000) % 1000) / 1000.0
        conc = np.stack([0.05 + 1.15 * u, 0.05 + 0.85 * v], axis=-1)
        if eosin_only:
            conc[..., 0] = 0.0
        rgb = rgb_from_concentrations(conc, w)
        glass = ((hashed // 1_000_000) % 100) >= int(tissue_fraction * 100)
        rgb[glass] = GLASS
        return rgb

    return picture


def he_slide_rgb(width_px: int, height_px: int, block_px: int = 8, margin: float = 0.06, seed: int = 0) -> np.ndarray:
    """A whole H&E section on glass: blocks of random concentrations inside a margin of empty glass."""
    rng = np.random.default_rng(seed)
    rows, cols = -(-height_px // block_px), -(-width_px // block_px)
    conc = np.stack([0.05 + 1.15 * rng.random((rows, cols)), 0.05 + 0.85 * rng.random((rows, cols))], axis=-1)
    blocks = rgb_from_concentrations(conc)
    rgb = np.repeat(np.repeat(blocks, block_px, axis=0), block_px, axis=1)[:height_px, :width_px].copy()
    m_x, m_y = int(width_px * margin), int(height_px * margin)
    rgb[:m_y] = GLASS
    rgb[-m_y:] = GLASS
    rgb[:, :m_x] = GLASS
    rgb[:, -m_x:] = GLASS
    return rgb
