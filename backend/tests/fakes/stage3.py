"""The Stage 3 artifact Stage 5 samples from: ``triage/tiles.parquet`` (SPEC-05 §4.3).

Written with the same columns and metadata as ``worker/triage.py::tiles_parquet``; only the columns
Stage 5 reads carry meaning (``x_um``, ``y_um``, ``is_tumor``).
"""
import io
import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from app.core.config import settings
from app.core.gcs import upload_blob_from_bytes
from pipeline.grading_sampling import TILES_FORMAT, TILES_METADATA_KEY

TILE_UM = 224.0


def tiles_parquet_bytes(tumor_ij, other_ij=(), tile_um: float = TILE_UM) -> bytes:
    """Tiles at grid cells ``(i, j)``: ``tumor_ij`` are invasive tumour, ``other_ij`` tissue only."""
    cells = [(i, j, True) for i, j in tumor_ij] + [(i, j, False) for i, j in other_ij]
    i = np.array([c[0] for c in cells], dtype=np.int32)
    j = np.array([c[1] for c in cells], dtype=np.int32)
    is_tumor = np.array([c[2] for c in cells], dtype=bool)
    table = pa.table({
        "i": pa.array(i, pa.int32()),
        "j": pa.array(j, pa.int32()),
        "x_um": pa.array(i * tile_um, pa.float32()),
        "y_um": pa.array(j * tile_um, pa.float32()),
        "tissue_fraction": pa.array(np.ones(len(cells)), pa.float32()),
        "p_tumor_cal": pa.array(np.where(is_tumor, 0.9, 0.1), pa.float32()),
        "is_tumor": pa.array(is_tumor, pa.bool_()),
    })
    meta = {"format": TILES_FORMAT, "classes": ["invasive_tumor", "stroma"], "tile_um": tile_um,
            "grid_version": "grid224_v1", "head_version": "tumor_head@test"}
    table = table.replace_schema_metadata({TILES_METADATA_KEY: json.dumps(meta).encode()})
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


def tiles_covering(x0_um: float, y0_um: float, x1_um: float, y1_um: float, tile_um: float = TILE_UM):
    """Grid cells whose tile overlaps the rectangle."""
    return [(i, j)
            for i in range(int(x0_um // tile_um), int(np.ceil(x1_um / tile_um)))
            for j in range(int(y0_um // tile_um), int(np.ceil(y1_um / tile_um)))]


def seed_tiles(case_id, tumor_ij, other_ij=()) -> None:
    upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{case_id}/triage/tiles.parquet",
                           tiles_parquet_bytes(tumor_ij, other_ij), "application/octet-stream")
