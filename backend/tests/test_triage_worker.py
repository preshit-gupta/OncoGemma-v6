"""Triage stage on the model gateway (SPEC-01 §3.4, §3.9; WP-2.3b).

Path Foundation and the referee are fakes; the tumour head and its calibrator are the real
local artifacts (models/tumor_head/1.0.0) through LocalSklearnAdapter.
"""
import functools
import hashlib
import json
import uuid
from pathlib import Path

import joblib
import numpy as np
import pytest
from app.core.config import settings
from app.core.db import Base
from app.core.gcs import download_blob_as_bytes
from app.core.pipeline_config import get_pipeline_config
from app.inference.adapters.base import RawResponse, TransientCallError
from app.inference.adapters.local_sklearn import LocalSklearnAdapter
from app.inference.errors import ModelUnavailableError, SchemaInvalidError
from app.inference.records import DecisionLog
from app.models import Case, Slide, StageExecution
from pipeline.errors import SlideReadError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from worker.triage import run_triage

from tests.fakes.gateway import FakeAdapter, InMemoryBlobStore, json_text
from tests.fakes.runtime import make_runtime
from tests.fakes.slide import FakeOpenSlide, install_fake_slide
from tests.fakes.stage2 import seed_stage2

REPO_ROOT = Path(__file__).resolve().parents[2]
WIDTH_PX, HEIGHT_PX, MPP = 2400, 1800, 0.5
EMBEDDING_DIM = 384


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def seed(db_session, **slide_overrides):
    case_id, slide_id = uuid.uuid4(), uuid.uuid4()
    raw_uri = f"gs://{settings.GCS_RAW_BUCKET}/cases/{case_id}/{slide_id}.svs"
    slide_values = {
        "id": slide_id, "case_id": case_id, "gcs_uri_original": raw_uri,
        "mpp_x": MPP, "mpp_y": MPP, "width_px": WIDTH_PX, "height_px": HEIGHT_PX,
        "checksum_sha256": hashlib.sha256(str(slide_id).encode()).hexdigest(),  # ingest records it
    }
    slide_values.update(slide_overrides)
    stage = StageExecution(
        id=uuid.uuid4(), case_id=case_id, stage="triage", attempt=1, status="running",
        input_ref={"slide_id": str(slide_id)},
    )
    db_session.add_all([Case(id=case_id, created_by="triage_test"), Slide(**slide_values), stage])
    db_session.commit()
    seed_stage2(db_session, case_id, slide_id, WIDTH_PX * MPP, HEIGHT_PX * MPP)
    return stage, raw_uri


@functools.lru_cache(maxsize=1)
def tumor_like_embedding() -> np.ndarray:
    """A 384-d vector the real head (models/tumor_head) scores as invasive tumour.

    Random vectors are not tumour to a trained head, so a fake Path Foundation answering noise
    would leave no tumour and no hotspot to referee. This points along the head's tumour direction
    in its z-scored feature space, mapped back to an (L2-normalised) embedding.
    """
    model = joblib.load(REPO_ROOT / get_pipeline_config().models.models["tumor_head"].artifact_uri)
    coef = model.multinomial.coef_
    k = model.positive_index
    direction = coef[k] - np.delete(coef, k, axis=0).mean(axis=0)
    return model.mean_ + model.scale_ * direction / np.linalg.norm(direction) * 3.0


def embed(request):
    """Deterministic 384-d tumour-like vectors, each with a little noise derived from its tile's bytes."""
    rows = []
    base = tumor_like_embedding()
    for image in request.images:
        seed_value = int(hashlib.sha256(image.data).hexdigest()[:8], 16)
        noise = np.random.default_rng(seed_value).standard_normal(EMBEDDING_DIM) * np.linalg.norm(base) * 0.01
        rows.append((base + noise).tolist())
    return RawResponse(data={"embeddings": rows})


def alternating_referee():
    calls = {"n": 0}

    def answer(request):
        calls["n"] += 1
        tumour = calls["n"] % 2 == 1
        return json_text({
            "tumor_present": tumour,
            "lesion_type": "invasive_carcinoma" if tumour else "benign_stroma",
            "rationale": "fake referee",
        })

    return answer


def adapters(pf=None, referee=None, sklearn=None):
    return {
        "vertex_endpoint_raw_predict": pf or FakeAdapter(then=embed),
        "vertex_endpoint_predict": referee or FakeAdapter(then=alternating_referee()),
        "local_sklearn": sklearn or LocalSklearnAdapter(),
    }


def output_json(stage_execution) -> dict:
    return json.loads(download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"cases/{stage_execution.case_id}/triage/output.json"))


