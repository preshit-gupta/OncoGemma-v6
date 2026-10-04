"""``one-shot``: one slide through the pipeline in EVAL mode, one JSON document out (SPEC-02 §5.5, V1; AC3).

The slide becomes a one-item ad-hoc run, so it takes exactly the harness path: the same stage
handlers, the same confirm service, EVAL mode with no fallbacks. By default the stages run in
this process (``execute_stage``); with ``in_process=False`` the app's workers run them.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.gcs import download_blob_to_filename, parse_gcs_uri
from app.core.pipeline_config import get_config_hash, get_pipeline_config
from app.models.decision_record import DecisionRecord
from app.models.detection import Detection
from app.models.grading import Grading
from app.models.hpf_site import HpfSite
from app.models.stage_execution import StageExecution
from app.models.validation import ValidationItem
from app.services import stages as stage_service
from eval.datasets.manifest import MANIFEST_COLUMNS
from eval.harness.controller import RunController
from eval.harness.documents import Decision, OneShotResult, StageResult
from eval.harness.runs import ADHOC, create_adhoc_run, sha256_file
from worker.execution import StageFailedError, execute_stage, mark_running

EXIT_OK, EXIT_STAGE_FAILED = 0, 2


def slide_sha256(slide_uri: str) -> str:
    """Streaming SHA-256 of the slide (8 MiB chunks), from a scratch copy that is deleted afterwards."""
    bucket, blob = parse_gcs_uri(slide_uri)
    with tempfile.TemporaryDirectory(prefix="og_one_shot_") as scratch:
        local = os.path.join(scratch, "slide")
        download_blob_to_filename(bucket, blob, local)
        return sha256_file(Path(local))


def write_one_row_manifest(path: Path, slide_uri: str, sha256: str, specimen_type: str, mpp: float | None) -> None:
    name = slide_uri.rstrip("/").rsplit("/", 1)[-1]
    row = {column: None for column in MANIFEST_COLUMNS}
    row.update({
        "dataset": ADHOC, "patient_id": name, "slide_id": name, "uri": slide_uri, "sha256": sha256,
        "specimen_type": specimen_type, "mpp_override": mpp, "mpp_source": "manual" if mpp is not None else "file",
    })
    pd.DataFrame([row]).astype(MANIFEST_COLUMNS).to_parquet(path)


def run_queued(session: Session, run_id, *, handlers=None, gateway_factory=None) -> int:
    """Execute this run's queued stages in-process, as a worker would. Returns how many ran."""
    ran = 0
    while True:
        execution = session.scalars(
            select(StageExecution)
            .where(StageExecution.run_id == run_id, StageExecution.status == "queued")
            .order_by(StageExecution.attempt, StageExecution.id)
        ).first()
        if execution is None:
            return ran
        mark_running(execution)
        session.commit()
        try:
            execute_stage(session, execution, handlers=handlers, gateway_factory=gateway_factory)
        except StageFailedError:
            pass  # the failure is on the execution row; the controller turns it into the item's error
        ran += 1


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _latency(execution: StageExecution) -> float | None:
    if execution.started_at is None or execution.completed_at is None:
        return None
    return (execution.completed_at - execution.started_at).total_seconds()


