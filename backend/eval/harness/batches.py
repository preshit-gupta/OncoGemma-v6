"""Batches in the application (SPEC-02 §6.1): a batch **is** a validation run.

From a manifest, the batch evaluates one split of it (``create_run``, with the same test lock
as the CLI). From a GCS prefix, every slide object under the prefix becomes one item of an
ad-hoc run; the generated manifest is stored next to the run's other artifacts. Workers drive
batches exactly as they drive CLI runs (``eval/harness/driver.py``).
"""
from __future__ import annotations

import io
import uuid
from collections import Counter
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.gcs import get_bucket, parse_gcs_uri, upload_blob_from_bytes
from app.models.validation import ValidationItem, ValidationRun
from eval.datasets.manifest import MANIFEST_COLUMNS
from eval.harness.runs import ADHOC, RunConfigError, RunRequest, create_adhoc_run, create_run

SLIDE_EXTENSIONS = (".svs", ".ndpi", ".tif", ".tiff", ".mrxs")
JPEG_EXTENSIONS = (".jpg", ".jpeg")


class UnsupportedSlideFormat(RunConfigError):
    """JPEG sources need the BCNB conversion (SPEC-02 §3.2), which the app does not run yet."""


def manifest_from_prefix(gcs_prefix: str, specimen_type: str, mpp_override: float | None) -> pd.DataFrame:
    """One ad-hoc manifest row per slide object under ``gcs_prefix``; no SHA-256 (ingest records it)."""
    bucket_name, prefix = parse_gcs_uri(gcs_prefix)
    prefix = prefix.strip("/")
    names = sorted(b.name for b in get_bucket(bucket_name).list_blobs(prefix=f"{prefix}/" if prefix else ""))
    jpegs = [n for n in names if n.lower().endswith(JPEG_EXTENSIONS)]
    if jpegs:
        raise UnsupportedSlideFormat(
            f"{len(jpegs)} JPEG objects under {gcs_prefix} (e.g. {jpegs[0]}): JPEG batches need the BCNB "
            "conversion, which is not available yet"
        )
    slides = [n for n in names if n.lower().endswith(SLIDE_EXTENSIONS)]
    if not slides:
        raise RunConfigError(f"no slide ({', '.join(SLIDE_EXTENSIONS)}) under {gcs_prefix}")
    rows = []
    for name in slides:
        slide_id = name[len(prefix):].lstrip("/") if prefix else name
        row = {column: None for column in MANIFEST_COLUMNS}
        row.update({
            "dataset": ADHOC, "patient_id": slide_id, "slide_id": slide_id, "uri": f"gs://{bucket_name}/{name}",
            "specimen_type": specimen_type, "mpp_override": mpp_override,
            "mpp_source": "manual" if mpp_override is not None else "file",
        })
        rows.append(row)
    return pd.DataFrame(rows).astype(MANIFEST_COLUMNS)


def store_manifest(frame: pd.DataFrame) -> str:
    buffer = io.BytesIO()
    frame.to_parquet(buffer)
    blob = f"batches/{uuid.uuid4()}/manifest.parquet"
    upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, blob, buffer.getvalue(), "application/vnd.apache.parquet")
    return f"gs://{settings.GCS_ARTIFACTS_BUCKET}/{blob}"


def create_batch_from_prefix(
    session: Session, *, name: str, gcs_prefix: str, specimen_type: str, mpp_override: float | None,
    stages: tuple[str, ...], mode: str, concurrency: int, actor: str,
) -> ValidationRun:
    manifest_uri = store_manifest(manifest_from_prefix(gcs_prefix, specimen_type, mpp_override))
    return create_adhoc_run(session, name=name, manifest_uri=manifest_uri, stages=stages, mode=mode,
                            concurrency=concurrency, actor=actor)


def create_batch_from_manifest(
    session: Session, *, name: str, manifest_uri: str, split: str, stages: tuple[str, ...], mode: str,
    concurrency: int, actor: str, test_access_reason: str | None,
) -> ValidationRun:
    return create_run(session, RunRequest(
        name=name, manifest_uri=manifest_uri, split=split, stages=stages, mode=mode, concurrency=concurrency,
        actor=actor, splits_lock=Path(settings.SPLITS_LOCK_PATH), splits_root=Path(settings.SPLITS_ROOT),
        test_access_reason=test_access_reason,
    ))


# --- read models -------------------------------------------------------------------


def progress(session: Session, run: ValidationRun) -> tuple[dict[str, int], dict[str, int]]:
    """Item counts by status, and failed items by error class."""
    items = session.scalars(select(ValidationItem).where(ValidationItem.run_id == run.id)).all()
    counts = dict(Counter(i.status for i in items))
    failures = dict(Counter(i.error_class for i in items if i.status == "failed"))
    return counts, failures


def summary(session: Session, run: ValidationRun) -> dict:
    counts, failures = progress(session, run)
    return {
        "batch_id": str(run.id), "name": run.name, "dataset": run.dataset, "split": run.split,
        "stages": list(run.stages), "mode": run.mode, "concurrency": run.concurrency, "status": run.status,
        "is_locked_test": run.is_locked_test, "created_by": run.created_by,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "n_items": sum(counts.values()), "counts": counts, "failures_by_error_class": failures,
    }


def detail(session: Session, run: ValidationRun) -> dict:
    items = session.scalars(
        select(ValidationItem).where(ValidationItem.run_id == run.id).order_by(ValidationItem.slide_id)
    ).all()
    return {
        **summary(session, run),
        "items": [
            {"slide_id": i.slide_id, "patient_id": i.patient_id, "status": i.status,
             "case_id": str(i.case_id) if i.case_id else None, "failed_stage": i.failed_stage,
             "error_class": i.error_class, "error_detail": i.error_detail,
             "runtime_s": i.runtime_s, "cost_usd": float(i.cost_usd) if i.cost_usd is not None else None}
            for i in items
        ],
    }
