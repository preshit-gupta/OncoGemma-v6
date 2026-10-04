"""SPEC-01 AC3: every former fallback path of §1.1 raises in EVAL.

Each row of the §1.1 table runs its stage in ``run_mode=eval`` with the provider behind the
real adapter answering 503 ServiceUnavailable (or the slide unreadable, for the rows about
synthetic images). The configuration allows every fallback a clinical run could take, so
the stage fails because EVAL fails loud, not because no fallback was configured. Nothing
may stand in for the failed component: no detections, labels, grade or heuristic record.
"""
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from google.api_core.exceptions import ServiceUnavailable
from google.genai import errors as genai_errors
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.fallbacks import FallbackEntry, FallbackPolicy
from app.core.pipeline_config import get_pipeline_config
from app.core.run_context import RunMode
from app.core.tasks import Task
from app.inference import schemas
from app.inference.adapters.local_sklearn import LocalSklearnAdapter
from app.inference.adapters.vertex_endpoint import VertexEndpointAdapter
from app.inference.adapters.vertex_genai import VertexGenAIAdapter
from app.inference.errors import FALLBACK_ELIGIBLE, ModelUnavailableError
from app.inference.records import DecisionLog
from app.models import Case, Detection, Grading, Slide, StageExecution
from pipeline.errors import SlideReadError
from tests import test_grading_worker as grading_t
from tests import test_mitosis_worker as mitosis_t
from tests import test_triage_worker as triage_t
from tests.fakes.gateway import FakeAdapter
from tests.fakes.runtime import make_runtime
from tests.fakes.slide import FakeOpenSlide, install_fake_slide
from worker.grading import run_grading
from worker.mitosis import run_mitosis
from worker.triage import run_triage

ENDPOINT_PROVIDERS = ("vertex_endpoint_predict", "vertex_endpoint_raw_predict")


# --- providers that are down ---------------------------------------------------

class DownEndpoint:
    """A Vertex endpoint answering 503 to predict and raw_predict, recording each request."""

    def __init__(self):
        self.instances = []

    def predict(self, instances, parameters=None, timeout=None):
        self.instances.append(instances)
        raise ServiceUnavailable("503 Service Unavailable")

    def raw_predict(self, body, headers=None, timeout=None):
        self.instances.append(json.loads(body)["instances"])
        raise ServiceUnavailable("503 Service Unavailable")


def down_endpoints(endpoint: DownEndpoint) -> VertexEndpointAdapter:
    return VertexEndpointAdapter("oncogemma-test", endpoint_factory=lambda *args: endpoint)


class GeminiModels:
    """Gemini answering 503 for the schemas in ``down`` and the strict test answer otherwise."""

    def __init__(self, down: set, answers: dict):
        self.down = {model.__name__ for model in down}
        self.answers = {model.__name__: answer for model, answer in answers.items()}
        self.requests = []

    def generate_content(self, **kwargs):
        self.requests.append(kwargs)
        schema = kwargs["config"].response_json_schema["title"]
        if schema in self.down:
            raise genai_errors.APIError(503, {"error": {"message": "Service Unavailable"}})
        return SimpleNamespace(text=json.dumps(self.answers[schema]))


def gemini(models: GeminiModels) -> VertexGenAIAdapter:
    return VertexGenAIAdapter("oncogemma-test", client_factory=lambda project, region: SimpleNamespace(models=models))


def images_sent(models: GeminiModels) -> list[int]:
    return [sum(1 for part in request["contents"] if part.inline_data is not None) for request in models.requests]


# --- configuration -----------------------------------------------------------------

def permissive(config):
    """Every task may survive every fallback-eligible error, in a clinical run."""
    policy = FallbackPolicy(fallbacks=[FallbackEntry(task=task, on=sorted(FALLBACK_ELIGIBLE), to=None) for task in Task])
    registry = config.models
    endpoints = {
        key: entry.model_copy(update={"endpoint_id": entry.endpoint_id or "123"})
        for key, entry in registry.models.items()
        if entry.provider in ENDPOINT_PROVIDERS
    }
    return config.model_copy(update={
        "fallbacks": policy,
        "models": registry.model_copy(update={"models": {**registry.models, **endpoints}}),
    })


def no_substitutes(log: DecisionLog) -> None:
    kinds = {record["producer_kind"] for record in log.pending()}
    assert not kinds & {"fallback", "heuristic"}, kinds


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


# --- one scenario per failing component --------------------------------------------

def mitosis_with(db_session, monkeypatch, *, endpoint=None, referee_down=False, run_mode=RunMode.EVAL):
    stage, raw_uri = mitosis_t.seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(mitosis_t.SIDE_PX, mitosis_t.SIDE_PX), raw_uri)
    models = GeminiModels(
        down={schemas.MitosisVerdict} if referee_down else set(),
        answers={schemas.MitosisVerdict: mitosis_t.verdict("MITOTIC_FIGURE")},
    )
    adapters = {
        "vertex_endpoint_raw_predict": VertexEndpointAdapter(
            "oncogemma-test", endpoint_factory=lambda *args: endpoint or mitosis_t.KongNetEndpoint()
        ),
        "vertex_genai": gemini(models),
    }
    log = DecisionLog()
    runtime = make_runtime(stage, adapters, config=permissive(mitosis_t.configured()), run_mode=run_mode, log=log)
    return stage, runtime, log, models


