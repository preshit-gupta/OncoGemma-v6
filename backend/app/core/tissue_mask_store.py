"""Persist and load a case's registered tissue mask (SPEC-04 §3.6).

Stage 2 writes ``tissue_mask.png`` and ``tissue_mask.json`` next to its other outputs; every later
stage and router loads them here. A case preprocessed before v6 has only the old PNG, which has no
registration metadata, so it is treated as having no mask and must be preprocessed again.
"""
import json

from app.core.config import settings
from app.core.gcs import blob_exists, download_blob_as_bytes, upload_blob_from_bytes
from pipeline.errors import TissueMaskMissingError
from pipeline.tissue_mask import TissueMask


def mask_blob_names(case_id) -> tuple[str, str]:
    prefix = f"cases/{case_id}/preprocess"
    return f"{prefix}/tissue_mask.png", f"{prefix}/tissue_mask.json"


def save_tissue_mask(case_id, mask: TissueMask, params: dict | None = None) -> tuple[str, str]:
    """Upload the mask's PNG and JSON; returns their gs:// URIs."""
    png_name, meta_name = mask_blob_names(case_id)
    png, meta = mask.to_artifacts(params)
    upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, png_name, png, "image/png")
    upload_blob_from_bytes(
        settings.GCS_ARTIFACTS_BUCKET, meta_name, json.dumps(meta, indent=2).encode("utf-8"), "application/json"
    )
    return f"gs://{settings.GCS_ARTIFACTS_BUCKET}/{png_name}", f"gs://{settings.GCS_ARTIFACTS_BUCKET}/{meta_name}"


def load_tissue_mask(case_id) -> TissueMask:
    """The case's registered mask, or TissueMaskMissingError when Stage 2 has not produced one."""
    png_name, meta_name = mask_blob_names(case_id)
    for name in (png_name, meta_name):
        if not blob_exists(settings.GCS_ARTIFACTS_BUCKET, name):
            raise TissueMaskMissingError(
                f"case {case_id} has no registered tissue mask (gs://{settings.GCS_ARTIFACTS_BUCKET}/{name} is missing); "
                "run the preprocess stage for it"
            )
    return TissueMask.from_json_bytes(
        download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, png_name),
        download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, meta_name),
    )
