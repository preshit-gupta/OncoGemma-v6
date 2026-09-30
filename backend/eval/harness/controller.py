"""The run controller (SPEC-02 §5.3).

The controller never runs a stage handler; the app's workers do, from the same queue. It:

- starts pending items while fewer than ``concurrency`` are in flight: it creates the case (with
  the manifest's specimen type) and a slide that points at the manifest URI, and queues ingest in
  ``eval`` mode with the run's id;
- watches each running item's latest stage executions. A stage that waits for review is
  confirmed through ``app.services.stages``, the function the API uses, as ``harness:<run_id>``
  (``auto`` mode only; in ``manual`` mode people confirm in the app);
- ends each item in exactly one of ``succeeded``, ``failed`` (with the stage and the error class)
  or ``excluded_qc``, and collects the predictions of succeeded items.

Every decision is taken from the database, so a controller that is killed and started again
(``resume``) continues where the old one stopped without repeating a stage.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.case import Case
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.models.validation import ValidationItem, ValidationRun
from app.services import stages as stage_service
from eval.harness.collect import collect_prediction, item_cost_usd
from eval.harness.runs import read_manifest

ACTIVE_ITEM = ("pending", "running")
FINAL_RUN = ("completed", "cancelled", "failed")


class ManifestChangedError(RuntimeError):
    """The manifest at the run's URI is not the one the run was created from."""


@dataclass(frozen=True)
class Progress:
    counts: dict[str, int]

    @property
    def active(self) -> int:
        return sum(self.counts.get(s, 0) for s in ACTIVE_ITEM)


def harness_actor(run: ValidationRun) -> str:
    return f"harness:{run.id}"


def error_class_of(execution: StageExecution) -> tuple[str, str]:
    """The error class and detail a failed execution recorded (``execute_stage`` writes JSON)."""
    try:
        error = json.loads(execution.error or "")
    except ValueError:
        return "StageFailed", execution.error or ""
    return error.get("class", "StageFailed"), error.get("detail", "")


def item_progress(session: Session, run_id) -> Progress:
    rows = session.execute(
        select(ValidationItem.status).where(ValidationItem.run_id == run_id)
    ).scalars().all()
    counts: dict[str, int] = {}
    for status in rows:
        counts[status] = counts.get(status, 0) + 1
    return Progress(counts)


class RunController:
    def __init__(self, session: Session, run_id, *, dispatch: Callable = stage_service.dispatch):
        self.session = session
        self.run = session.get(ValidationRun, run_id)
        if self.run is None:
            raise LookupError(f"validation run {run_id} not found")
        self.dispatch = dispatch
        self._manifest: pd.DataFrame | None = None

    # --- manifest --------------------------------------------------------------

    def manifest_row(self, slide_id: str) -> pd.Series:
        if self._manifest is None:
            manifest, sha256 = read_manifest(self.run.manifest_uri)
            if sha256 != self.run.manifest_sha256:
                raise ManifestChangedError(
                    f"{self.run.manifest_uri} has SHA-256 {sha256}; run {self.run.id} was created from "
                    f"{self.run.manifest_sha256}"
                )
            self._manifest = manifest[manifest["split"] == self.run.split].set_index("slide_id", drop=False)
        return self._manifest.loc[slide_id]

    # --- one pass --------------------------------------------------------------

    def step(self) -> Progress:
        """Advance every running item, then start pending items up to the run's concurrency."""
        run = self.run
        self.session.refresh(run)
        if run.status in FINAL_RUN:
            return item_progress(self.session, run.id)
        if run.status == "created":
            run.status = "running"
            self.session.commit()

        running = self.session.scalars(
            select(ValidationItem).where(ValidationItem.run_id == run.id, ValidationItem.status == "running")
        ).all()
        for item in running:
            self.advance(item)

        free = run.concurrency - item_progress(self.session, run.id).counts.get("running", 0)
        if free > 0:
            pending = self.session.scalars(
                select(ValidationItem)
                .where(ValidationItem.run_id == run.id, ValidationItem.status == "pending")
                .order_by(ValidationItem.slide_id)
                .limit(free)
            ).all()
            for item in pending:
                self.start(item)

        progress = item_progress(self.session, run.id)
        if progress.active == 0:
            run.status = "completed"
            run.finished_at = datetime.now(timezone.utc)
            self.session.commit()
        return progress

    def run_until_done(self, poll_s: float, *, sleep: Callable[[float], None] = time.sleep,
                       on_step: Callable[[Progress], None] | None = None) -> Progress:
        while True:
            progress = self.step()
            if on_step is not None:
                on_step(progress)
            if self.run.status in FINAL_RUN:
                return progress
            sleep(poll_s)

    # --- items -----------------------------------------------------------------

    def start(self, item: ValidationItem) -> None:
        """Create the item's case and slide and queue ingest, in one transaction."""
        row = self.manifest_row(item.slide_id)
        actor = harness_actor(self.run)
        case = Case(created_by=actor, status="open", specimen_type=str(row["specimen_type"]))
        self.session.add(case)
        self.session.flush()
        slide = Slide(case_id=case.id, gcs_uri_original=str(row["uri"]), checksum_sha256=str(row["sha256"]))
        if not pd.isna(row["mpp_override"]):
            slide.mpp_x = slide.mpp_y = float(row["mpp_override"])
            slide.mpp_source = str(row["mpp_source"])
        self.session.add(slide)
        self.session.flush()
        ingest = stage_service.queue_stage(
            self.session, case.id, "ingest",
            input_ref={"slide_id": str(slide.id), "gcs_uri_original": slide.gcs_uri_original},
            run_id=self.run.id,
        )
        item.case_id = case.id
        item.status = "running"
        item.started_at = datetime.now(timezone.utc)
        self.session.add(AuditEvent(
            case_id=str(case.id), actor=actor, event_type="case_created",
            payload={"run_id": str(self.run.id), "dataset": self.run.dataset, "slide_id": item.slide_id},
        ))
        self.session.commit()
        self.dispatch(ingest, ingest.input_ref)

    def advance(self, item: ValidationItem) -> None:
        """Move one running item on from its latest stage executions."""
        case = self.session.get(Case, item.case_id)
        slide = self.session.scalars(select(Slide).where(Slide.case_id == case.id)).first()
        if slide.status == "needs_mpp":
            self.finish(item, "failed", stage="ingest", error=("MissingMppError", "the slide has no MPP and the manifest gives none"))
            return
        stages = list(self.run.stages)
        for index, stage in enumerate(stages):
            execution = stage_service.latest_execution(self.session, case.id, stage)
            last = index == len(stages) - 1
            if execution is None:
                if case.status == "done":  # triage confirmed with no invasive tumour: nothing follows
                    self.succeed(item, case)
                return  # the previous stage has not queued this one yet
            if execution.status in ("queued", "running"):
                return
            if execution.status == "failed":
                if stage == "qc" and case.status == "needs_rescan":
                    self.finish(item, "excluded_qc", stage="qc", error=("QcHardFail", execution.error or ""))
                else:
                    self.finish(item, "failed", stage=stage, error=error_class_of(execution))
                return
            if execution.status == "awaiting_review":
                if last:
                    self.succeed(item, case)
                elif self.run.mode == "auto":
                    self.confirm(item, case, execution)
                return
            if execution.status not in ("done", "confirmed"):
                self.finish(item, "failed", stage=stage, error=("UnexpectedStageStatus", execution.status))
                return
            if last:
                self.succeed(item, case)
                return

    def confirm(self, item: ValidationItem, case: Case, execution: StageExecution) -> None:
        options = {}
        try:
            if execution.stage == "triage":
                hotspots = stage_service.effective_triage_hotspots(execution)
                options["no_invasive_tumor"] = not any(not h.get("excluded", False) for h in hotspots)
            stage_service.confirm_stage(self.session, case.id, execution.stage, harness_actor(self.run), **options)
        except stage_service.StageServiceError as exc:
            self.session.rollback()
            self.finish(item, "failed", stage=execution.stage, error=(type(exc).__name__, exc.detail))

    def succeed(self, item: ValidationItem, case: Case) -> None:
        try:
            item.prediction = collect_prediction(self.session, case, list(self.run.stages))
        except (stage_service.StageServiceError, LookupError) as exc:
            detail = exc.detail if isinstance(exc, stage_service.StageServiceError) else str(exc)
            self.finish(item, "failed", stage="collect", error=(type(exc).__name__, detail))
            return
        self.finish(item, "succeeded")

    def finish(self, item: ValidationItem, status: str, *, stage: str | None = None,
               error: tuple[str, str] | None = None) -> None:
        now = datetime.now(timezone.utc)
        item.status = status
        item.failed_stage = stage
        item.error_class, item.error_detail = error if error else (None, None)
        item.finished_at = now
        started = item.started_at if item.started_at.tzinfo else item.started_at.replace(tzinfo=timezone.utc)
        item.runtime_s = (now - started).total_seconds()
        item.cost_usd = item_cost_usd(self.session, item.case_id)
        self.session.commit()