def test_triage_runs_on_the_gateway_and_records_every_decision(db_session, monkeypatch):
    stage, raw_uri = seed(db_session)
    slide = install_fake_slide(monkeypatch, FakeOpenSlide(WIDTH_PX, HEIGHT_PX), raw_uri)
    pf = FakeAdapter(then=embed)
    log = DecisionLog()
    runtime = make_runtime(stage, adapters(pf=pf), log=log)

    output_ref, model_versions = run_triage(stage, db_session, runtime)

    config = get_pipeline_config()
    registry = config.models
    assert output_ref.endswith(f"cases/{stage.case_id}/triage/output.json")
    assert stage.status == "awaiting_review"
    assert model_versions == {
        "path_foundation": registry.version_of("path_foundation"),
        "tumor_head": registry.version_of("tumor_head"),
        "tumor_head_calibrator": registry.version_of("tumor_head_calibrator"),
        "medgemma": registry.version_of("medgemma"),
    }
    assert slide.closed

    rows = log.pending()
    by_task = {task: [r for r in rows if r["task"] == task] for task in ("pf_embed", "tumor_head", "tumor_referee")}
    assert {r["status"] for r in rows} == {"ok"} and len(rows) == sum(len(v) for v in by_task.values())

    # Tiles: batches within the registry limits, each tile sent at the contract resolution.
    limits = registry.models["path_foundation"].limits
    sent = [len(request.images) for _, request, _ in pf.calls]
    assert sent and max(sent) <= limits.max_batch
    assert len(by_task["pf_embed"]) == len(pf.calls)
    assert all(r["entity_type"] == "tile_batch" and r["entity_ids_uri"] for r in by_task["pf_embed"])
    tile_specs = [spec for r in by_task["pf_embed"] for spec in r["input_spec"]["images"]]
    assert all(abs(s["mpp"] - 1.0) <= 0.02 and s["size_px"] == [224, 224] for s in tile_specs)

    # One head record and one calibrator record over every tile.
    head, calibrator = by_task["tumor_head"]
    assert head["producer_id"] == "tumor_head" and head["input_spec"]["features"]["shape"] == [sum(sent), 384]
    assert calibrator["producer_id"] == "tumor_head_calibrator"
    assert calibrator["input_spec"]["features"] == {**calibrator["input_spec"]["features"], "producer": "tumor_head", "shape": [sum(sent), 1]}

    # Every candidate was refereed, and each hotspot links to its referee record.
    output = output_json(stage)
    referees = by_task["tumor_referee"]
    assert referees and all(r["prompt_id"] == "tumor_verification@v2.md" for r in referees)
    record_ids = {str(r["id"]) for r in referees}
    assert output["hotspots"]
    for hotspot in output["hotspots"]:
        assert hotspot["referee"]["record_id"] in record_ids
        assert hotspot["referee"]["producer_id"] == "medgemma"
        assert isinstance(hotspot["referee"]["tumor_present"], bool)
    # Confirmed tumour first.
    flags = [h["referee"]["tumor_present"] for h in output["hotspots"]]
    assert flags == sorted(flags, reverse=True)
    assert output["model_versions"] == model_versions
    assert output["audit"]["endpoint_calls_made"] == sum(sent)


