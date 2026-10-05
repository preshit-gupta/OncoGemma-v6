"""A scripted six-stage pipeline and an in-process worker for harness tests.

Each fake handler ends its stage the way the real one does (status, chaining through
``queue_stage``, triage output in GCS, detections, gradings row), so the controller sees the
same database states as in production. A slide's behaviour comes from its URI:

- ``.../ok-*``        every stage succeeds; 3 mitoses; grade 2 (2 + 2 + 2)
- ``.../qcfail-*``    QC hard-fails (stage failed, case needs_rescan)
- ``.../boom-*``      triage raises RuntimeError (while ``BOOM`` is on)
- ``.../notumour-*``  triage finds only excluded hotspots
"""
import json

from sqlalchemy import select

from app.core.config import settings
from app.core.gcs import upload_blob_from_bytes
from app.models.case import Case
from app.models.detection import Detection
from app.models.grading import Grading
from app.models.slide import Slide
from app.models.stage_execution import StageExecution
from app.services.stages import queue_stage
from tests.fakes.gateway import make_gateway
from worker.execution import StageFailedError, execute_stage, mark_running

BOOM = {"on": True}


def _slide(session, execution) -> Slide:
    return session.scalars(select(Slide).where(Slide.case_id == execution.case_id)).first()


def _kind(session, execution) -> str:
    return _slide(session, execution).gcs_uri_original.rsplit("/", 1)[-1].split("-", 1)[0]


def _next(session, execution, stage):
    queue_stage(session, execution.case_id, stage, input_ref={"slide_id": str(_slide(session, execution).id)}, parent=execution)


def ingest(execution, session, runtime):
    slide = _slide(session, execution)
    if slide.mpp_x is None:
        slide.mpp_x = slide.mpp_y = 0.25
        slide.mpp_source = "file"
    slide.status = "ready"
    _next(session, execution, "preprocess")
    return "gs://fake/ingest.json", {}


def preprocess(execution, session, runtime):
    _next(session, execution, "qc")
    return "gs://fake/preprocess.json", {}


def qc(execution, session, runtime):
    if _kind(session, execution) == "qcfail":
        execution.status = "failed"
        execution.error = "QC Hard Failure: ['tissue area below minimum']"
        session.get(Case, execution.case_id).status = "needs_rescan"
    else:
        _next(session, execution, "triage")
    return "gs://fake/qc.json", {}


def triage(execution, session, runtime):
    kind = _kind(session, execution)
    if kind == "boom" and BOOM["on"]:
        raise RuntimeError("tumour head unavailable")
    hotspot = {"id": f"hs_{execution.attempt}", "center_um": [300.0, 300.0], "hpf_diameter_um": 500.0,
               "polygon_um": [[0, 0], [600, 0], [600, 600], [0, 600], [0, 0]], "window_um": 600.0,
               "source": "model", "excluded": kind == "notumour"}
    blob = f"cases/{execution.case_id}/triage/output.json"
    output = {"hotspots": [hotspot], "hpf_diameter_um": 500.0, "frame_um": 600.0, "hpf_target": 10}
    upload_blob_from_bytes(settings.GCS_ARTIFACTS_BUCKET, blob, json.dumps(output).encode(), "application/json")
    execution.status = "awaiting_review"
    return f"gs://{settings.GCS_ARTIFACTS_BUCKET}/{blob}", {}


def mitosis(execution, session, runtime):
    for i in range(3):
        session.add(Detection(id=f"m_{execution.case_id}_{i}", case_id=execution.case_id,
                              centroid_um=[100.0 * i, 50.0], p_a=0.9, final_decision="mitosis", decision_path="A"))
    execution.status = "awaiting_review"
    return "gs://fake/mitosis.json", {}


def grading(execution, session, runtime):
    session.merge(Grading(case_id=execution.case_id, tubule_score=2, pleo_score=2, mitotic_score=2,
                          nottingham_sum=6, grade=2, histologic_type="idc_nst", machine={"needs_human": False},
                          overrides={}))
    execution.status = "awaiting_review"
    return "gs://fake/grading.json", {}


HANDLERS = {"ingest": ingest, "preprocess": preprocess, "qc": qc, "triage": triage,
            "mitosis": mitosis, "grading": grading}


def drain(session) -> int:
    """Run every queued execution once, as the worker loop does; returns how many ran."""
    ran = 0
    while True:
        execution = session.scalars(
            select(StageExecution).where(StageExecution.status == "queued").order_by(StageExecution.attempt, StageExecution.stage)
        ).first()
        if execution is None:
            return ran
        mark_running(execution)
        session.commit()
        try:
            execute_stage(session, execution, handlers=HANDLERS,
                          gateway_factory=lambda config, log: make_gateway(config, {}, log=log))
        except StageFailedError:
            pass  # recorded on the execution; the controller reads it
        ran += 1
