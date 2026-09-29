"""Mitosis stage on the model gateway (SPEC-01 §3.4, §3.9; SPEC-06 §5.1-5.2, §9; WP-2.3c, WP-7.2).

KongNet is a fake endpoint behind the real VertexEndpointAdapter (kongnet_midog_v2 codec,
raw predict, pinned weights); the referee is a fake Gemini returning strict MitosisVerdict JSON.
"""
import json
import threading
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.db import Base
from app.core.gcs import download_blob_as_bytes
from app.core.pipeline_config import get_pipeline_config
from app.inference.adapters.base import TransientCallError
from app.inference.adapters.vertex_endpoint import VertexEndpointAdapter
from app.inference.errors import ModelCallError, ModelUnavailableError, SchemaInvalidError
from app.inference.records import DecisionLog
from app.models import Case, Detection, Hotspot, Slide, StageExecution
from pipeline.errors import SlideReadError
from tests.fakes.gateway import FakeAdapter, json_text
from tests.fakes.runtime import make_runtime
from tests.fakes.slide import FakeOpenSlide, install_fake_slide
from worker.mitosis import run_mitosis

SIDE_PX, MPP = 8000, 0.25
HOTSPOT = [[700.0, 700.0], [1300.0, 700.0], [1300.0, 1300.0], [700.0, 1300.0], [700.0, 700.0]]
VERDICTS = ["MITOTIC_FIGURE", "NOT_MITOTIC_FIGURE", "EQUIVOCAL"]


def verdict(name):
    return {
        "verdict": name,
        "criteria": {"membrane_absent": name == "MITOTIC_FIGURE", "condensed_chromosome_projections": True,
                     "phase": "metaphase" if name == "MITOTIC_FIGURE" else "none", "neoplastic_cell": True},
        "mimic": "none" if name == "MITOTIC_FIGURE" else "pyknotic_nucleus",
        "rationale": f"fake {name.lower()}",
    }


class KongNetEndpoint:
    """The v2 service over raw predict: in each request's first tile, one detection above
    det_threshold and one between min_prob and det_threshold; answers name the pinned weights.

    The position shifts per call so candidates fall on different parts of the procedural
    slide; identical crops would be served from the gateway cache.
    """

    def __init__(self, failure=None, weights=None):
        self.failure = failure
        self.weights = weights
        self.calls = []
        self._lock = threading.Lock()

    def raw_predict(self, body, headers=None, timeout=None):
        sent = json.loads(body)
        with self._lock:  # the worker sweeps tiles on several threads
            self.calls.append(sent)
            shift = 13.0 * (len(self.calls) % 20)
        if self.failure is not None:
            raise self.failure
        predictions = [{"points": [], "error": None} for _ in sent["instances"]]
        predictions[0]["points"] = [{"x": 200.0 + shift, "y": 256.0, "prob": 0.9}, {"x": 100.0, "y": 100.0, "prob": 0.2}]
        weights = self.weights or get_pipeline_config().models.models["kongnet_det_midog_1"].weights_sha256
        payload = {"predictions": predictions, "model_sha256": weights}
        return SimpleNamespace(status_code=200, json=lambda: payload, text=json.dumps(payload))


def cycling_referee():
    calls = {"n": 0}

    def answer(request):
        name = VERDICTS[calls["n"] % len(VERDICTS)]
        calls["n"] += 1
        return json_text(verdict(name))

    return answer


def configured():
    """The repo config with the detector endpoint set (tests have no VERTEX_MITOSIS_ENDPOINT_ID)."""
    config = get_pipeline_config()
    registry = config.models
    kongnet = registry.models["kongnet_det_midog_1"].model_copy(update={"endpoint_id": "456"})
    return config.model_copy(update={"models": registry.model_copy(update={"models": {**registry.models, "kongnet_det_midog_1": kongnet}})})


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def seed(db_session, mpp=MPP):
    case_id, slide_id, exec_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    raw_uri = f"gs://{settings.GCS_RAW_BUCKET}/cases/{case_id}/{slide_id}.svs"
    stage = StageExecution(id=exec_id, case_id=case_id, stage="mitosis", attempt=1, status="running")
    db_session.add_all([
        Case(id=case_id, created_by="mitosis_test"),
        Slide(id=slide_id, case_id=case_id, gcs_uri_original=raw_uri, mpp_x=mpp, mpp_y=mpp,
              width_px=SIDE_PX, height_px=SIDE_PX),
        stage,
        Hotspot(id="hs_01", case_id=case_id, stage_execution_id=exec_id, polygon_um=HOTSPOT, area_mm2=0.36,
                prob_mean=0.9, prob_max=0.95, source="model", excluded=False),
    ])
    db_session.commit()
    return stage, raw_uri