def detector_down(db_session, monkeypatch, tmp_path):
    endpoint = DownEndpoint()
    stage, runtime, log, _ = mitosis_with(db_session, monkeypatch, endpoint=endpoint)
    try:
        run_mitosis(stage, db_session, runtime)
    finally:
        assert endpoint.instances, "the detector was never asked"
        assert db_session.scalars(select(Detection).where(Detection.case_id == stage.case_id)).all() == []
        no_substitutes(log)


def mitosis_referee_down(db_session, monkeypatch, tmp_path):
    stage, runtime, log, models = mitosis_with(db_session, monkeypatch, referee_down=True)
    try:
        run_mitosis(stage, db_session, runtime)
    finally:
        # Every attempt carried both crops: none was retried without its images.
        assert models.requests and all(count == 2 for count in images_sent(models))
        assert db_session.scalars(select(Detection).where(Detection.case_id == stage.case_id)).all() == []
        no_substitutes(log)


def triage_with(db_session, monkeypatch, tmp_path, **overrides):
    stage, raw_uri = triage_t.seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(triage_t.WIDTH_PX, triage_t.HEIGHT_PX), raw_uri)
    log = DecisionLog()
    runtime = make_runtime(
        stage, triage_t.adapters(**overrides), config=permissive(get_pipeline_config()), run_mode=RunMode.EVAL, log=log
    )
    return stage, runtime, log


def tumour_referee_down(db_session, monkeypatch, tmp_path):
    endpoint = DownEndpoint()
    stage, runtime, log = triage_with(db_session, monkeypatch, tmp_path, referee=down_endpoints(endpoint))
    try:
        run_triage(stage, db_session, runtime)
    finally:
        assert endpoint.instances, "the tumour referee was never asked"
        assert all(instances for instances in endpoint.instances)
        no_substitutes(log)


def embeddings_down(db_session, monkeypatch, tmp_path):
    endpoint = DownEndpoint()
    stage, runtime, log = triage_with(db_session, monkeypatch, tmp_path, pf=down_endpoints(endpoint))
    try:
        run_triage(stage, db_session, runtime)
    finally:
        assert endpoint.instances, "Path Foundation was never asked"
        no_substitutes(log)


def probe_missing(db_session, monkeypatch, tmp_path):
    stage, runtime, log = triage_with(db_session, monkeypatch, tmp_path, sklearn=LocalSklearnAdapter(root=tmp_path))
    try:
        run_triage(stage, db_session, runtime)
    finally:
        assert not (tmp_path / "models").exists(), "a probe was trained in place of the artifact"
        no_substitutes(log)


def triage_region_unreadable(db_session, monkeypatch, tmp_path):
    import openslide

    stage, raw_uri = triage_t.seed(db_session)
    slide = install_fake_slide(monkeypatch, FakeOpenSlide(triage_t.WIDTH_PX, triage_t.HEIGHT_PX), raw_uri)

    def broken(location, level, size):
        raise openslide.OpenSlideError("TIFFRGBAImageGet failed")

    slide.read_region = broken
    referee = FakeAdapter()  # any call fails the test: no synthetic crop may reach it
    log = DecisionLog()
    runtime = make_runtime(
        stage, triage_t.adapters(referee=referee), config=permissive(get_pipeline_config()), run_mode=RunMode.EVAL, log=log
    )
    try:
        run_triage(stage, db_session, runtime)
    finally:
        assert referee.calls == []
        no_substitutes(log)


def grading_with_down(*down):
    def scenario(db_session, monkeypatch, tmp_path):
        stage, raw_uri = grading_t.seed(db_session)
        install_fake_slide(monkeypatch, FakeOpenSlide(grading_t.SIDE_PX, grading_t.SIDE_PX), raw_uri)
        models = GeminiModels(down=set(down), answers=grading_t.ANSWERS)
        log = DecisionLog()
        runtime = make_runtime(
            stage, grading_t.with_verifier(gemini(models)), config=permissive(grading_t.small_config()), run_mode=RunMode.EVAL, log=log
        )
        try:
            run_grading(stage, db_session, runtime)
        finally:
            assert models.requests and all(count >= 1 for count in images_sent(models))
            assert db_session.scalars(select(Grading).where(Grading.case_id == stage.case_id)).first() is None
            no_substitutes(log)

    return scenario


