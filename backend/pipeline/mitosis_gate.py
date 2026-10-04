"""
Stage 4 tumour-cell gate (SPEC-06 §5.5) on the SPEC-05 tumour mask that triage persists.

Triage writes ``cases/<case>/triage/tumor_mask.png`` (one pixel per grid tile, 255 = tumour: the
calibrated tumour probability at or above ``tumor_head.threshold``, after the head's smoothing) and
``tumor_mask.json`` (``tile_um``, ``origin_um``, ``nx``, ``ny``, ``head_version``, ``threshold``).
The gate reads that mask as it is; it never thresholds probabilities again.

- A candidate is in tumour when its tile, in the mask dilated by ``dilation_tiles`` tiles, is tumour.
- SPEC-06 §5.5 also excludes in-situ mitoses (``argmax != in_situ``). ``tumor_head@1.0.0`` has no
  in-situ class (BCSS had one DCIS tile), so that clause cannot exclude anything yet; the stage
  output says so (``TumorGate.summary``).
- HPF tumour fractions (SPEC-06 §5.8) are area fractions of the undilated mask.
"""
import io
import json
from dataclasses import dataclass

import numpy as np
from PIL import Image
from scipy.ndimage import binary_dilation

from app.core.config import settings
from app.core.gcs import blob_exists, download_blob_as_bytes
from pipeline.errors import TissueMaskError
from pipeline.tissue_mask import TissueMask


class TumorMaskMissingError(TissueMaskError):
    """The case has no triage tumour mask (triage ran before WP-6.2, or has not run)."""


# worker/triage.py writes tumour tiles as 255 and the rest as 0.
MASK_ON_LEVEL = 128
# Sample points per disk radius for tumour fractions (a 262 µm HPF: ~4 µm apart, about 13,500 points).
DISK_SAMPLES_PER_RADIUS = 64


def tumor_mask_blob_names(case_id) -> tuple[str, str]:
    prefix = f"cases/{case_id}/triage"
    return f"{prefix}/tumor_mask.png", f"{prefix}/tumor_mask.json"


@dataclass(frozen=True)
class TumorGate:
    """The tumour mask on the triage tile grid, and the same mask dilated for the candidate gate."""

    mask: TissueMask          # one pixel per tile, True = tumour
    dilated: TissueMask       # ``mask`` dilated by ``dilation_tiles`` (8-connected)
    dilation_tiles: int
    head_version: str

    @classmethod
    def from_artifacts(cls, png: bytes, meta: dict, dilation_tiles: int) -> "TumorGate":
        if dilation_tiles < 0:
            raise ValueError(f"dilation_tiles must be >= 0, got {dilation_tiles}")
        if [float(v) for v in meta["origin_um"]] != [0.0, 0.0]:
            raise TissueMaskError(f"the tumour mask grid must start at the slide origin, got origin_um {meta['origin_um']}")
        array = np.asarray(Image.open(io.BytesIO(png)).convert("L")) >= MASK_ON_LEVEL
        if array.shape != (int(meta["ny"]), int(meta["nx"])):
            raise TissueMaskError(f"tumour mask is {array.shape[1]}x{array.shape[0]} tiles but its metadata says {meta['nx']}x{meta['ny']}")
        tile_um = float(meta["tile_um"])
        structure = np.ones((3, 3), dtype=bool)
        dilated = binary_dilation(array, structure=structure, iterations=dilation_tiles) if dilation_tiles else array
        return cls(TissueMask(array, tile_um), TissueMask(dilated, tile_um), dilation_tiles, str(meta["head_version"]))

    def in_tumor(self, x_um: float, y_um: float) -> bool:
        """The gate (SPEC-06 §5.5): the candidate's tile is tumour in the dilated mask (False off the grid)."""
        return self.dilated.contains_um(x_um, y_um)

    def tumor_fraction_in_disk(self, cx_um: float, cy_um: float, r_um: float) -> float:
        """Area fraction of the disk that is tumour (undilated mask), from a lattice of DISK_SAMPLES_PER_RADIUS
        points per radius. A disk wholly in tumour (or stroma) is exactly 1 (or 0); a tile one pixel per 224 µm
        is too coarse for TissueMask's edge sampling. Points off the grid are not tumour."""
        if not r_um > 0:
            raise ValueError(f"r must be positive, got {r_um!r}")
        offsets = (np.arange(-DISK_SAMPLES_PER_RADIUS, DISK_SAMPLES_PER_RADIUS) + 0.5) * (r_um / DISK_SAMPLES_PER_RADIUS)
        dx, dy = np.meshgrid(offsets, offsets)
        inside = dx**2 + dy**2 <= r_um**2
        cols = np.floor((cx_um + dx[inside]) / self.mask.mpp).astype(np.int64)
        rows = np.floor((cy_um + dy[inside]) / self.mask.mpp).astype(np.int64)
        on_grid = (cols >= 0) & (cols < self.mask.width_px) & (rows >= 0) & (rows < self.mask.height_px)
        tumour = np.zeros(cols.shape, dtype=bool)
        tumour[on_grid] = self.mask.array[rows[on_grid], cols[on_grid]]
        return float(tumour.mean())

    def summary(self) -> dict:
        """What the gate did, for the stage output."""
        return {
            "applied": True,
            "dilation_tiles": self.dilation_tiles,
            "tile_um": self.mask.mpp,
            "head_version": self.head_version,
            "in_situ_exclusion": False,
            "in_situ_exclusion_reason": f"{self.head_version} has no in-situ class",
        }


def load_tumor_gate(case_id, dilation_tiles: int) -> TumorGate:
    """The case's tumour gate, or TumorMaskMissingError when triage has not written a tumour mask."""
    png_name, meta_name = tumor_mask_blob_names(case_id)
    for name in (png_name, meta_name):
        if not blob_exists(settings.GCS_ARTIFACTS_BUCKET, name):
            raise TumorMaskMissingError(
                f"case {case_id} has no tumour mask (gs://{settings.GCS_ARTIFACTS_BUCKET}/{name} is missing); "
                "run the triage stage for it again"
            )
    meta = json.loads(download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, meta_name).decode("utf-8"))
    return TumorGate.from_artifacts(download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, png_name), meta, dilation_tiles)