def test_triage_writes_tile_scores_heatmap_and_tumor_mask_on_one_grid(db_session, monkeypatch):
    """SPEC-05 §4.3: every tissue tile scored (S3-COV = 1), 1 px per tile, alpha 0 only off tissue."""
    import io

    import pyarrow.parquet as pq
    from PIL import Image

    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(WIDTH_PX, HEIGHT_PX), raw_uri)
    run_triage(stage, db_session, make_runtime(stage, adapters()))

    head_cfg = get_pipeline_config().triage.tumor_head
    output = output_json(stage)
    prefix = f"cases/{stage.case_id}/triage"
    blob = lambda name: download_blob_as_bytes(settings.GCS_ARTIFACTS_BUCKET, f"{prefix}/{name}")
    grid = output["tile_grid"]

    tiles = pq.read_table(io.BytesIO(blob("tiles.parquet")))
    meta = json.loads(tiles.schema.metadata[b"oncogemma.triage_tiles"])
    frame = tiles.to_pandas()
    assert len(frame) == grid["n_tiles"]  # every tissue tile has a probability
    assert meta["classes"][0] == head_cfg.positive_class and meta["head_version"] == "tumor_head@1.0.0"
    p = np.stack(frame["p"].to_numpy())
    np.testing.assert_allclose(p.sum(axis=1), 1.0, atol=1e-5)
    assert ((frame["p_tumor_cal"] >= 0) & (frame["p_tumor_cal"] <= 1)).all()
    assert (frame["is_tumor"] == (frame["p_tumor_cal"] >= head_cfg.threshold)).all()  # no smoothing configured

    heatmap = np.asarray(Image.open(io.BytesIO(blob("heatmap.png"))))
    assert heatmap.shape == (grid["n_rows"], grid["n_cols"], 4)
    tissue = np.zeros((grid["n_rows"], grid["n_cols"]), dtype=bool)
    tissue[frame["j"], frame["i"]] = True
    assert (heatmap[..., 3][tissue] > 0).all() and (heatmap[..., 3][~tissue] == 0).all()
    heatmap_json = json.loads(blob("heatmap.json"))
    assert heatmap_json == output["heatmap"]
    assert heatmap_json == {"tile_um": 224.0, "origin_um": [0.0, 0.0], "nx": grid["n_cols"], "ny": grid["n_rows"],
                            "head_version": "tumor_head@1.0.0", "value": "p_tumor_cal"}

    mask = np.asarray(Image.open(io.BytesIO(blob("tumor_mask.png"))))
    assert mask.shape == (grid["n_rows"], grid["n_cols"])
    assert int((mask == 255).sum()) == int(frame["is_tumor"].sum()) == json.loads(blob("tumor_mask.json"))["n_tumor_tiles"]
    assert output["tumor_threshold"] == head_cfg.threshold


def test_second_run_is_served_from_the_gateway_cache(db_session, monkeypatch):
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(WIDTH_PX, HEIGHT_PX), raw_uri)
    pf, blobs = FakeAdapter(then=embed), InMemoryBlobStore()
    run_triage(stage, db_session, make_runtime(stage, adapters(pf=pf), blobs=blobs))
    calls_after_first = len(pf.calls)
    # Without the tile-embedding cache, the tiles go back to the gateway, which answers from its own cache.
    for path in [p for p in blobs.blobs if p.startswith("embeddings/")]:
        del blobs.blobs[path]

    stage.status = "running"
    log = DecisionLog()
    run_triage(stage, db_session, make_runtime(stage, adapters(pf=pf), blobs=blobs, log=log))

    assert len(pf.calls) == calls_after_first
    embeds = [r for r in log.pending() if r["task"] == "pf_embed"]
    assert embeds and all(r["cache_hit"] for r in embeds)
    assert output_json(stage)["audit"]["endpoint_calls_made"] == 0


def test_every_tissue_tile_is_embedded_and_a_rerun_reads_the_slide_embedding_cache(db_session, monkeypatch):
    """SPEC-05 §3: the whole tissue grid, no cap; the cache is keyed by slide checksum, embedder and grid."""
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(WIDTH_PX, HEIGHT_PX), raw_uri)
    pf, blobs = FakeAdapter(then=embed), InMemoryBlobStore()
    run_triage(stage, db_session, make_runtime(stage, adapters(pf=pf), blobs=blobs))
    first = output_json(stage)
    n_tiles = first["tile_grid"]["n_tiles"]
    sha = db_session.get(Slide, slide_id_of(stage)).checksum_sha256

    assert first["tile_grid"]["version"] == "grid224_v1" and first["tile_grid"]["tile_um"] == 224.0
    assert sum(len(request.images) for _, request, _ in pf.calls) == n_tiles
    assert first["embedding_cache"]["tiles_embedded"] == n_tiles and first["embedding_cache"]["tiles_cached"] == 0
    assert first["embedding_cache"]["uri"].endswith(f"embeddings/{sha}/models_5848531596314935296@1@2026-09-22/grid224_v1.parquet")

    stage.status = "running"
    log, calls_after_first = DecisionLog(), len(pf.calls)
    run_triage(stage, db_session, make_runtime(stage, adapters(pf=pf), blobs=blobs, log=log))
    second = output_json(stage)
    assert len(pf.calls) == calls_after_first
    assert not [r for r in log.pending() if r["task"] == "pf_embed"]
    assert second["embedding_cache"]["tiles_cached"] == n_tiles and second["embedding_cache"]["tiles_embedded"] == 0
    assert second["audit"]["endpoint_calls_made"] == 0
    head, calibrator = [r for r in log.pending() if r["task"] == "tumor_head"]
    assert head["input_spec"]["features"]["shape"] == [n_tiles, EMBEDDING_DIM]
    assert calibrator["input_spec"]["features"]["shape"] == [n_tiles, 1]


