"""Create a validation run from a manifest split (SPEC-02 §5.2–5.3).

A run records what it evaluated: the manifest's SHA-256, the config hash, the model registry's
SHA-256 and the splits lock's SHA-256. Running on ``split=test`` needs a stated reason, which is
written to ``audit_events`` (``test_split_access``) before any item starts.
"""
from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.gcs import download_blob_to_filename, parse_gcs_uri
from app.core.pipeline_config import get_config_hash
from app.core.run_context import STAGES
from app.models.audit import AuditEvent
from app.models.validation import RUN_MODES, ValidationItem, ValidationRun
from eval.datasets.manifest import validate_manifest
from eval.splits import verify_lock

HASH_CHUNK_BYTES = 8 * 1024 * 1024  # SPEC-02 §5.5 streaming hash
# The pipeline stops for review after these stages, so a run may end at one of them.
RUN_END_STAGES = ("triage", "mitosis", "grading")


class RunConfigError(ValueError):
    """The run request is inconsistent (stages, mode, concurrency)."""


class LockedTestSplitError(PermissionError):
    """``split=test`` was requested without a stated reason (SPEC-02 §5.2)."""


class EmptySplitError(ValueError):
    """The manifest has no row in the requested split."""


@dataclass(frozen=True)
class RunRequest:
    name: str
    manifest_uri: str             # local path or gs:// URI of the manifest parquet
    split: str
    stages: tuple[str, ...]
    mode: str
    concurrency: int
    actor: str
    splits_lock: Path
    splits_root: Path
    arm: str | None = None
    test_access_reason: str | None = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_stages(stages: tuple[str, ...]) -> tuple[str, ...]:
    """A run executes the pipeline from ingest up to a stage that stops for review."""
    if not stages or stages[0] != "ingest":
        raise RunConfigError(f"a run starts at ingest, got {list(stages)}")
    if tuple(stages) != STAGES[: len(stages)]:
        raise RunConfigError(f"stages must be a prefix of {list(STAGES)}, got {list(stages)}")
    if stages[-1] not in RUN_END_STAGES:
        raise RunConfigError(f"a run must end at one of {list(RUN_END_STAGES)}, got {stages[-1]!r}")
    return tuple(stages)


def read_manifest(uri: str) -> tuple[pd.DataFrame, str]:
    """The manifest and its SHA-256; a gs:// manifest is copied to a temporary file first."""
    if uri.startswith("gs://"):
        bucket, blob = parse_gcs_uri(uri)
        with tempfile.TemporaryDirectory(prefix="og_manifest_") as scratch:
            local = Path(scratch) / "manifest.parquet"
            download_blob_to_filename(bucket, blob, str(local))
            return pd.read_parquet(local), sha256_file(local)
    path = Path(uri)
    return pd.read_parquet(path), sha256_file(path)


def registry_sha256() -> str:
    return sha256_file(Path(settings.CONFIGS_DIR) / "models.yaml")


def create_run(session: Session, request: RunRequest) -> ValidationRun:
    """Validate the request and the manifest, then insert the run and one pending item per slide. Commits."""
    stages = check_stages(tuple(request.stages))
    if request.mode not in RUN_MODES:
        raise RunConfigError(f"mode must be one of {list(RUN_MODES)}, got {request.mode!r}")
    if request.concurrency < 1:
        raise RunConfigError(f"concurrency must be at least 1, got {request.concurrency}")
    reason = (request.test_access_reason or "").strip()
    if request.split == "test" and not reason:
        raise LockedTestSplitError('split=test is locked: pass --confirm-test-access "<reason>"')

    verify_lock(request.splits_lock, request.splits_root)
    manifest, manifest_sha256 = read_manifest(request.manifest_uri)
    validate_manifest(manifest)
    rows = manifest[manifest["split"] == request.split]
    if rows.empty:
        raise EmptySplitError(f"{request.manifest_uri} has no rows in split {request.split!r}")
    datasets = sorted(rows["dataset"].unique())
    if len(datasets) != 1:
        raise RunConfigError(f"a run evaluates one dataset, the split holds {datasets}")

    run = ValidationRun(
        name=request.name,
        dataset=datasets[0],
        split=request.split,
        manifest_uri=request.manifest_uri,
        manifest_sha256=manifest_sha256,
        stages=list(stages),
        mode=request.mode,
        concurrency=request.concurrency,
        config_hash=get_config_hash(),
        registry_sha256=registry_sha256(),
        splits_lock_sha256=sha256_file(request.splits_lock),
        arm=request.arm,
        is_locked_test=request.split == "test",
        status="created",
        created_by=request.actor,
    )
    session.add(run)
    session.flush()
    for row in rows.itertuples(index=False):
        session.add(ValidationItem(
            run_id=run.id, slide_id=str(row.slide_id), patient_id=str(row.patient_id), status="pending"
        ))
    if run.is_locked_test:
        session.add(AuditEvent(
            actor=request.actor,
            event_type="test_split_access",
            payload={
                "run_id": str(run.id), "dataset": run.dataset, "reason": reason,
                "config_hash": run.config_hash, "items": len(rows),
            },
        ))
    session.commit()
    return run
