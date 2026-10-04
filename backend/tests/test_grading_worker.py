"""Grading stage on the model gateway (SPEC-01 §3.4, §3.9; WP-2.3d).

The estimator is a fake Gemini that answers according to the strict schema it is asked
for. This replaces test_grading.py::test_medgemma_endpoint_failure_raises_when_mock_disabled
(SPEC-01 §6.3).
"""
import json
import threading
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.db import Base
from app.core.gcs import download_blob_as_bytes
from app.core.pipeline_config import get_pipeline_config
from app.inference import schemas
from app.inference.adapters.base import TransientCallError
from app.inference.errors import ModelUnavailableError, SchemaInvalidError
from app.inference.records import DecisionLog
from app.models import Case, Detection, Grading, HpfSite, Hotspot, Slide, StageExecution
from pipeline.errors import SlideReadError
from tests.fakes.gateway import FakeAdapter, json_text
from tests.fakes.runtime import make_runtime
from tests.fakes.slide import FakeOpenSlide, install_fake_slide
from tests.fakes.stage2 import seed_stage2
from worker.grading import run_grading

SIDE_PX, MPP = 16000, 0.25
N_PATCHES, HISTOTYPE_IMAGES = 6, 3
HOTSPOT = [[1000.0, 1000.0], [3000.0, 1000.0], [3000.0, 3000.0], [1000.0, 3000.0], [1000.0, 1000.0]]

ANSWERS = {
    schemas.TubuleEstimate: {"tumor_present": True, "tubule_percent": 40, "rationale": "tubules in a third"},
    schemas.PleoEstimate: {"pleomorphism_score": 3, "rationale": "marked variation"},
    schemas.HistotypeVerdict: {"type": "ILC", "rationale": "single files"},
}


def answer_by_schema(request):
    return json_text(ANSWERS[request.output_model])


def small_config(**fallback_tasks):
    """Six patches instead of 24 keeps the test fast; optional clinical fallbacks per task."""
    config = get_pipeline_config()
    scoring = config.scoring
    estimators = scoring.grading.estimators.model_copy(update={"histotype_images": HISTOTYPE_IMAGES})
    grading = scoring.grading.model_copy(update={"n_patches": N_PATCHES, "min_tumor_patches": 2, "estimators": estimators})
    config = config.model_copy(update={"scoring": scoring.model_copy(update={"grading": grading})})
    if fallback_tasks:
        policy = config.fallbacks.model_validate({"fallbacks": [
            {"task": task, "on": errors, "to": None} for task, errors in fallback_tasks.items()
        ]})
        config = config.model_copy(update={"fallbacks": policy})
    return config


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def seed(db_session, with_hpfs=True):
    case_id, slide_id, exec_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    raw_uri = f"gs://{settings.GCS_RAW_BUCKET}/cases/{case_id}/{slide_id}.svs"
    stage = StageExecution(id=exec_id, case_id=case_id, stage="grading", attempt=1, status="running")
    rows = [
        Case(id=case_id, created_by="grading_test"),
        Slide(id=slide_id, case_id=case_id, gcs_uri_original=raw_uri, mpp_x=MPP, mpp_y=MPP,
              width_px=SIDE_PX, height_px=SIDE_PX),
        stage,
        Hotspot(id="hs_01", case_id=case_id, stage_execution_id=exec_id, polygon_um=HOTSPOT, area_mm2=4.0,
                prob_mean=0.9, prob_max=0.95, source="model", excluded=False),
    ]
    if with_hpfs:
        rows += [
            HpfSite(case_id=case_id, seq=1, center_um=[2000.0, 2000.0], radius_um=262.0, mitotic_count=2),
            Detection(id="m_0001", case_id=case_id, hotspot_id="hs_01", centroid_um=[2000.0, 2010.0],
                      p_a=0.9, final_decision="mitosis", decision_path="A"),
            Detection(id="m_0002", case_id=case_id, hotspot_id="hs_01", centroid_um=[2050.0, 2000.0],
                      p_a=None, final_decision="mitosis", decision_path="human", review_label="mitosis"),
        ]
    db_session.add_all(rows)
    db_session.commit()
    seed_stage2(db_session, case_id, slide_id, SIDE_PX * MPP, SIDE_PX * MPP)
    return stage, raw_uri


def grading_row(db_session, stage):
    return db_session.scalars(select(Grading).where(Grading.case_id == stage.case_id)).first()