def runtime_for(stage, endpoint=None, referee=None, config=None, log=None):
    adapters = {
        "vertex_endpoint_raw_predict": VertexEndpointAdapter("p", endpoint_factory=lambda *args: endpoint or KongNetEndpoint()),
        "vertex_genai": referee or FakeAdapter(then=cycling_referee()),
    }
    return make_runtime(stage, adapters, config=config or configured(), log=log)


def detections(db_session, stage):
    return db_session.scalars(select(Detection).where(Detection.case_id == stage.case_id).order_by(Detection.id)).all()


def test_mitosis_runs_on_the_gateway_and_labels_come_from_the_referee(db_session, monkeypatch):
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    endpoint, log = KongNetEndpoint(), DecisionLog()

    output_ref, model_versions = run_mitosis(stage, db_session, runtime_for(stage, endpoint=endpoint, log=log))

    registry = get_pipeline_config().models
    assert stage.status == "awaiting_review"
    assert model_versions == {
        "kongnet_det_midog_1": registry.version_of("kongnet_det_midog_1"),
        "gemini_referee": registry.version_of("gemini_referee"),
    }
    rows = log.pending()
    assert {r["status"] for r in rows} == {"ok"}
    detects = [r for r in rows if r["task"] == "mitosis_detect"]
    referees = [r for r in rows if r["task"] == "mitosis_referee"]

    # Tiles go out as 512 px PNG patches at the slide's 0.25 µm/px, within the registry limits,
    # asking for every candidate down to min_prob (v2 contract).
    limits = registry.models["kongnet_det_midog_1"].limits
    assert detects and all(len(r["input_spec"]["images"]) <= limits.max_batch for r in detects)
    assert all(s == {"mpp": 0.25, "size_px": [512, 512], "color": "raw", "format": "png", "stain_profile_id": None}
               for r in detects for s in r["input_spec"]["images"])
    assert all(r["params"] == {"min_prob": 0.01} for r in detects)
    assert all(call["parameters"] == {"min_prob": 0.01} for call in endpoint.calls)
    assert all(set(inst) == {"image_png_b64", "mpp"} and inst["mpp"] == 0.25 for call in endpoint.calls for inst in call["instances"])

    # Raw Stage A keeps the 0.2 detections (>= min_prob) for offline threshold sweeps.
    stage_a = json.loads(download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{stage.case_id}/mitosis/stage_a.json"))
    assert stage_a["weights_sha256"] == registry.models["kongnet_det_midog_1"].weights_sha256
    assert stage_a["min_prob"] == 0.01 and {p["prob"] for p in stage_a["points"]} == {0.9, 0.2}

    # One candidate per request survives (the 0.2 detection is below det_threshold); each is refereed once.
    found = detections(db_session, stage)
    assert found and len(referees) == len(found)
    assert all(len(r["input_spec"]["images"]) == 2 and r["prompt_id"] == "mitosis_confirmation@v1.md" for r in referees)
    focus, context = referees[0]["input_spec"]["images"]
    assert (focus["size_px"], focus["format"], focus["mpp"]) == ([128, 128], "png", 0.25)
    assert (context["size_px"], context["format"]) == ([512, 512], "jpeg") and abs(context["mpp"] - 1.0) < 0.01

    assert not any(r["cache_hit"] for r in referees), "candidates must be distinct for this test"
    by_verdict = {}
    for det in found:
        by_verdict.setdefault(det.medgemma_verdict, set()).add(det.label)
        assert det.label_source == "referee:gemini_referee"
        assert det.ver_conf is None and det.medgemma_confidence is None
    assert by_verdict == {"MITOTIC_FIGURE": {"mitosis"}, "NOT_MITOTIC_FIGURE": {"not_mitosis"}, "EQUIVOCAL": {"unreviewed"}}

    output = json.loads(download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{stage.case_id}/mitosis/output.json"))
    record_ids = {str(r["id"]) for r in referees}
    for cand in output["candidates"]:
        assert cand["referee_record_id"] in record_ids
        assert cand["vlm"]["verdict"] in VERDICTS and cand["vlm"]["rule_override"] is False
        assert cand["det_record_id"] in {str(r["id"]) for r in detects}
    # Only referee-confirmed figures are counted.
    counted = sum(1 for d in found if d.label == "mitosis")
    assert output["summary"]["count_total"] <= counted


def test_detector_outage_fails_the_stage_without_detections(db_session, monkeypatch):
    from google.api_core.exceptions import ServiceUnavailable

    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    with pytest.raises(ModelUnavailableError) as raised:
        run_mitosis(stage, db_session, runtime_for(stage, endpoint=KongNetEndpoint(failure=ServiceUnavailable("503"))))
    assert raised.value.task == "mitosis_detect"
    assert detections(db_session, stage) == []


def test_20x_slide_is_resampled_to_the_detector_resolution(db_session, monkeypatch):
    """SPEC-06 §5.1 / AC5: KongNet only ever receives 0.25 µm/px; a 0.5 µm/px slide is resampled."""
    stage, raw_uri = seed(db_session, mpp=0.5)
    slide = install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX // 2, SIDE_PX // 2), raw_uri)
    reads = []
    read_region = slide.read_region

    def recording(location, level, size):
        reads.append(size)
        return read_region(location, level, size)

    slide.read_region = recording
    endpoint = KongNetEndpoint()
    run_mitosis(stage, db_session, runtime_for(stage, endpoint=endpoint))

    assert endpoint.calls and all(inst["mpp"] == 0.25 for call in endpoint.calls for inst in call["instances"])
    # Each 512 px tile at 0.25 µm/px covers 128 µm, a 256 px window of the 20x slide.
    assert (256, 256) in reads
    assert detections(db_session, stage)


def test_a_detector_answering_with_other_weights_fails_the_stage(db_session, monkeypatch):
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    with pytest.raises(ModelCallError, match="the registry pins"):
        run_mitosis(stage, db_session, runtime_for(stage, endpoint=KongNetEndpoint(weights="0" * 64)))
    assert detections(db_session, stage) == []


def test_v5_shaped_referee_answer_fails_the_stage(db_session, monkeypatch):
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    v5 = FakeAdapter(then=json_text({"verdict": "CONFIRMED", "envelope_dissolved": True, "spiculation_detected": True,
                                     "confidence": "high", "rationale": "v5 shape"}))
    with pytest.raises(SchemaInvalidError):
        run_mitosis(stage, db_session, runtime_for(stage, referee=v5))


def test_allowed_referee_outage_leaves_candidates_unreviewed(db_session, monkeypatch):
    config = configured()
    policy = config.fallbacks.model_validate(
        {"fallbacks": [{"task": "mitosis_referee", "on": ["ModelUnavailableError"], "to": None}]}
    )
    config = config.model_copy(update={"fallbacks": policy})
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    log = DecisionLog()

    run_mitosis(stage, db_session, runtime_for(stage, referee=FakeAdapter(then=TransientCallError("503")), config=config, log=log))

    found = detections(db_session, stage)
    assert found and all(d.label == "unreviewed" and d.label_source == "referee_unavailable" for d in found)
    assert {r["task"] for r in log.pending() if r["producer_kind"] == "fallback"} == {"mitosis_referee"}


def test_unreadable_tile_fails_instead_of_dropping_it(db_session, monkeypatch):
    import openslide

    stage, raw_uri = seed(db_session)
    slide = install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)

    def broken(location, level, size):
        raise openslide.OpenSlideError("JPEG decode failed")

    slide.read_region = broken
    with pytest.raises(SlideReadError, match="JPEG decode failed"):
        run_mitosis(stage, db_session, runtime_for(stage))