# --- run actions (CLI and batch API) ---------------------------------------------


def cancel_run(session: Session, run_id, actor: str) -> int:
    """Pending items become cancelled; running items finish their current stage. Commits."""
    run = session.get(ValidationRun, run_id)
    if run is None:
        raise LookupError(f"validation run {run_id} not found")
    pending = session.scalars(
        select(ValidationItem).where(ValidationItem.run_id == run.id, ValidationItem.status == "pending")
    ).all()
    for item in pending:
        item.status = "cancelled"
    session.add(AuditEvent(actor=actor, event_type="validation_run_cancelled",
                           payload={"run_id": str(run.id), "cancelled_items": len(pending)}))
    session.commit()
    return len(pending)


def retry_items(session: Session, run_id, statuses: tuple[str, ...], actor: str) -> int:
    """Failed or cancelled items run again. A failed item gets a new attempt of its failed stage on
    the same case (earlier attempts stay); a cancelled item that never started goes back to pending."""
    allowed = {"failed", "cancelled"}
    if not statuses or not set(statuses) <= allowed:
        raise ValueError(f"only {sorted(allowed)} items are retried, got {list(statuses)}")
    run = session.get(ValidationRun, run_id)
    if run is None:
        raise LookupError(f"validation run {run_id} not found")
    items = session.scalars(
        select(ValidationItem).where(ValidationItem.run_id == run.id, ValidationItem.status.in_(statuses))
    ).all()
    queued: list[StageExecution] = []
    for item in items:
        if item.case_id is None:
            item.status = "pending"
            continue
        if item.failed_stage is None:
            raise ValueError(f"item {item.slide_id} is {item.status} without a failed stage")
        if item.failed_stage != "collect":  # a failed collection is simply read again
            execution = stage_service.latest_execution(session, item.case_id, item.failed_stage)
            queued.append(stage_service.queue_stage(
                session, item.case_id, item.failed_stage, input_ref=execution.input_ref, parent=execution
            ))
        item.status = "running"
        item.failed_stage = item.error_class = item.error_detail = None
        item.finished_at = item.runtime_s = None
    if items:
        run.status = "running"
        run.finished_at = None
    session.add(AuditEvent(actor=actor, event_type="validation_run_retried",
                           payload={"run_id": str(run.id), "statuses": list(statuses), "items": len(items)}))
    session.commit()
    for execution in queued:
        stage_service.dispatch(execution, execution.input_ref)
    return len(items)