def output_json(stage):
    return json.loads(download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{stage.case_id}/grading_output.json"))


def test_grading_estimates_come_from_the_gateway_and_aggregate_deterministically(db_session, monkeypatch):
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    vlm, log = FakeAdapter(then=answer_by_schema), DecisionLog()

    output_ref, model_versions = run_grading(stage, db_session, make_runtime(stage, {"vertex_genai": vlm}, config=small_config(), log=log))

    config = get_pipeline_config()
    assert stage.status == "awaiting_review"
    assert model_versions == {"gemini_referee": config.models.version_of("gemini_referee")}
    rows = log.pending()
    assert {r["status"] for r in rows} == {"ok"}
    tubule = [r for r in rows if r["task"] == "tubule_patch"]
    pleo = [r for r in rows if r["task"] == "pleo_field"]
    (histotype,) = [r for r in rows if r["task"] == "histotype"]
    assert len(tubule) == len(pleo) == N_PATCHES
    assert {r["prompt_id"] for r in tubule} == {"tubule@v1.md"} and {r["prompt_id"] for r in pleo} == {"pleo@v1.md"}
    assert histotype["prompt_id"] == "histologic_type@v1.md" and len(histotype["input_spec"]["images"]) == HISTOTYPE_IMAGES
    assert histotype["entity_type"] == "slide"
    assert all(s["mpp"] == 1.0 and s["size_px"] == [512, 512] for r in tubule for s in r["input_spec"]["images"])

    grading = grading_row(db_session, stage)
    assert grading.tubule_percent == 40.0 and grading.tubule_score == 2
    assert grading.pleo_score == 3
    assert grading.histologic_type == "ILC" and grading.type_confirmed_by == "unconfirmed"
    assert grading.nottingham_sum == grading.tubule_score + grading.pleo_score + grading.mitotic_score

    output = output_json(stage)
    assert output["needs_human"] is False and output["schema_failed_patches"] == []
    assert output["histologic_type"]["type"] == "ILC" and output["histologic_type"]["record_id"] == str(histotype["id"])
    record_ids = {str(r["id"]) for r in tubule + pleo}
    for patch in output["patches"]:
        assert patch["tubule"]["record_id"] in record_ids and patch["pleo"]["record_id"] in record_ids
        assert "confidence" not in patch["tubule"] and "doer_percent" not in patch["tubule"]
        assert patch["review_status"] == "suggested"


def test_grading_requires_stage_4_hpfs(db_session, monkeypatch):
    stage, raw_uri = seed(db_session, with_hpfs=False)
    with pytest.raises(ValueError, match="no Stage 4 HPFs"):
        run_grading(stage, db_session, make_runtime(stage, {"vertex_genai": FakeAdapter(then=answer_by_schema)}, config=small_config()))


def test_estimator_outage_fails_the_stage_without_a_grade(db_session, monkeypatch):
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    runtime = make_runtime(stage, {"vertex_genai": FakeAdapter(then=TransientCallError("503"))}, config=small_config())
    with pytest.raises(ModelUnavailableError):
        run_grading(stage, db_session, runtime)
    assert grading_row(db_session, stage) is None


def test_v5_shaped_tubule_answer_fails_the_stage(db_session, monkeypatch):
    """v5 turned "40%" into 40 and a missing value into 0 (score 3); strict parsing refuses it."""
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)

    def v5_shaped(request):
        if request.output_model is schemas.TubuleEstimate:
            return json_text({"tubule_percent": "40%", "tumor_present": "yes", "confidence": "high"})
        return answer_by_schema(request)

    with pytest.raises(SchemaInvalidError):
        run_grading(stage, db_session, make_runtime(stage, {"vertex_genai": FakeAdapter(then=v5_shaped)}, config=small_config()))


def test_allowed_outages_leave_components_unassessed_never_defaulted(db_session, monkeypatch):
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)

    def histotype_down(request):
        if request.output_model is schemas.HistotypeVerdict:
            raise TransientCallError("503")
        return answer_by_schema(request)

    config = small_config(histotype=["ModelUnavailableError"])
    log = DecisionLog()
    run_grading(stage, db_session, make_runtime(stage, {"vertex_genai": FakeAdapter(then=histotype_down)}, config=config, log=log))

    grading = grading_row(db_session, stage)
    assert grading.histologic_type is None  # v5 wrote IDC-NST here
    output = output_json(stage)
    assert output["histologic_type"] is None and output["needs_human"] is True
    assert [r["task"] for r in log.pending() if r["producer_kind"] == "fallback"] == ["histotype"]


def test_failed_patch_estimates_are_left_out_of_the_aggregate(db_session, monkeypatch):
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    calls, lock = {"pleo": 0}, threading.Lock()  # estimates run on several threads

    def first_pleo_down(request):
        if request.output_model is schemas.PleoEstimate:
            with lock:
                calls["pleo"] += 1
                first = calls["pleo"] == 1
            if first:
                raise TransientCallError("503")
        return answer_by_schema(request)

    config = small_config(pleo_field=["ModelUnavailableError"])
    config = config.model_copy(update={"models": config.models.model_copy(update={
        "models": {**config.models.models, "gemini_referee": config.models.models["gemini_referee"].model_copy(update={"max_attempts": 1})}
    })})
    run_grading(stage, db_session, make_runtime(stage, {"vertex_genai": FakeAdapter(then=first_pleo_down)}, config=config))

    output = output_json(stage)
    failed = [p for p in output["patches"] if p["review_status"] == "needs_review"]
    assert len(failed) == 1 and failed[0]["pleo"]["pleomorphism_score"] is None
    assert output["schema_failed_patches"] == [failed[0]["id"]] and output["needs_human"] is True
    # The remaining patches still grade: no default 2 was voted in for the failed one.
    assert grading_row(db_session, stage).pleo_score == 3


def test_unreadable_slide_fails_the_stage(db_session, monkeypatch):
    import openslide

    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    monkeypatch.setattr(openslide, "OpenSlide", lambda path: (_ for _ in ()).throw(openslide.OpenSlideError("corrupt")))
    with pytest.raises(SlideReadError, match="corrupt"):
        run_grading(stage, db_session, make_runtime(stage, {"vertex_genai": FakeAdapter(then=answer_by_schema)}, config=small_config()))
