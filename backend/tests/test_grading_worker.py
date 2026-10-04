"""Stage 5 worker on the model gateway (SPEC-01 §3.4, §3.9; SPEC-07 §4-7; WP-2.3d, WP-8.6).

The estimator is a fake Gemini that answers according to the strict schema it is asked for.
Samples come from tumour tiles inside the confirmed hotspot; M from the Stage 4 rows.
"""
import json
import threading
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.db import Base
from app.core.gcs import blob_exists, download_blob_as_bytes
from app.core.pipeline_config import get_pipeline_config
from app.inference import schemas
from app.inference.adapters.base import TransientCallError
from app.inference.errors import ModelUnavailableError, SchemaInvalidError
from app.inference.records import DecisionLog
from app.models import Case, Detection, Grading, HpfSite, Hotspot, Slide, StageExecution
from pipeline.errors import SlideReadError
from pipeline.grading import MACHINE_SCHEMA, sample_blob
from pipeline.grading_sampling import SamplingFrameEmptyError
from tests.fakes.gateway import FakeAdapter, json_text
from tests.fakes.runtime import make_runtime
from tests.fakes.slide import FakeOpenSlide, install_fake_slide
from tests.fakes.stage2 import seed_stage2
from tests.fakes.stage3 import seed_tiles, tiles_covering
from worker.grading import TriageOutputMissingError, run_grading

SIDE_PX, MPP = 16000, 0.25
N_TUBULE, N_PLEO, HISTOTYPE_IMAGES = 6, 4, 3
HOTSPOT = [[1000.0, 1000.0], [3000.0, 1000.0], [3000.0, 3000.0], [1000.0, 3000.0], [1000.0, 1000.0]]
EXCLUDED = [[3200.0, 3200.0], [3800.0, 3200.0], [3800.0, 3800.0], [3200.0, 3800.0], [3200.0, 3200.0]]

ANSWERS = {
    schemas.TubuleEstimate: {"tumor_present": True, "tubule_percent": 40, "rationale": "tubules in a third"},
    schemas.PleoEstimate: {"pleomorphism_score": 3, "rationale": "marked variation"},
    schemas.HistotypeVerdict: {"type": "ILC", "rationale": "single files"},
}


def answer_by_schema(request):
    return json_text(ANSWERS[request.output_model])


def small_config(**fallback_tasks):
    """Six tubule samples and four fields instead of 48 + 48 keep the test fast; optional clinical fallbacks per task."""
    config = get_pipeline_config()
    scoring = config.scoring
    estimators = scoring.grading.estimators.model_copy(update={"histotype_images": HISTOTYPE_IMAGES})
    grading = scoring.grading.model_copy(update={"estimators": estimators})
    config = config.model_copy(update={"scoring": scoring.model_copy(update={"grading": grading})})
    profiles = config.specimen_profiles
    resection = profiles.profiles["resection"]
    counts = resection.grading.model_copy(update={"tubule_patches": N_TUBULE, "pleo_fields": N_PLEO})
    profiles = profiles.model_copy(update={"profiles": {**profiles.profiles, "resection": resection.model_copy(update={"grading": counts})}})
    config = config.model_copy(update={"specimen_profiles": profiles})
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