def evidence_unreadable(db_session, monkeypatch, tmp_path):
    """The triage evidence route answers 404 instead of drawing a patch."""
    import app.routers.triage as triage_router

    case_id, slide_id = "case_ac3_unreadable", "slide_ac3_unreadable"
    db_session.add_all([
        Case(id=case_id, created_by="test", status="processing"),
        Slide(id=slide_id, case_id=case_id, gcs_uri_original="gs://raw/missing.svs",
              mpp_x=0.25, mpp_y=0.25, width_px=1000, height_px=1000),
        StageExecution(case_id=case_id, stage="triage", attempt=1, status="awaiting_review"),
    ])
    db_session.commit()
    machine = json.dumps({"hotspots": [{"id": "hs_01", "polygon_um": [[0, 0], [100, 0], [100, 100], [0, 100]]}]})

    def download(bucket, blob):
        if blob.endswith("triage/output.json"):
            return machine.encode()
        raise FileNotFoundError(blob)

    assert not hasattr(triage_router, "generate_synthetic_microscopic_patch")
    with patch("app.routers.triage.download_blob_as_bytes", side_effect=download), \
         patch("pipeline.tiles.extract_patch_from_pyramid", return_value=None), \
         patch("app.core.slide_access.download_blob_to_filename", side_effect=FileNotFoundError("gs://raw/missing.svs")), \
         patch("app.routers.triage.upload_blob_from_bytes") as upload:
        try:
            triage_router.get_hotspot_thumbnail(
                case_id=case_id, hotspot_id="hs_01", mag="40x", stain="orig", db=db_session
            )
        finally:
            upload.assert_not_called()


# --- the §1.1 table --------------------------------------------------------------------

@dataclass(frozen=True)
class Row:
    location: str
    behaviour: str
    scenario: Callable
    raises: type
    match: str = ""


ROWS = [
    Row("pipeline/detect.py:264-282", "KongNet failure replaced by OD-heuristic candidates",
        detector_down, ModelUnavailableError, "mitosis_detect"),
    Row("worker/mitosis.py:398", "referee label_source named Gemini whoever answered",
        mitosis_referee_down, ModelUnavailableError, "mitosis_referee"),
    Row("pipeline/medgemma.py:1069-1071", "Gemini exception replaced by a morphometric verdict",
        mitosis_referee_down, ModelUnavailableError, "mitosis_referee"),
    Row("pipeline/medgemma.py:361-373", "failed vision call retried without images",
        mitosis_referee_down, ModelUnavailableError, "mitosis_referee"),
    Row("pipeline/medgemma.py:1204,1233-1316", "tumour verification fell back to colour thresholds",
        tumour_referee_down, ModelUnavailableError, "tumor_referee"),
    Row("pipeline/medgemma.py:764-778,846-855", "doer-only results reported as DOER_CONFIRMED, high",
        mitosis_referee_down, ModelUnavailableError, "mitosis_referee"),
    Row("worker/grading.py:495", "tubule failure became tubule_percent=20",
        grading_with_down(schemas.TubuleEstimate), ModelUnavailableError, "tubule_patch"),
    Row("worker/grading.py:507", "pleomorphism failure became pleomorphism_score=2",
        grading_with_down(schemas.PleoScore), ModelUnavailableError, "pleo_field"),
    Row("worker/grading.py:519-528", "histologic-type failure became IDC-NST",
        grading_with_down(schemas.HistotypeVerdict), ModelUnavailableError, "histotype"),
    Row("worker/triage.py:600,706-710", "synthetic pink crops sent to the referee and uploaded",
        triage_region_unreadable, SlideReadError, "TIFFRGBAImageGet failed"),
    Row("app/routers/triage.py:259", "synthetic evidence patch generator",
        evidence_unreadable, HTTPException, ""),
    Row("worker/triage.py:66-68,155-159", "random embeddings when the endpoint is missing",
        embeddings_down, ModelUnavailableError, "pf_embed"),
    Row("pipeline/probe.py:56-62; worker/triage.py:516-517", "mock projection or runtime synthetic probe",
        probe_missing, ModelUnavailableError, "does not exist"),
]


@pytest.mark.parametrize("row", ROWS, ids=[f"{row.location} {row.behaviour}" for row in ROWS])
def test_former_fallback_path_raises_in_eval(row, db_session, monkeypatch, tmp_path):
    with pytest.raises(row.raises) as raised:
        row.scenario(db_session, monkeypatch, tmp_path)
    if isinstance(raised.value, ModelUnavailableError):
        assert row.match == raised.value.task or row.match in str(raised.value), raised.value
    elif isinstance(raised.value, HTTPException):
        assert raised.value.status_code == 404
    else:
        assert row.match in str(raised.value)


def test_the_same_policy_lets_a_clinical_run_survive(db_session, monkeypatch):
    """Control: the permissive policy is real, so the EVAL failures above come from EVAL."""
    stage, runtime, log, _ = mitosis_with(db_session, monkeypatch, referee_down=True, run_mode=RunMode.CLINICAL)
    run_mitosis(stage, db_session, runtime)
    found = db_session.scalars(select(Detection).where(Detection.case_id == stage.case_id)).all()
    # v5 "unreviewed" is v6 final_decision "equivocal" with no review_label (WP-7.6a, migration 0016).
    assert found and all(d.final_decision == "equivocal" and d.review_label is None for d in found)
    assert {r["task"] for r in log.pending() if r["producer_kind"] == "fallback"} == {"mitosis_referee"}
