"""Mitosis stage on the model gateway (SPEC-01 §3.4, §3.9; SPEC-06 §5.1-5.8, §9; WP-2.3c, WP-7.2, WP-7.6a).

KongNet is a fake endpoint behind the real VertexEndpointAdapter (kongnet_midog_v2 codec,
raw predict, pinned weights); the referee is a fake Gemini returning strict MitosisVerdict JSON.
"""
import json
import math
import threading
import uuid
from types import SimpleNamespace

import numpy as np
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
from tests.fakes.stage2 import seed_stage2
from tests.test_mitosis_gate import save_tumor_mask
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


def configured(referee=True):
    """The repo config with the detector endpoint set (tests have no VERTEX_MITOSIS_ENDPOINT_ID).

    The referee is on by default here so its path stays covered; production has it off.
    """
    config = get_pipeline_config()
    registry = config.models
    kongnet = registry.models["kongnet_det_midog_1"].model_copy(update={"endpoint_id": "456"})
    mitosis = config.mitosis.model_copy(update={"referee": config.mitosis.referee.model_copy(update={"enabled": referee})})
    return config.model_copy(update={
        "models": registry.model_copy(update={"models": {**registry.models, "kongnet_det_midog_1": kongnet}}),
        "mitosis": mitosis,
    })


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def seed(db_session, mpp=MPP, tumor=None):
    case_id, slide_id, exec_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    raw_uri = f"gs://{settings.GCS_RAW_BUCKET}/cases/{case_id}/{slide_id}.svs"
    stage = StageExecution(id=exec_id, case_id=case_id, stage="mitosis", attempt=1, status="running")
    db_session.add_all([
        Case(id=case_id, created_by="mitosis_test"),
        Slide(id=slide_id, case_id=case_id, gcs_uri_original=raw_uri, mpp_x=mpp, mpp_y=mpp,
              width_px=SIDE_PX, height_px=SIDE_PX),
        stage,
        Hotspot(id="hs_01", case_id=case_id, stage_execution_id=exec_id, polygon_um=HOTSPOT, area_mm2=0.196,
                center_um=[1000.0, 1000.0], hpf_diameter_um=500.0, window_um=600.0, rank=1,
                source="model", excluded=False),
    ])
    db_session.commit()
    seed_stage2(db_session, case_id, slide_id, SIDE_PX * MPP, SIDE_PX * MPP)  # the mask spans the section, whatever the scan's mpp
    # The triage tumour mask on 224 µm tiles: all tumour unless the test gives one.
    n_tiles = int(np.ceil(SIDE_PX * MPP / 224.0))
    save_tumor_mask(case_id, np.ones((n_tiles, n_tiles), dtype=bool) if tumor is None else tumor)
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
    detect_ids, referee_ids = {str(r["id"]) for r in detects}, {str(r["id"]) for r in referees}
    by_verdict = {}
    for det in found:
        by_verdict.setdefault(det.vlm["verdict"], set()).add(det.final_decision)
        assert det.vlm["rule_override"] is False and det.p_b is None and det.in_tumor is True
        assert det.decision_path == "A" and det.review_label is None and det.p_a >= 0.75
        assert det.record_ids[0] in detect_ids and det.record_ids[1:] == [det.record_ids[1]] and det.record_ids[1] in referee_ids
    # EQUIVOCAL is never a mitosis (SPEC-06 §5.6) and never counted.
    assert by_verdict == {"MITOTIC_FIGURE": {"mitosis"}, "NOT_MITOTIC_FIGURE": {"not_mitosis"}, "EQUIVOCAL": {"equivocal"}}
    assert all(bool(d.counted) == (d.final_decision == "mitosis") for d in found)

    output = json.loads(download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{stage.case_id}/mitosis/output.json"))
    for cand in output["candidates"]:
        assert cand["vlm"]["verdict"] in VERDICTS
        assert set(cand["record_ids"]) <= detect_ids | referee_ids
    # Only referee-confirmed figures are counted.
    assert output["summary"]["count_total"] <= sum(1 for d in found if d.counted)


def test_detector_outage_fails_the_stage_without_detections(db_session, monkeypatch):
    from google.api_core.exceptions import ServiceUnavailable

    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    with pytest.raises(ModelUnavailableError) as raised:
        run_mitosis(stage, db_session, runtime_for(stage, endpoint=KongNetEndpoint(failure=ServiceUnavailable("503"))))
    assert raised.value.task == "mitosis_detect"
    assert detections(db_session, stage) == []


def detector_specs(log):
    return [spec for row in log.pending() if row["task"] == "mitosis_detect" for spec in row["input_spec"]["images"]]


def mitosis_output(stage) -> dict:
    return json.loads(download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{stage.case_id}/mitosis/output.json"))


def test_a_20x_slide_is_upsampled_to_the_detector_resolution_and_reported(db_session, monkeypatch):
    """SPEC-04 AC6: KongNet gets 512 px patches at 0.25 µm/px from a 0.5 µm/px scan, and the slice is recorded."""
    stage, raw_uri = seed(db_session, mpp=0.5)
    slide = install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX // 2, SIDE_PX // 2), raw_uri)
    endpoint, log = KongNetEndpoint(), DecisionLog()

    run_mitosis(stage, db_session, runtime_for(stage, endpoint=endpoint, log=log))

    specs = detector_specs(log)
    assert specs and all(s["mpp"] == 0.25 and s["size_px"] == [512, 512] for s in specs)
    assert endpoint.calls  # the detector ran
    output = mitosis_output(stage)
    assert (output["native_mpp"], output["detector_upsampled"]) == (0.5, True)
    assert set(slide.levels_read) == {0}  # the only level is coarser than the request


def test_a_40x_slide_is_not_reported_as_upsampled(db_session, monkeypatch):
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    run_mitosis(stage, db_session, runtime_for(stage))
    output = mitosis_output(stage)
    assert (output["native_mpp"], output["detector_upsampled"]) == (0.25, False)


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
    assert found and all(d.final_decision == "equivocal" and d.vlm is None and not d.counted for d in found)
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


def test_a_detector_answering_with_other_weights_fails_the_stage(db_session, monkeypatch):
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    with pytest.raises(ModelCallError, match="the registry pins"):
        run_mitosis(stage, db_session, runtime_for(stage, endpoint=KongNetEndpoint(weights="0" * 64)))
    assert detections(db_session, stage) == []


def test_with_the_referee_off_the_detector_decides(db_session, monkeypatch):
    """SPEC-06 arm A1 (production since the MIDOG++ baseline): no VLM call, candidates >= det_threshold count."""
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    referee, log = FakeAdapter(), DecisionLog()  # any call fails the test
    runtime = runtime_for(stage, referee=referee, config=configured(referee=False), log=log)

    _, model_versions = run_mitosis(stage, db_session, runtime)

    assert referee.calls == [] and {r["task"] for r in log.pending()} == {"mitosis_detect"}
    assert list(model_versions) == ["kongnet_det_midog_1"]
    found = detections(db_session, stage)
    detect_ids = {str(r["id"]) for r in log.pending()}
    for d in found:
        assert (d.final_decision, d.decision_path, d.in_tumor, d.review_label, d.vlm, d.p_b) == ("mitosis", "A", True, None, None, None)
        assert d.p_a >= get_pipeline_config().mitosis.detector.det_threshold and d.counted
        assert len(d.record_ids) == 1 and d.record_ids[0] in detect_ids
    assert found
    assert not get_pipeline_config().mitosis.referee.enabled


def baseline_run(db_session, monkeypatch):
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    log = DecisionLog()
    run_mitosis(stage, db_session, runtime_for(stage, config=configured(referee=False), log=log))
    return stage, log


def test_referee_images_are_not_read_while_the_referee_is_off(db_session, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("referee images read with the referee off")

    monkeypatch.setattr("worker.mitosis.mitosis_referee_images", refuse)
    stage, _ = baseline_run(db_session, monkeypatch)
    assert detections(db_session, stage)


def test_every_candidate_gets_the_contract_crop_and_context_in_raw_colour(db_session, monkeypatch):
    """64 µm at 0.25 µm/px and 256 µm at 1.0 µm/px, both 256 px PNG (contract mitosis_v6)."""
    from PIL import Image
    import io

    stage, _ = baseline_run(db_session, monkeypatch)
    found = detections(db_session, stage)
    assert found
    for det in found:
        for kind in ("crop", "context"):
            data = download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{stage.case_id}/mitosis/crops/{det.id}_{kind}.png")
            image = Image.open(io.BytesIO(data))
            assert image.format == "PNG" and image.size == (256, 256)


def test_the_count_is_a_decision_record_linked_to_the_detections(db_session, monkeypatch):
    """SPEC-06 AC8: one mitosis_count record per run, naming the counted candidates and their detect records."""
    from app.models.decision_record import DecisionRecord

    stage, log = baseline_run(db_session, monkeypatch)
    db_session.flush()
    records = db_session.scalars(select(DecisionRecord).where(DecisionRecord.task == "mitosis_count")).all()
    assert len(records) == 1
    record = records[0]
    found = detections(db_session, stage)
    counted = [d for d in found if d.counted]
    assert record.input_spec["counted_ids"] == sorted(d.id for d in counted)
    assert set(record.input_spec["parent_record_ids"]) == {rid for d in counted for rid in d.record_ids}
    assert set(record.input_spec["parent_record_ids"]) <= {str(r["id"]) for r in log.pending() if r["task"] == "mitosis_detect"}
    assert record.params == {"thresholds": get_pipeline_config().mitosis.scoring.thresholds.model_dump()}
    output = mitosis_output(stage)
    assert record.output == output["summary"]
    assert record.producer_kind == "heuristic" and record.stage_execution_id == stage.id


def test_hpfs_are_the_confirmed_circles_and_report_coverage(db_session, monkeypatch):
    from app.models import HpfSite

    stage, _ = baseline_run(db_session, monkeypatch)
    hpfs = db_session.scalars(select(HpfSite).where(HpfSite.case_id == stage.case_id)).all()
    hs = get_pipeline_config().specimen_profiles.profiles["resection"].hotspots
    assert len(hpfs) == 1
    (hpf,) = hpfs
    # The HPF is the confirmed site's circle exactly, whatever the figures do (D22).
    assert hpf.center_um == [1000.0, 1000.0] and hpf.radius_um == hs.hpf_radius_um
    assert hpf.tissue_coverage >= 0.7 and hpf.tumor_fraction == 1.0
    output = mitosis_output(stage)
    summary = output["summary"]
    assert summary["n_hpf"] == 1 and summary["flags"] == ["hpf_count_lt_10"] and summary["hpf_target"] == hs.k_max
    assert output["hpfs"][0]["hotspot_id"] == "hs_01" and output["hpfs"][0]["frame_um"] == HOTSPOT
    assert all("hpf_seq" in c for c in output["candidates"])
    assert all((c["hpf_seq"] == 1) == (math.dist(c["centroid_um"], (1000.0, 1000.0)) <= hs.hpf_radius_um)
               for c in output["candidates"])


def test_pathologist_decisions_survive_a_rerun(db_session, monkeypatch):
    """Rows with a review_label and pathologist-added figures are kept; a new candidate on top of one is suppressed."""
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    first = runtime_for(stage, config=configured(referee=False))
    run_mitosis(stage, db_session, first)
    found = detections(db_session, stage)
    reviewed = found[0]
    reviewed.review_label = "not_mitosis"
    db_session.add(Detection(id="m_user_ab12cd34", case_id=stage.case_id, centroid_um=[1000.0, 1000.0], p_a=None,
                             final_decision="mitosis", decision_path="human", review_label="mitosis"))
    db_session.commit()

    stage.status = "running"
    run_mitosis(stage, db_session, runtime_for(stage, config=configured(referee=False)))

    after = {d.id: d for d in detections(db_session, stage)}
    assert after[reviewed.id].review_label == "not_mitosis" and not after[reviewed.id].counted
    assert after["m_user_ab12cd34"].decision_path == "human" and after["m_user_ab12cd34"].counted
    others = [d for d in after.values() if d.id not in (reviewed.id, "m_user_ab12cd34")]
    assert all(((d.centroid_um[0] - reviewed.centroid_um[0]) ** 2 + (d.centroid_um[1] - reviewed.centroid_um[1]) ** 2) ** 0.5 >= 7.5
               for d in others)
    assert len(after) == len(set(after))


def test_candidates_outside_the_dilated_tumour_mask_are_not_counted(db_session, monkeypatch):
    """SPEC-06 §5.5: with tumour only in the top-left tiles, candidates in the hotspot (around 1 mm) are in stroma."""
    tumor = np.zeros((9, 9), dtype=bool)
    tumor[0, 0] = True
    stage, raw_uri = seed(db_session, tumor=tumor)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    run_mitosis(stage, db_session, runtime_for(stage, config=configured(referee=False)))
    found = detections(db_session, stage)
    assert found and all(d.in_tumor is False and d.final_decision == "mitosis" and not d.counted for d in found)
    output = mitosis_output(stage)
    assert output["tumor_gate"]["applied"] is True and output["tumor_gate"]["in_situ_exclusion"] is False
    # The circle is still the HPF (nothing is filtered at Stage 4); it holds no counted figure.
    assert len(output["hpfs"]) == 1 and output["hpfs"][0]["tumor_fraction"] == 0.0
    assert output["summary"]["count_total"] == 0 and output["summary"]["mitotic_score"] == 1


def test_a_clinical_run_refuses_a_disabled_gate(db_session, monkeypatch):
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    config = configured(referee=False)
    off = config.mitosis.tumor_gate.model_copy(update={"enabled": False})
    config = config.model_copy(update={"mitosis": config.mitosis.model_copy(update={"tumor_gate": off})})
    with pytest.raises(ValueError, match="eval ablation"):
        run_mitosis(stage, db_session, runtime_for(stage, config=config))


def test_a_case_triaged_without_a_tumour_mask_fails(db_session, monkeypatch):
    from app.core.gcs import delete_blob
    from pipeline.mitosis_gate import TumorMaskMissingError, tumor_mask_blob_names

    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    for name in tumor_mask_blob_names(stage.case_id):
        delete_blob(settings.GCS_ARTIFACTS_BUCKET, name)
    with pytest.raises(TumorMaskMissingError):
        run_mitosis(stage, db_session, runtime_for(stage, config=configured(referee=False)))


def test_a_pinned_site_always_becomes_an_hpf_after_the_model_sites(db_session, monkeypatch):
    """D22: HPF seq follows the model sites by rank, then the pinned sites in id order; the centres are the sites' own."""
    stage, raw_uri = seed(db_session)
    frame = [[1200.0, 1200.0], [1800.0, 1200.0], [1800.0, 1800.0], [1200.0, 1800.0], [1200.0, 1200.0]]
    db_session.add(Hotspot(id="hs_u_1", case_id=stage.case_id, stage_execution_id=stage.id, polygon_um=frame, area_mm2=0.196,
                           center_um=[1500.0, 1500.0], hpf_diameter_um=500.0, window_um=600.0, rank=None,
                           source="pathologist_added", excluded=False))
    db_session.commit()
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    run_mitosis(stage, db_session, runtime_for(stage, config=configured(referee=False)))
    hpfs = mitosis_output(stage)["hpfs"]
    assert [(h["seq"], h["hotspot_id"], h["center_um"]) for h in hpfs] == [
        (1, "hs_01", [1000.0, 1000.0]), (2, "hs_u_1", [1500.0, 1500.0])]
    assert hpfs[1]["source"] == "pathologist" and hpfs[1]["frame_um"] == frame


def test_a_site_confirmed_before_hpf_sites_is_refused_not_guessed(db_session, monkeypatch):
    from pipeline.hpf import SiteWithoutCentreError

    stage, raw_uri = seed(db_session)
    db_session.get(Hotspot, ("hs_01", stage.case_id)).center_um = None
    db_session.commit()
    install_fake_slide(monkeypatch, FakeOpenSlide(SIDE_PX, SIDE_PX), raw_uri)
    with pytest.raises(SiteWithoutCentreError, match="run triage again"):
        run_mitosis(stage, db_session, runtime_for(stage, config=configured(referee=False)))