def test_a_slide_without_a_checksum_cannot_be_triaged(db_session, monkeypatch):
    from pipeline.tile_embeddings import MissingChecksumError

    stage, raw_uri = seed(db_session, checksum_sha256=None)
    install_fake_slide(monkeypatch, FakeOpenSlide(WIDTH_PX, HEIGHT_PX), raw_uri)
    pf = FakeAdapter(then=embed)
    with pytest.raises(MissingChecksumError):
        run_triage(stage, db_session, make_runtime(stage, adapters(pf=pf)))
    assert pf.calls == []


def slide_id_of(stage) -> str:
    return stage.input_ref["slide_id"]


def test_unreadable_slide_fails_instead_of_synthesising_tissue(db_session, monkeypatch):
    import openslide

    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(WIDTH_PX, HEIGHT_PX), raw_uri)

    def unreadable(path):
        raise openslide.OpenSlideUnsupportedFormatError("Unsupported or missing image file")

    monkeypatch.setattr(openslide, "OpenSlide", unreadable)
    pf = FakeAdapter(then=embed)
    with pytest.raises(SlideReadError, match="could not open slide"):
        run_triage(stage, db_session, make_runtime(stage, adapters(pf=pf)))
    assert pf.calls == []


def test_region_read_errors_fail_the_stage(db_session, monkeypatch):
    import openslide

    stage, raw_uri = seed(db_session)
    slide = install_fake_slide(monkeypatch, FakeOpenSlide(WIDTH_PX, HEIGHT_PX), raw_uri)

    def broken(location, level, size):
        raise openslide.OpenSlideError("TIFFRGBAImageGet failed")

    slide.read_region = broken
    with pytest.raises(SlideReadError, match="TIFFRGBAImageGet failed"):
        run_triage(stage, db_session, make_runtime(stage, adapters()))


def test_missing_slide_dimensions_are_refused(db_session, monkeypatch):
    stage, _ = seed(db_session, width_px=None)
    with pytest.raises(ValueError, match="no pixel dimensions"):
        run_triage(stage, db_session, make_runtime(stage, adapters()))


def test_embedding_outage_fails_the_stage(db_session, monkeypatch):
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(WIDTH_PX, HEIGHT_PX), raw_uri)
    log = DecisionLog()
    runtime = make_runtime(stage, adapters(pf=FakeAdapter(then=TransientCallError("503"))), log=log)
    with pytest.raises(ModelUnavailableError) as raised:
        run_triage(stage, db_session, runtime)
    assert raised.value.task == "pf_embed" and raised.value.producer_id == "path_foundation"
    assert {r["status"] for r in log.pending()} == {"unavailable"}


def test_missing_classifier_artifact_is_unavailable(db_session, monkeypatch, tmp_path):
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(WIDTH_PX, HEIGHT_PX), raw_uri)
    runtime = make_runtime(stage, adapters(sklearn=LocalSklearnAdapter(root=tmp_path)))
    with pytest.raises(ModelUnavailableError, match="does not exist"):
        run_triage(stage, db_session, runtime)
    # Nothing is trained in its place.
    assert not (tmp_path / "models").exists()


def test_invalid_referee_answer_fails_the_stage(db_session, monkeypatch):
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(WIDTH_PX, HEIGHT_PX), raw_uri)
    referee = FakeAdapter(then=json_text({"tumor_present": True, "lesion_type": "invasive_carcinoma",
                                          "cellularity": "high", "confidence": "high", "rationale": "v1 shape"}))
    with pytest.raises(SchemaInvalidError):
        run_triage(stage, db_session, make_runtime(stage, adapters(referee=referee)))


def test_allowed_referee_outage_leaves_candidates_unverified(db_session, monkeypatch):
    config = get_pipeline_config()
    policy = config.fallbacks.model_validate(
        {"fallbacks": [{"task": "tumor_referee", "on": ["ModelUnavailableError"], "to": None}]}
    )
    config = config.model_copy(update={"fallbacks": policy})
    stage, raw_uri = seed(db_session)
    install_fake_slide(monkeypatch, FakeOpenSlide(WIDTH_PX, HEIGHT_PX), raw_uri)
    log = DecisionLog()
    runtime = make_runtime(stage, adapters(referee=FakeAdapter(then=TransientCallError("503"))), config=config, log=log)

    run_triage(stage, db_session, runtime)

    hotspots = output_json(stage)["hotspots"]
    assert hotspots and all(h["referee"]["tumor_present"] is None and h["referee"]["needs_human"] for h in hotspots)
    fallbacks = [r for r in log.pending() if r["producer_kind"] == "fallback"]
    assert fallbacks and {r["task"] for r in fallbacks} == {"tumor_referee"}
