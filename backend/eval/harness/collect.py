"""Read an item's predictions from the database into ``validation_items.prediction`` (SPEC-02 §5.3).

The prediction is what the machine produced at the end of the run's stages, before any human
confirmation of grading: the harness never confirms the last stage. Mitosis points are the
candidates labelled ``mitosis`` (µm, slide coordinates), so SPEC-00 matching can use them.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.decision_record import DecisionRecord
from app.models.detection import Detection
from app.models.grading import Grading
from app.services import stages as stage_service

PREDICTION_SCHEMA_VERSION = 1


def item_cost_usd(session: Session, case_id) -> float:
    """Sum of the case's DecisionRecord costs (SPEC-02 §6.2 cost ledger); records without a price add 0."""
    total = session.scalar(select(func.sum(DecisionRecord.cost_usd)).where(DecisionRecord.case_id == case_id))
    return float(total or 0)


def collect_prediction(session: Session, case: Case, stages: list[str]) -> dict:
    prediction: dict = {
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "stage_outputs": {},
        "no_invasive_tumor": False,
    }
    for stage in stages:
        execution = stage_service.latest_execution(session, case.id, stage)
        if execution is not None:
            prediction["stage_outputs"][stage] = {
                "attempt": execution.attempt, "status": execution.status, "output_ref": execution.output_ref,
            }

    if "triage" in stages:
        # The triage output with any edits, whether or not the run went on to confirm it.
        hotspots = stage_service.effective_triage_hotspots(stage_service.latest_execution(session, case.id, "triage"))
        active = [h for h in hotspots if not h.get("excluded", False)]
        prediction["hotspots"] = {"active": len(active), "excluded": len(hotspots) - len(active)}
        prediction["no_invasive_tumor"] = not active

    if "mitosis" in stages and not prediction["no_invasive_tumor"]:
        figures = session.scalars(
            select(Detection).where(Detection.case_id == case.id, Detection.label == "mitosis").order_by(Detection.id)
        ).all()
        prediction["mitosis"] = {
            "count": len(figures),
            "points_um": [list(d.centroid_um) for d in figures],
            "scores": [d.ver_conf if d.ver_conf is not None else d.det_conf for d in figures],
        }

    if "grading" in stages and not prediction["no_invasive_tumor"]:
        grading = session.get(Grading, case.id)
        if grading is None:
            raise LookupError(f"case {case.id} finished grading without a gradings row")
        prediction["grading"] = {
            "grade": grading.grade,
            "total": grading.nottingham_sum,
            "tubule": grading.tubule_score,
            "pleo": grading.pleo_score,
            "mitoses": grading.mitotic_score,
            "tubule_percent": grading.tubule_percent,
            "histotype": grading.histologic_type,
            "needs_human": bool((grading.machine or {}).get("needs_human")),
        }
    return prediction