def build_result(session: Session, run, item: ValidationItem, slide_uri: str, specimen_type: str) -> OneShotResult:
    case_id = item.case_id
    stages = []
    for stage in run.stages:
        execution = stage_service.latest_execution(session, case_id, stage)
        if execution is None:
            continue
        error = None
        if execution.error:
            try:
                error = json.loads(execution.error)
            except ValueError:
                error = {"class": "StageFailed", "detail": execution.error}
        stages.append(StageResult(
            stage=stage, attempt=execution.attempt, status=execution.status, output_ref=execution.output_ref,
            model_versions=execution.model_versions, config_hash=execution.config_hash,
            started_at=_iso(execution.started_at), completed_at=_iso(execution.completed_at),
            latency_s=_latency(execution), error=error,
        ))

    triage = stage_service.latest_execution(session, case_id, "triage")
    hotspots = stage_service.effective_triage_hotspots(triage) if triage is not None and triage.output_ref else []
    candidates = [
        {"id": d.id, "centroid_um": d.centroid_um, "hotspot_id": d.hotspot_id, "p_a": d.p_a, "p_b": d.p_b,
         "in_tumor": d.in_tumor, "final_decision": d.final_decision, "decision_path": d.decision_path,
         "review_label": d.review_label, "counted": bool(d.counted)}
        for d in session.scalars(select(Detection).where(Detection.case_id == case_id).order_by(Detection.id))
    ]
    hpfs = [
        {"seq": h.seq, "center_um": h.center_um, "radius_um": h.radius_um, "count": h.mitotic_count, "source": h.source}
        for h in session.scalars(select(HpfSite).where(HpfSite.case_id == case_id).order_by(HpfSite.seq))
    ]
    grading = session.get(Grading, case_id)
    patches = list((grading.machine or {}).get("patches", [])) if grading is not None else []
    decisions = [
        Decision(
            id=str(r.id), stage=r.stage, task=r.task, entity_type=r.entity_type, entity_id=r.entity_id,
            producer_kind=r.producer_kind, producer_id=r.producer_id, producer_version=r.producer_version,
            status=r.status, error_class=r.error_class, output=r.output, latency_ms=r.latency_ms,
            cost_usd=float(r.cost_usd) if r.cost_usd is not None else None, cache_hit=r.cache_hit,
            supersedes_id=str(r.supersedes_id) if r.supersedes_id else None,
        )
        for r in session.scalars(
            select(DecisionRecord).where(DecisionRecord.case_id == case_id).order_by(DecisionRecord.created_at, DecisionRecord.id)
        )
    ]
    registry = get_pipeline_config().models
    return OneShotResult(
        slide_uri=slide_uri, specimen_type=specimen_type, run_id=str(run.id), case_id=str(case_id),
        status=item.status, failed_stage=item.failed_stage, error_class=item.error_class,
        error_detail=item.error_detail, config_hash=get_config_hash(),
        model_versions={key: registry.version_of(key) for key in [*registry.models, *registry.heuristics]},
        stages=stages, prediction=item.prediction, hotspots=hotspots, candidates=candidates, hpfs=hpfs,
        patches=patches, decisions=decisions, cost_usd=float(item.cost_usd or 0), runtime_s=item.runtime_s,
    )


def one_shot(
    session: Session,
    slide_uri: str,
    specimen_type: str,
    out_path: Path,
    *,
    actor: str,
    mpp: float | None = None,
    stages: tuple[str, ...] = ("ingest", "preprocess", "qc", "triage", "mitosis", "grading"),
    in_process: bool = True,
    handlers=None,
    gateway_factory=None,
    poll_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, OneShotResult]:
    """Run one slide and write the result JSON to ``out_path``. Returns (exit code, result)."""
    if not slide_uri.startswith("gs://"):
        raise ValueError(f"one-shot reads slides from gs:// URIs, got {slide_uri!r}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = out_path.with_name(out_path.stem + ".manifest.parquet")
    write_one_row_manifest(manifest_path, slide_uri, slide_sha256(slide_uri), specimen_type, mpp)
    run = create_adhoc_run(
        session, name=f"one-shot {out_path.stem}", manifest_uri=str(manifest_path), stages=stages,
        mode="auto", concurrency=1, actor=actor,
    )
    controller = RunController(session, run.id)
    while True:
        controller.step()
        if controller.run.status == "completed":
            break
        if in_process:
            run_queued(session, run.id, handlers=handlers, gateway_factory=gateway_factory)
        else:
            sleep(poll_s)

    item = session.scalars(select(ValidationItem).where(ValidationItem.run_id == run.id)).one()
    result = build_result(session, run, item, slide_uri, specimen_type)
    out_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return (EXIT_OK if item.status == "succeeded" else EXIT_STAGE_FAILED), result