def seed(db_session, with_hpfs=True, triage_status="confirmed", with_tiles=True):
    case_id, slide_id, exec_id, triage_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    raw_uri = f"gs://{settings.GCS_RAW_BUCKET}/cases/{case_id}/{slide_id}.svs"
    stage = StageExecution(id=exec_id, case_id=case_id, stage="grading", attempt=1, status="running")
    rows = [
        Case(id=case_id, created_by="grading_test"),
        Slide(id=slide_id, case_id=case_id, gcs_uri_original=raw_uri, mpp_x=MPP, mpp_y=MPP,
              width_px=SIDE_PX, height_px=SIDE_PX, checksum_sha256="5e" * 32),
        StageExecution(id=triage_id, case_id=case_id, stage="triage", attempt=1, status=triage_status),
        stage,
        Hotspot(id="hs_01", case_id=case_id, stage_execution_id=triage_id, polygon_um=HOTSPOT, area_mm2=4.0,
                prob_mean=0.9, prob_max=0.95, source="model", excluded=False, rank=1),
        Hotspot(id="hs_02", case_id=case_id, stage_execution_id=triage_id, polygon_um=EXCLUDED, area_mm2=0.36,
                prob_mean=0.95, prob_max=0.97, source="model", excluded=True, rank=2),
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
    if with_tiles:
        # Tumour under both hotspots; the excluded one must never be sampled.
        seed_tiles(case_id, sorted(set(tiles_covering(900, 900, 3100, 3100) + tiles_covering(3100, 3100, 3900, 3900))))
    return stage, raw_uri


def grading_row(db_session, stage):
    return db_session.scalars(select(Grading).where(Grading.case_id == stage.case_id)).first()


def output_json(stage):
    return json.loads(download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{stage.case_id}/grading/output.json"))


def test_grading_estimates_come_from_the_gateway_and_aggregate_deterministically(db_session, monkeypatch):
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    vlm, log = FakeAdapter(then=answer_by_schema), DecisionLog()

    output_ref, model_versions = run_grading(stage, db_session, make_runtime(stage, {"vertex_genai": vlm}, config=small_config(), log=log))

    config = get_pipeline_config()
    assert stage.status == "awaiting_review"
    assert output_ref.endswith(f"cases/{stage.case_id}/grading/output.json")
    assert model_versions == {"gemini_referee": config.models.version_of("gemini_referee")}
    rows = log.pending()
    assert {r["status"] for r in rows} == {"ok"}
    tubule = [r for r in rows if r["task"] == "tubule_patch"]
    pleo = [r for r in rows if r["task"] == "pleo_field"]
    (histotype,) = [r for r in rows if r["task"] == "histotype"]
    assert len(tubule) == N_TUBULE and len(pleo) == N_PLEO
    assert {r["prompt_id"] for r in tubule} == {"tubule@v1.md"} and {r["prompt_id"] for r in pleo} == {"pleo@v1.md"}
    assert histotype["prompt_id"] == "histologic_type@v1.md" and len(histotype["input_spec"]["images"]) == HISTOTYPE_IMAGES
    assert histotype["entity_type"] == "slide"
    # SPEC-07 AC6: tubule samples at 1.0 µm/px, pleomorphism fields at 0.25 µm/px, 512 px each.
    assert all(s["mpp"] == 1.0 and s["size_px"] == [512, 512] for r in tubule for s in r["input_spec"]["images"])
    assert all(s["mpp"] == 0.25 and s["size_px"] == [512, 512] for r in pleo for s in r["input_spec"]["images"])

    grading = grading_row(db_session, stage)
    assert grading.tubule_percent == 40.0 and grading.tubule_score == 2
    assert grading.pleo_score == 3
    # Two counted figures in one HPF of radius 262 µm: 9.3 per mm², score 3 (pipeline/scoring.py).
    assert grading.mitotic_score == 3
    assert grading.nottingham_sum == 8 and grading.grade == 3
    assert grading.histologic_type == "ILC" and grading.type_confirmed_by == "unconfirmed"
    assert grading.overrides == {}

    machine = grading.machine
    assert machine == output_json(stage)
    assert machine["schema"] == MACHINE_SCHEMA and machine["flags"] == []
    assert machine["frame"]["hotspot_ids"] == ["hs_01"]
    assert machine["histotype"]["type"] == "ILC" and machine["histotype"]["record_id"] == str(histotype["id"])
    assert machine["tubule"]["estimator"] == "T1:gemini_referee@tubule@v1"
    assert machine["mitotic"] == {"score": 3, "count_total": 2, "n_hpf": 1, "area_mm2": 0.216, "per_mm2": 9.27,
                                  "flags": ["hpf_count_lt_10"]}
    assert machine["result"]["flags"] == ["near_grade_boundary", "hpf_count_lt_10"] and machine["needs_human"] is False
    record_ids = {str(r["id"]) for r in tubule + pleo}
    for s in machine["tubule"]["samples"] + machine["pleomorphism"]["fields"]:
        assert s["record_id"] in record_ids and s["hotspot_id"] == "hs_01" and s["tumor_area_um2"] > 0
    for f in machine["pleomorphism"]["fields"]:
        assert f["nuclei"] is None
        assert blob_exists(settings.GCS_ARTIFACTS_BUCKET, sample_blob(str(stage.case_id), "pleo", f["id"]))
    assert all(blob_exists(settings.GCS_ARTIFACTS_BUCKET, sample_blob(str(stage.case_id), "tubule", s["id"]))
               for s in machine["tubule"]["samples"])


def test_sampling_needs_a_confirmed_triage(db_session):
    stage, _ = seed(db_session, triage_status="awaiting_review")
    with pytest.raises(SamplingFrameEmptyError, match="confirmed Stage 3"):
        run_grading(stage, db_session, make_runtime(stage, {"vertex_genai": FakeAdapter(then=answer_by_schema)}, config=small_config()))


def test_sampling_needs_the_stage_3_tiles(db_session):
    stage, _ = seed(db_session, with_tiles=False)
    with pytest.raises(TriageOutputMissingError, match="tiles.parquet"):
        run_grading(stage, db_session, make_runtime(stage, {"vertex_genai": FakeAdapter(then=answer_by_schema)}, config=small_config()))


def test_no_stage_4_hpf_means_no_mitotic_score_and_no_grade(db_session, monkeypatch):
    stage, raw_uri = seed(db_session, with_hpfs=False)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    run_grading(stage, db_session, make_runtime(stage, {"vertex_genai": FakeAdapter(then=answer_by_schema)}, config=small_config()))
    grading = grading_row(db_session, stage)
    assert grading.mitotic_score is None and grading.grade is None and grading.nottingham_sum is None
    assert grading.tubule_score == 2 and grading.pleo_score == 3
    assert "needs_human" in grading.machine["result"]["flags"] and grading.machine["needs_human"] is True


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
    assert output["histotype"] is None and "needs_human" in output["result"]["flags"]
    assert [r["task"] for r in log.pending() if r["producer_kind"] == "fallback"] == ["histotype"]


def test_failed_field_estimates_are_left_out_of_the_aggregate(db_session, monkeypatch):
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
    failed = [f for f in output["pleomorphism"]["fields"] if f["estimate"] is None]
    assert len(failed) == 1 and failed[0]["rationale"] is None
    assert "needs_human" in output["result"]["flags"]
    # The remaining fields still grade: no default 2 was voted in for the failed one.
    assert grading_row(db_session, stage).pleo_score == 3


def test_unreadable_slide_fails_the_stage(db_session, monkeypatch):
    import openslide

    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    monkeypatch.setattr(openslide, "OpenSlide", lambda path: (_ for _ in ()).throw(openslide.OpenSlideError("corrupt")))
    with pytest.raises(SlideReadError, match="corrupt"):
        run_grading(stage, db_session, make_runtime(stage, {"vertex_genai": FakeAdapter(then=answer_by_schema)}, config=small_config()))
